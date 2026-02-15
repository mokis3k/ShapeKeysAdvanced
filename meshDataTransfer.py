# SPDX-License-Identifier: GPL-2.0-or-later
# Integrated Mesh Data Transfer module (Shape Keys transfer only)
#
# Notes:
# - This module intentionally keeps the original transfer algorithm (BVH + barycentric sampling).
# - Only the functionality used by this addon UI is retained:
#   * Search method: Closest
#   * Space: Local
#   * Attribute: Shape Keys
#   * Optional vertex group mask (no invert)

import bpy
import bmesh
import numpy as np

from mathutils import Vector
from mathutils.bvhtree import BVHTree

from bpy.types import PropertyGroup, Operator
from bpy.props import PointerProperty, StringProperty


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _is_mesh_object(obj) -> bool:
    return obj is not None and obj.type == "MESH"


def _mesh_poll(self, obj):
    return _is_mesh_object(obj)


# -----------------------------------------------------------------------------
# Mesh cache (source/target)
# -----------------------------------------------------------------------------

class MeshData:
    """Lightweight mesh wrapper providing numpy accessors + BVH for sampling."""

    def __init__(self, obj, triangulate=True):
        if not _is_mesh_object(obj):
            raise TypeError("MeshData requires a mesh object")
        self.obj = obj
        self.mesh = obj.data
        self.triangulate = bool(triangulate)

        self.bvhtree = None
        self.transfer_bmesh = None

    def free(self) -> None:
        if self.transfer_bmesh is not None:
            try:
                self.transfer_bmesh.free()
            except Exception:
                pass
        self.transfer_bmesh = None
        self.bvhtree = None

    @property
    def shape_keys(self):
        if self.mesh.shape_keys:
            return self.mesh.shape_keys.key_blocks
        return None

    @property
    def v_count(self) -> int:
        return len(self.mesh.vertices)

    def get_verts_position(self) -> np.ndarray:
        v_count = len(self.mesh.vertices)
        co = np.zeros(v_count * 3, dtype=np.float32)
        self.mesh.vertices.foreach_get("co", co)
        co.shape = (v_count, 3)
        return co

    def get_selected_verts(self) -> np.ndarray:
        v_count = len(self.mesh.vertices)
        sel = np.zeros(v_count, dtype=np.float32)
        self.mesh.vertices.foreach_get("select", sel)
        sel.shape = (v_count, 1)
        return sel

    # ---- Vertex group mask ----

    def get_vertex_group_weights(self, vertex_group_name: str):
        v_group = self.obj.vertex_groups.get(vertex_group_name)
        if not v_group:
            return None

        v_count = len(self.mesh.vertices)
        weights = np.zeros(v_count, dtype=np.float32)
        for i in range(v_count):
            try:
                weights[i] = v_group.weight(i)
            except RuntimeError:
                weights[i] = 0.0
        weights.shape = (v_count, 1)
        return weights

    # ---- Shape keys IO ----

    def get_shape_keys_vert_pos(self, exclude_muted=False):
        if not self.shape_keys:
            return None
        data = {}
        for sk in self.shape_keys:
            if sk.name == "Basis":
                continue
            if exclude_muted and getattr(sk, "mute", False):
                continue
            data[sk.name] = self.get_shape_key_vert_pos(sk.name)
        return data

    def get_shape_key_vert_pos(self, shape_key_name: str):
        if not self.shape_keys:
            return None
        sk = self.shape_keys.get(shape_key_name)
        if not sk:
            return None
        v_count = len(sk.data)
        co = np.zeros(v_count * 3, dtype=np.float32)
        sk.data.foreach_get("co", co)
        co.shape = (v_count, 3)
        return co

    def set_position_as_shape_key(self, shape_key_name: str, co: np.ndarray) -> None:
        # Ensure Basis exists
        if not self.shape_keys:
            self.obj.shape_key_add(name="Basis", from_mix=False)

        key_blocks = self.mesh.shape_keys.key_blocks
        if shape_key_name in key_blocks:
            sk = key_blocks[shape_key_name]
        else:
            sk = self.obj.shape_key_add(name=shape_key_name, from_mix=False)

        sk.data.foreach_set("co", co.ravel())

    # ---- BVH / transfer mesh ----

    def ensure_mesh_data(self) -> None:
        """Build transfer_bmesh and BVH once."""
        if self.transfer_bmesh is not None and self.bvhtree is not None:
            return

        bm = bmesh.new()
        bm.from_mesh(self.mesh)

        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        if self.triangulate:
            bmesh.ops.triangulate(bm, faces=bm.faces)
            bm.faces.ensure_lookup_table()

        self.transfer_bmesh = bm
        self.bvhtree = BVHTree.FromBMesh(self.transfer_bmesh)


