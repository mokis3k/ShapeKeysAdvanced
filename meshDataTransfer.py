# SPDX-License-Identifier: GPL-2.0-or-later
# Shape Keys Transfer (simplified: Closest + Shape Keys)

import bpy
import bmesh
import numpy as np

from mathutils import Vector, kdtree
from mathutils.bvhtree import BVHTree

from bpy.types import PropertyGroup, Operator
from bpy.props import (
    BoolProperty,
    PointerProperty,
    StringProperty,
)


def _mesh_poll(self, obj):
    return obj is not None and obj.type == "MESH"


def _clear_status(self, context):
    try:
        self.status_message = ""
        self.status_is_error = False
    except Exception:
        pass


def _barycentric(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Compute barycentric coordinates of points w.r.t. triangles."""
    v0 = tris[:, 1] - tris[:, 0]
    v1 = tris[:, 2] - tris[:, 0]
    v2 = points - tris[:, 0]

    d00 = np.einsum("ij,ij->i", v0, v0)
    d01 = np.einsum("ij,ij->i", v0, v1)
    d11 = np.einsum("ij,ij->i", v1, v1)
    d20 = np.einsum("ij,ij->i", v2, v0)
    d21 = np.einsum("ij,ij->i", v2, v1)

    denom = (d00 * d11 - d01 * d01)
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1.0 - v - w
    return np.stack((u, v, w), axis=1).astype(np.float32)


def _barycentric_location(tri_points: np.ndarray, bary: np.ndarray) -> np.ndarray:
    return (
        tri_points[:, 0] * bary[:, [0]] +
        tri_points[:, 1] * bary[:, [1]] +
        tri_points[:, 2] * bary[:, [2]]
    )


def _shape_key_names(source_obj, exclude_muted: bool) -> list[str]:
    sk = source_obj.data.shape_keys
    if not sk:
        return []
    names = []
    for kb in sk.key_blocks:
        if kb.name == "Basis":
            continue
        if exclude_muted and getattr(kb, "mute", False):
            continue
        names.append(kb.name)
    return names


def _get_sk_coords_world(obj, key_name: str) -> np.ndarray | None:
    sk = obj.data.shape_keys
    if not sk:
        return None
    kb = sk.key_blocks.get(key_name)
    if not kb:
        return None
    v_count = len(kb.data)
    co = np.zeros(v_count * 3, dtype=np.float32)
    kb.data.foreach_get("co", co)
    co.shape = (v_count, 3)

    mw = np.array(obj.matrix_world, dtype=np.float32)
    co4 = np.ones((v_count, 4), dtype=np.float32)
    co4[:, :3] = co
    return np.einsum("ij,aj->ai", mw, co4)[:, :3]


def _get_base_coords_world(obj) -> np.ndarray:
    v_count = len(obj.data.vertices)
    co = np.zeros(v_count * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    co.shape = (v_count, 3)

    mw = np.array(obj.matrix_world, dtype=np.float32)
    co4 = np.ones((v_count, 4), dtype=np.float32)
    co4[:, :3] = co
    return np.einsum("ij,aj->ai", mw, co4)[:, :3]


def _world_to_local(obj, coords_world: np.ndarray) -> np.ndarray:
    imw = np.array(obj.matrix_world.inverted(), dtype=np.float32)
    co4 = np.ones((coords_world.shape[0], 4), dtype=np.float32)
    co4[:, :3] = coords_world
    return np.einsum("ij,aj->ai", imw, co4)[:, :3]


def _ensure_target_shape_key(obj, name: str):
    if not obj.data.shape_keys:
        obj.shape_key_add(name="Basis", from_mix=False)
    sk = obj.data.shape_keys
    if name in sk.key_blocks:
        return sk.key_blocks[name]
    return obj.shape_key_add(name=name, from_mix=False)


def _set_sk_coords_local(obj, name: str, coords_local: np.ndarray):
    kb = _ensure_target_shape_key(obj, name)
    kb.data.foreach_set("co", coords_local.ravel())
    return kb


def _get_vertex_group_mask(obj, group_name: str) -> np.ndarray | None:
    if not group_name:
        return None
    vg = obj.vertex_groups.get(group_name)
    if not vg:
        return None
    v_count = len(obj.data.vertices)
    w = np.zeros(v_count, dtype=np.float32)
    for i in range(v_count):
        try:
            w[i] = vg.weight(i)
        except RuntimeError:
            w[i] = 0.0
    w.shape = (v_count, 1)
    return w


def _snap_to_source_vertices(coords_world: np.ndarray, source_coords_world: np.ndarray) -> np.ndarray:
    kd = kdtree.KDTree(len(source_coords_world))
    for i, co in enumerate(source_coords_world):
        kd.insert(Vector(co), i)
    kd.balance()

    out = coords_world.copy()
    for i in range(len(out)):
        out[i] = kd.find(Vector(out[i]))[0]
    return out


class _ClosestProjector:
    def __init__(self, source_obj):
        self.source_obj = source_obj
        self.bm = bmesh.new()

        depsgraph = bpy.context.evaluated_depsgraph_get()
        src_eval = source_obj.evaluated_get(depsgraph)
        mesh_eval = src_eval.to_mesh()

        self.bm.from_mesh(mesh_eval)
        src_eval.to_mesh_clear()

        self.bm.verts.ensure_lookup_table()
        self.bm.faces.ensure_lookup_table()
        bmesh.ops.triangulate(self.bm, faces=self.bm.faces)

        mw = source_obj.matrix_world
        for v in self.bm.verts:
            v.co = mw @ v.co

        self.bm.verts.ensure_lookup_table()
        self.bm.faces.ensure_lookup_table()
        self.bvh = BVHTree.FromBMesh(self.bm)

    def free(self):
        try:
            self.bm.free()
        except Exception:
            pass

    def project(self, target_obj):
        v_count = len(target_obj.data.vertices)
        tgt_world = _get_base_coords_world(target_obj)

        bary = np.zeros((v_count, 3), dtype=np.float32)
        tri_ids = np.zeros((v_count, 3), dtype=np.int64)
        missed = np.ones((v_count, 1), dtype=np.float32)

        for i in range(v_count):
            loc, normal, face_index, dist = self.bvh.find_nearest(Vector(tgt_world[i]))
            if loc is None:
                continue

            face = self.bm.faces[face_index]
            v1, v2, v3 = face.verts[0], face.verts[1], face.verts[2]
            tri = np.array([[v1.co.x, v1.co.y, v1.co.z],
                            [v2.co.x, v2.co.y, v2.co.z],
                            [v3.co.x, v3.co.y, v3.co.z]], dtype=np.float32)
            p = np.array([[loc.x, loc.y, loc.z]], dtype=np.float32)

            b = _barycentric(p, tri.reshape((1, 3, 3)))[0]
            bary[i] = b
            tri_ids[i] = np.array([v1.index, v2.index, v3.index], dtype=np.int64)
            missed[i] = 0.0

        return bary, tri_ids, missed


def transfer_shape_keys_closest(
    source_obj,
    target_obj,
    vertex_group: str = "",
    exclude_muted: bool = False,
    snap_to_closest: bool = False,
):
    names = _shape_key_names(source_obj, exclude_muted=exclude_muted)
    if not names:
        return False, "Source has no shape keys."

    if len(source_obj.data.vertices) == 0 or len(target_obj.data.vertices) == 0:
        return False, "Source/Target mesh has no vertices."

    proj = _ClosestProjector(source_obj)
    try:
        bary, tri_ids, missed = proj.project(target_obj)

        mask = _get_vertex_group_mask(target_obj, vertex_group.strip())
        tgt_base_world = _get_base_coords_world(target_obj)
        src_base_world = _get_base_coords_world(source_obj)

        for sk_name in names:
            src_sk_world = _get_sk_coords_world(source_obj, sk_name)
            if src_sk_world is None or len(src_sk_world) != len(source_obj.data.vertices):
                continue

            idx = tri_ids.ravel()
            tri_coords = src_sk_world[idx].reshape((len(tri_ids), 3, 3))
            dst_world = _barycentric_location(tri_coords, bary)

            dst_world = dst_world * (1.0 - missed) + tgt_base_world * missed

            if mask is not None:
                inv = 1.0 - mask
                dst_world = dst_world * mask + tgt_base_world * inv

            if snap_to_closest:
                dst_world = _snap_to_source_vertices(dst_world, src_base_world)

            dst_local = _world_to_local(target_obj, dst_world)
            dst_kb = _set_sk_coords_local(target_obj, sk_name, dst_local)

            # Copy slider range, value and mute
            try:
                src_kb = source_obj.data.shape_keys.key_blocks[sk_name]
                dst_kb.slider_min = src_kb.slider_min
                dst_kb.slider_max = src_kb.slider_max
                dst_kb.value = float(src_kb.value)
                dst_kb.mute = bool(getattr(src_kb, "mute", False))
            except Exception:
                pass

        if not target_obj.data.shape_keys:
            return False, "Failed to create shape keys on target."

        return True, ""
    finally:
        proj.free()


class SKV_ShapeKeysTransferSettings(PropertyGroup):
    mesh_source: PointerProperty(
        name="Source",
        type=bpy.types.Object,
        poll=_mesh_poll,
        update=_clear_status,
    )

    vertex_group_filter: StringProperty(
        name="Vertex Group",
        default="",
        description="Optional mask on Target (weights 0..1). Empty = no mask.",
        update=_clear_status,
    )

    exclude_muted_shapekeys: BoolProperty(
        name="Exclude Muted",
        default=False,
        description="Skip muted shape keys on Source.",
        update=_clear_status,
    )

    snap_to_closest_vertex: BoolProperty(
        name="Snap to Closest",
        default=False,
        description="Snap transferred coordinates to nearest Source vertex (world space).",
        update=_clear_status,
    )

    status_message: StringProperty(name="Status", default="")
    status_is_error: BoolProperty(name="Is Error", default=False)


class SKV_OT_TransferShapeKeys(Operator):
    bl_idname = "skv.transfer_shape_keys"
    bl_label = "Transfer Shape Keys"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != "MESH":
            return False
        p = getattr(obj, "skv_shape_keys_transfer", None)
        if p is None:
            return False
        if p.mesh_source is None or p.mesh_source.type != "MESH":
            return False
        return True

    def execute(self, context):
        target = context.active_object
        p = target.skv_shape_keys_transfer
        source = p.mesh_source

        ok, err = transfer_shape_keys_closest(
            source_obj=source,
            target_obj=target,
            vertex_group=p.vertex_group_filter,
            exclude_muted=p.exclude_muted_shapekeys,
            snap_to_closest=p.snap_to_closest_vertex,
        )

        if not ok:
            p.status_is_error = True
            p.status_message = err or "Transfer failed."
            self.report({'ERROR'}, p.status_message)
            return {'CANCELLED'}

        # Auto scan after successful transfer (populate Groups module & show keys)
        try:
            bpy.ops.skv.init_rescan()
        except Exception:
            pass

        p.status_is_error = False
        p.status_message = f"Transfer OK: {source.name} -> {target.name}"
        self.report({'INFO'}, p.status_message)
        return {'FINISHED'}


def draw_transfer_ui(layout, context):
    obj = context.active_object
    if not obj or obj.type != "MESH":
        layout.label(text="Active object is not a Mesh", icon="ERROR")
        return

    p = getattr(obj, "skv_shape_keys_transfer", None)
    if p is None:
        layout.label(text="Transfer settings not available", icon="ERROR")
        return

    col = layout.column(align=True)
    col.prop(p, "mesh_source")
    col.prop(p, "vertex_group_filter")

    row = col.row(align=True)
    row.prop(p, "exclude_muted_shapekeys", text="Exclude Muted", toggle=True)
    row.prop(p, "snap_to_closest_vertex", text="Snap to Closest", toggle=True)

    col.separator()
    col.operator("skv.transfer_shape_keys", icon="MOD_DATA_TRANSFER")

    if p.status_message:
        icon = "ERROR" if p.status_is_error else "CHECKMARK"
        col.separator()
        col.label(text=p.status_message, icon=icon)


CLASSES = (
    SKV_ShapeKeysTransferSettings,
    SKV_OT_TransferShapeKeys,
)
