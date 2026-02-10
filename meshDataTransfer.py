# SPDX-License-Identifier: GPL-2.0-or-later
# Shape Keys Transfer (simplified)

import bpy
import bmesh
import numpy as np

from mathutils import Vector, kdtree
from mathutils.bvhtree import BVHTree

from bpy.types import PropertyGroup, Operator
from bpy.props import (
    BoolProperty,
    EnumProperty,
    PointerProperty,
    StringProperty,
)


# -----------------------------------------------------------------------------
# Poll helpers
# -----------------------------------------------------------------------------

def _mesh_poll(self, obj):
    return obj is not None and obj.type == "MESH"


# -----------------------------------------------------------------------------
# Core math / geometry
# -----------------------------------------------------------------------------

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


def _get_sk_coords_world(obj, key_name: str) -> np.ndarray:
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
    w = np.einsum("ij,aj->ai", mw, co4)[:, :3]
    return w


def _get_base_coords_world(obj) -> np.ndarray:
    v_count = len(obj.data.vertices)
    co = np.zeros(v_count * 3, dtype=np.float32)
    obj.data.vertices.foreach_get("co", co)
    co.shape = (v_count, 3)

    mw = np.array(obj.matrix_world, dtype=np.float32)
    co4 = np.ones((v_count, 4), dtype=np.float32)
    co4[:, :3] = co
    w = np.einsum("ij,aj->ai", mw, co4)[:, :3]
    return w


def _world_to_local(obj, coords_world: np.ndarray) -> np.ndarray:
    imw = np.array(obj.matrix_world.inverted(), dtype=np.float32)
    co4 = np.ones((coords_world.shape[0], 4), dtype=np.float32)
    co4[:, :3] = coords_world
    loc = np.einsum("ij,aj->ai", imw, co4)[:, :3]
    return loc


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


# -----------------------------------------------------------------------------
# Transfer implementation (Closest)
# -----------------------------------------------------------------------------

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
        """
        For each target vertex (in world), find closest source triangle:
        returns barycentric coords and triangle vertex indices (source mesh vertex ids).
        """
        v_count = len(target_obj.data.vertices)
        tgt_world = _get_base_coords_world(target_obj)

        bary = np.zeros((v_count, 3), dtype=np.float32)
        tri_ids = np.zeros((v_count, 3), dtype=np.int64)
        hit = np.zeros((v_count, 1), dtype=np.float32)

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
            hit[i] = 1.0

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

    # Projection cache
    proj = _ClosestProjector(source_obj)
    try:
        bary, tri_ids, missed = proj.project(target_obj)
    finally:
        # Keep bmesh around only while projecting
        pass

    mask = _get_vertex_group_mask(target_obj, vertex_group)
    tgt_base_world = _get_base_coords_world(target_obj)

    src_base_world = _get_base_coords_world(source_obj)

    # Pre-build KD for snap (base mesh vertices)
    if snap_to_closest:
        # For best results, snap after transferring each key.
        pass

    # Transfer each shape key
    for sk_name in names:
        src_sk_world = _get_sk_coords_world(source_obj, sk_name)
        if src_sk_world is None or len(src_sk_world) != len(source_obj.data.vertices):
            continue

        idx = tri_ids.ravel()
        tri_coords = src_sk_world[idx].reshape((len(tri_ids), 3, 3))
        dst_world = _barycentric_location(tri_coords, bary)

        # Missed projections -> keep target base coords
        dst_world = dst_world * (1.0 - missed) + tgt_base_world * missed

        # Mask blend (only where mask>0)
        if mask is not None:
            inv = 1.0 - mask
            dst_world = dst_world * mask + tgt_base_world * inv

        if snap_to_closest:
            dst_world = _snap_to_source_vertices(dst_world, src_base_world)

        dst_local = _world_to_local(target_obj, dst_world)
        _set_sk_coords_local(target_obj, sk_name, dst_local)

        # Copy slider range (best-effort)
        try:
            src_kb = source_obj.data.shape_keys.key_blocks[sk_name]
            dst_kb = target_obj.data.shape_keys.key_blocks[sk_name]
            dst_kb.slider_min = src_kb.slider_min
            dst_kb.slider_max = src_kb.slider_max
        except Exception:
            pass

    # Cleanup
    proj.free()
    return True, ""


# -----------------------------------------------------------------------------
# Settings + Operator + UI
# -----------------------------------------------------------------------------

class SKV_ShapeKeysTransferSettings(PropertyGroup):
    # Keep property for UI parity, but only one real option is implemented
    search_method: EnumProperty(
        items=[
            ('CLOSEST', 'Closest', "Closest point on source surface", 1),
        ],
        name="Search Method",
        default='CLOSEST',
    )

    attributes_to_transfer: EnumProperty(
        items=[
            ("SHAPE_KEYS", "Shape Keys", "", 1),
        ],
        name="Attribute",
        default="SHAPE_KEYS",
    )

    mesh_source: PointerProperty(
        name="Source",
        type=bpy.types.Object,
        poll=_mesh_poll,
    )

    vertex_group_filter: StringProperty(
        name="Vertex Group",
        default="",
        description="Optional mask on Target (weights 0..1). Empty = no mask.",
    )

    exclude_muted_shapekeys: BoolProperty(
        name="Exclude Muted",
        default=False,
        description="Skip muted shape keys on Source.",
    )

    snap_to_closest_vertex: BoolProperty(
        name="Snap to Closest Vertex",
        default=False,
        description="Snap transferred coordinates to nearest Source vertex (world space).",
    )


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
            vertex_group=p.vertex_group_filter.strip(),
            exclude_muted=p.exclude_muted_shapekeys,
            snap_to_closest=p.snap_to_closest_vertex,
        )
        if not ok:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
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
    col.prop(p, "search_method", text="")
    col.prop(p, "attributes_to_transfer", text="")
    col.separator()
    col.prop(p, "vertex_group_filter")
    col.prop(p, "exclude_muted_shapekeys")
    col.prop(p, "snap_to_closest_vertex")
    col.separator()
    col.operator("skv.transfer_shape_keys", icon="MOD_DATA_TRANSFER")


CLASSES = (
    SKV_ShapeKeysTransferSettings,
    SKV_OT_TransferShapeKeys,
)