# -----------------------------------------------------------------------------
# Transfer engine (Closest + barycentric sampling)
# -----------------------------------------------------------------------------

class MeshDataTransfer:
    def __init__(
        self,
        source,
        target,
        vertex_group=None,
        exclude_muted_shapekeys=False,
        restrict_to_selection=False,
    ):
        self.vertex_group = vertex_group
        self.exclude_muted_shapekeys = bool(exclude_muted_shapekeys)
        self.restrict_to_selection = bool(restrict_to_selection)

        self.source = MeshData(source)
        self.target = MeshData(target)

        self._projection_ready = False
        self.missed_projections = None
        self.ray_casted = None
        self.hit_faces = None
        self.related_ids = None
        self.has_zero_area_faces = False
        self.barycentric_coords = None

    def free(self) -> None:
        if self.target:
            self.target.free()
        if self.source:
            self.source.free()

    # ---- Masks ----

    def get_vertices_mask(self):
        selection = None
        if self.restrict_to_selection:
            selection = self.target.get_selected_verts()

        if self.vertex_group:
            v_group = self.target.get_vertex_group_weights(self.vertex_group)
            if v_group is None:
                return selection
            if selection is not None:
                v_group = v_group * selection
            return v_group

        return selection

    # ---- Projection cache ----

    def ensure_projection_cache(self) -> None:
        if self._projection_ready:
            return

        self.source.ensure_mesh_data()
        self.target.ensure_mesh_data()

        self.cast_verts()
        self.has_zero_area_faces = self.check_zero_area_triangles(self.hit_faces)
        self.barycentric_coords = self.get_barycentric_coords(self.ray_casted, self.hit_faces)
        self._projection_ready = True

    def cast_verts(self):
        self.target.transfer_bmesh.verts.ensure_lookup_table()
        v_count = len(self.target.mesh.vertices)

        self.ray_casted = np.zeros((v_count, 3), dtype=np.float32)
        self.hit_faces = np.zeros((v_count, 3, 3), dtype=np.float32)
        self.related_ids = np.zeros((v_count, 3), dtype=np.int64)

        self.source.transfer_bmesh.faces.ensure_lookup_table()
        self.missed_projections = np.ones((v_count, 3), dtype=bool)

        for v in self.target.transfer_bmesh.verts:
            projection = self.source.bvhtree.find_nearest(v.co)

            if projection[0]:
                face = self.source.transfer_bmesh.faces[projection[2]]
                tri = (face.verts[0].co, face.verts[1].co, face.verts[2].co)

                v1_id, v2_id, v3_id = face.verts[0].index, face.verts[1].index, face.verts[2].index
                v_array = np.array([v1_id, v2_id, v3_id], dtype=np.int64)

                vid = v.index
                self.ray_casted[vid] = projection[0]
                self.missed_projections[vid] = False
                self.hit_faces[vid] = tri
                self.related_ids[vid] = v_array
            else:
                vid = v.index
                self.ray_casted[vid] = v.co

        return self.ray_casted, self.hit_faces, self.related_ids

    @staticmethod
    def check_zero_area_triangles(triangles: np.ndarray) -> bool:
        v1 = triangles[:, 1] - triangles[:, 0]
        v2 = triangles[:, 2] - triangles[:, 0]
        area_vectors = np.cross(v1, v2)
        area_magnitudes = np.linalg.norm(area_vectors, axis=1)
        zero_area = np.isclose(area_magnitudes, 0)
        return bool(zero_area.any())

    @staticmethod
    def get_barycentric_coords(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
        v0 = triangles[:, 1] - triangles[:, 0]
        v1 = triangles[:, 2] - triangles[:, 0]
        v2 = points - triangles[:, 0]

        d00 = np.einsum("ij,ij->i", v0, v0)
        d01 = np.einsum("ij,ij->i", v0, v1)
        d11 = np.einsum("ij,ij->i", v1, v1)
        d20 = np.einsum("ij,ij->i", v2, v0)
        d21 = np.einsum("ij,ij->i", v2, v1)

        denom = (d00 * d11 - d01 * d01)

        # Avoid division by zero warnings; NaNs are handled later via has_zero_area_faces.
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (d11 * d20 - d01 * d21) / denom
            w = (d00 * d21 - d01 * d20) / denom
            u = 1.0 - v - w

        bary = np.stack((u, v, w), axis=1).astype(np.float32)
        return bary

    @staticmethod
    def calculate_barycentric_location(tri_points: np.ndarray, bary_coords: np.ndarray) -> np.ndarray:
        return (
            tri_points[:, 0] * bary_coords[:, [0]] +
            tri_points[:, 1] * bary_coords[:, [1]] +
            tri_points[:, 2] * bary_coords[:, [2]]
        )

    def get_transferred_vert_coords(self, transfer_coord: np.ndarray) -> np.ndarray:
        indexes = self.related_ids.ravel()
        sorted_coords = transfer_coord[indexes]
        sorted_coords.shape = self.hit_faces.shape
        transferred = self.calculate_barycentric_location(sorted_coords, self.barycentric_coords)
        return transferred

    # ---- Shape keys transfer ----

    def transfer_shape_keys(self, shapekey_names=None) -> bool:
        shape_keys = self.source.get_shape_keys_vert_pos(exclude_muted=self.exclude_muted_shapekeys)
        if not shape_keys:
            return False

        self.ensure_projection_cache()

        # Target Basis coordinates (local)
        undeformed_verts = self.target.get_verts_position()

        # Source Basis coords (local)
        base_coords = self.source.get_verts_position()

        # Transfer source Basis to target space (for delta extraction)
        base_transferred_position = self.get_transferred_vert_coords(base_coords)
        if self.has_zero_area_faces:
            base_transferred_position = np.where(
                np.isnan(base_transferred_position),
                undeformed_verts,
                base_transferred_position,
            )

        # Fallback for missed projections
        base_transferred_position = np.where(self.missed_projections, undeformed_verts, base_transferred_position)

        masked_vertices = self.get_vertices_mask()

        # Preserve target slider values and active key index (avoid "stuck" deformation after transfer).
        target_obj = self.target.obj
        pre_active_index = int(getattr(target_obj, "active_shape_key_index", 0))
        pre_values = {}
        if self.target.shape_keys:
            for kb in self.target.shape_keys:
                pre_values[kb.name] = float(kb.value)

        for sk_name, sk_points in shape_keys.items():
            if shapekey_names is not None and sk_name not in shapekey_names:
                continue
            src_kb = self.source.shape_keys.get(sk_name) if self.source.shape_keys else None
            slider_min = src_kb.slider_min if src_kb else 0.0
            slider_max = src_kb.slider_max if src_kb else 1.0

            transferred_sk = self.get_transferred_vert_coords(sk_points)
            transferred_sk = np.where(self.missed_projections, undeformed_verts, transferred_sk)

            if self.has_zero_area_faces:
                transferred_sk = np.where(np.isnan(transferred_sk), undeformed_verts, transferred_sk)

            # Extract deltas in target local space
            delta = transferred_sk - base_transferred_position

            # Apply vertex group mask. Outside mask: keep existing target shape (if any), otherwise Basis.
            if masked_vertices is not None:
                delta = delta * masked_vertices

                if self.target.shape_keys and self.target.shape_keys.get(sk_name):
                    old = self.target.get_shape_key_vert_pos(sk_name)
                    if old is not None:
                        inverted_mask = 1.0 - masked_vertices
                        old_delta = (old - undeformed_verts) * inverted_mask
                        delta = old_delta + delta

            final_coords = undeformed_verts + delta

            self.target.set_position_as_shape_key(shape_key_name=sk_name, co=final_coords)

            # Restore slider limits and keep target value unchanged (or 0.0 for new keys).
            try:
                dst_kb = self.target.shape_keys.get(sk_name)
                if dst_kb:
                    dst_kb.slider_min = slider_min
                    dst_kb.slider_max = slider_max
                    dst_kb.value = pre_values.get(sk_name, 0.0)
            except Exception:
                pass

        # Restore active key index (best-effort).
        try:
            target_obj.active_shape_key_index = pre_active_index if self.target.shape_keys else 0
        except Exception:
            pass

        return True


# -----------------------------------------------------------------------------
# Properties
# -----------------------------------------------------------------------------

class SKV_MeshDataSettings(PropertyGroup):
    mesh_source: PointerProperty(
        name="Source",
        type=bpy.types.Object,
        poll=_mesh_poll,
    )

    # Kept as StringProperty for robustness; UI uses prop_search to present it as a dropdown list.
    vertex_group_filter: StringProperty(
        name="Vertex Group",
        default="",
    )

    transfer_status: StringProperty(
        name="Status",
        default="",
        options={'SKIP_SAVE'},
    )


# -----------------------------------------------------------------------------
# Operator
# -----------------------------------------------------------------------------

class SKV_OT_TransferMeshData(Operator):
    bl_idname = "skv.transfer_mesh_data"
    bl_label = "Transfer Shape Keys"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not _is_mesh_object(obj):
            return False
        if context.object is None or context.object.mode != "OBJECT":
            return False
        p = getattr(obj, "skv_mesh_data_transfer", None)
        return p is not None and p.mesh_source is not None

    def execute(self, context):
        active = context.active_object
        p = active.skv_mesh_data_transfer

        def _clear_fields():
            try:
                p.mesh_source = None
                p.vertex_group_filter = ""
            except Exception:
                pass

        source = p.mesh_source
        if not _is_mesh_object(source):
            p.transfer_status = "Failed transfer"
            _clear_fields()
            self.report({'ERROR'}, "Invalid source object (must be Mesh).")
            return {'CANCELLED'}

        mask_vertex_group = p.vertex_group_filter.strip() if p.vertex_group_filter else ""
        mask_vertex_group = mask_vertex_group if mask_vertex_group else None

        transfer_data = MeshDataTransfer(
            source=source,
            target=active,
            vertex_group=mask_vertex_group,
            exclude_muted_shapekeys=False,
            restrict_to_selection=False,
        )

        try:
            ok = bool(transfer_data.transfer_shape_keys())
        except RuntimeError as e:
            p.transfer_status = "Failed transfer"
            _clear_fields()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        finally:
            transfer_data.free()

        p.transfer_status = "Transfer was successful" if ok else "Failed transfer"
        _clear_fields()

        return {'FINISHED'} if ok else {'CANCELLED'}


# -----------------------------------------------------------------------------
# UI hook (called from main addon panel)
# -----------------------------------------------------------------------------

def draw_transfer_ui(layout, context):
    obj = context.active_object
    if not _is_mesh_object(obj):
        layout.label(text="Active object is not a Mesh", icon="ERROR")
        return

    p = getattr(obj, "skv_mesh_data_transfer", None)
    if p is None:
        layout.label(text="Transfer settings not available", icon="ERROR")
        return

    col = layout.column(align=True)
    col.prop(p, "mesh_source")
    col.separator()

    # Dropdown-style selection from existing vertex groups (target object).
    col.prop_search(p, "vertex_group_filter", obj, "vertex_groups", text="Vertex Group")
    col.separator()

    status = (getattr(p, "transfer_status", "") or "").strip()
    if status:
        icon = "INFO" if status == "Transfer was successful" else "ERROR" if status == "Failed transfer" else "INFO"
        col.label(text=status, icon=icon)
        col.separator()

    col.operator("skv.transfer_mesh_data", text="Transfer", icon="PASTEDOWN")


# -----------------------------------------------------------------------------
# Registration (hooked by main addon)
# -----------------------------------------------------------------------------

CLASSES = (
    SKV_MeshDataSettings,
    SKV_OT_TransferMeshData,
)
