# # SPDX-License-Identifier: GPL-2.0-or-later
# # Integrated Mesh Data Transfer module (adapted from single-file version)
#
# import bpy
# import bmesh
# import numpy as np
#
# from mathutils import Vector, kdtree
# from mathutils.bvhtree import BVHTree
#
# from bpy.types import PropertyGroup, Operator
# from bpy.props import (
#     BoolProperty,
#     EnumProperty,
#     PointerProperty,
#     StringProperty,
# )
#
#
# # -----------------------------------------------------------------------------
# # Utility helpers
# # -----------------------------------------------------------------------------
#
# def _is_mesh_object(obj) -> bool:
#     return obj is not None and obj.type == "MESH"
#
#
# def _is_armature_object(obj) -> bool:
#     return obj is not None and obj.type == "ARMATURE"
#
#
# def _safe_mode_set(mode: str) -> None:
#     """Set mode if possible (Blender raises if no active object)."""
#     try:
#         if bpy.context.object and bpy.context.object.mode != mode:
#             bpy.ops.object.mode_set(mode=mode)
#     except Exception:
#         # Avoid failing hard during cleanup/restore.
#         pass
#
#
# def _set_active_object(obj) -> None:
#     try:
#         bpy.context.view_layer.objects.active = obj
#     except Exception:
#         pass
#
#
# def _mesh_poll(self, obj):
#     return _is_mesh_object(obj)
#
#
# def _arm_poll(self, obj):
#     return _is_armature_object(obj)
#
#
# # -----------------------------------------------------------------------------
# # Core data model
# # -----------------------------------------------------------------------------
#
# class MeshData:
#     def __init__(self, obj, deformed=False, world_space=False, uv_space=False, triangulate=True):
#         if not _is_mesh_object(obj):
#             raise TypeError("MeshData requires a mesh object")
#         self.obj = obj
#         self.mesh = obj.data
#         self.deformed = deformed
#         self.world_space = world_space
#         self.uv_space = uv_space
#         self.triangulate = triangulate
#
#         self.bvhtree = None
#         self.transfer_bmesh = None
#         self.vertex_map = {}  # uv-vertex index -> list(mesh vertex ids) OR identity for normal mesh
#
#     def free(self):
#         if self.transfer_bmesh:
#             try:
#                 self.transfer_bmesh.free()
#             except Exception:
#                 pass
#             self.transfer_bmesh = None
#         self.bvhtree = None
#         self.vertex_map = {}
#
#     @property
#     def seam_edges(self):
#         return self.get_seam_edges()
#
#     @seam_edges.setter
#     def seam_edges(self, edges):
#         self.set_seam_edges(edges)
#
#     @property
#     def shape_keys(self):
#         if self.mesh.shape_keys:
#             return self.mesh.shape_keys.key_blocks
#         return None
#
#     @property
#     def vertex_groups(self):
#         return self.obj.vertex_groups
#
#     @property
#     def v_count(self):
#         return len(self.mesh.vertices)
#
#     @property
#     def shape_keys_drivers(self):
#         if self.mesh.shape_keys and self.mesh.shape_keys.animation_data:
#             return self.mesh.shape_keys.animation_data.drivers
#         return None
#
#     @property
#     def shape_keys_names(self):
#         if self.shape_keys:
#             return [x.name for x in self.shape_keys]
#         return []
#
#     # ---- Mesh direct arrays ----
#
#     def get_seam_edges(self):
#         edges = self.mesh.edges
#         edges_array = [False] * len(edges)
#         edges.foreach_get("use_seam", edges_array)
#         return edges_array
#
#     def set_seam_edges(self, edges_array):
#         edges = self.mesh.edges
#         edges.foreach_set("use_seam", edges_array)
#
#     def get_verts_position(self):
#         v_count = len(self.mesh.vertices)
#         co = np.zeros(v_count * 3, dtype=np.float32)
#         self.mesh.vertices.foreach_get("co", co)
#         co.shape = (v_count, 3)
#         return co
#
#     def get_selected_verts(self):
#         v_count = len(self.mesh.vertices)
#         sel = np.zeros(v_count, dtype=np.float32)
#         self.mesh.vertices.foreach_get("select", sel)
#         sel.shape = (v_count, 1)
#         return sel
#
#     def set_verts_position(self, co):
#         self.mesh.vertices.foreach_set("co", co.ravel())
#         self.mesh.update()
#
#     # ---- Vertex groups ----
#
#     def get_locked_vertex_groups_array(self):
#         v_groups = self.vertex_groups
#         if not v_groups:
#             return None
#         return [not g.lock_weight for g in v_groups]
#
#     def get_vertex_groups_names(self, ignore_locked=False):
#         if not self.vertex_groups:
#             return None
#         group_names = [g.name for g in self.vertex_groups]
#         if ignore_locked:
#             filter_array = self.get_locked_vertex_groups_array()
#             return [n for n, ok in zip(group_names, filter_array) if ok]
#         return group_names
#
#     def get_vertex_group_weights(self, vertex_group_name):
#         v_group = self.vertex_groups.get(vertex_group_name)
#         if not v_group:
#             return None
#
#         v_count = len(self.mesh.vertices)
#         weights = np.zeros(v_count, dtype=np.float32)
#         for i, v in enumerate(self.mesh.vertices):
#             try:
#                 weights[i] = v_group.weight(i)
#             except RuntimeError:
#                 weights[i] = 0.0
#         weights.shape = (v_count, 1)
#         return weights
#
#     def set_vertex_group_weights(self, group_name, weights):
#         v_group = self.vertex_groups.get(group_name)
#         if not v_group:
#             v_group = self.vertex_groups.new(name=group_name)
#
#         w = weights.reshape((-1,))
#         for i, value in enumerate(w):
#             if value > 0.0:
#                 v_group.add([i], float(value), 'REPLACE')
#
#     # ---- Shape keys ----
#
#     def get_shape_keys_vert_pos(self, exclude_muted=False):
#         if not self.shape_keys:
#             return None
#         data = {}
#         for sk in self.shape_keys:
#             if sk.name == "Basis":
#                 continue
#             if exclude_muted and getattr(sk, "mute", False):
#                 continue
#             data[sk.name] = self.get_shape_key_vert_pos(sk.name)
#         return data
#
#     def get_shape_key_vert_pos(self, shape_key_name):
#         if not self.shape_keys:
#             return None
#         sk = self.shape_keys.get(shape_key_name)
#         if not sk:
#             return None
#         v_count = len(sk.data)
#         co = np.zeros(v_count * 3, dtype=np.float32)
#         sk.data.foreach_get("co", co)
#         co.shape = (v_count, 3)
#         return co
#
#     def set_position_as_shape_key(self, shape_key_name, co, activate=False):
#         if not self.shape_keys:
#             self.obj.shape_key_add(name="Basis", from_mix=False)
#
#         key_blocks = self.mesh.shape_keys.key_blocks
#         if shape_key_name in key_blocks:
#             sk = key_blocks[shape_key_name]
#         else:
#             sk = self.obj.shape_key_add(name=shape_key_name, from_mix=False)
#
#         sk.data.foreach_set("co", co.ravel())
#
#         if activate:
#             for k in key_blocks:
#                 k.value = 0.0
#             sk.value = 1.0
#
#     # ---- BVH / transfer mesh ----
#
#     def ensure_mesh_data(self):
#         """Build transfer_bmesh and BVH only once."""
#         if self.transfer_bmesh is not None and self.bvhtree is not None:
#             return
#
#         self.transfer_bmesh = bmesh.new()
#
#         if self.deformed:
#             depsgraph = bpy.context.evaluated_depsgraph_get()
#             obj_eval = self.obj.evaluated_get(depsgraph)
#             mesh_eval = obj_eval.to_mesh()
#             self.transfer_bmesh.from_mesh(mesh_eval)
#             obj_eval.to_mesh_clear()
#         else:
#             self.transfer_bmesh.from_mesh(self.mesh)
#
#         if self.world_space:
#             mw = self.obj.matrix_world
#             for v in self.transfer_bmesh.verts:
#                 v.co = mw @ v.co
#
#         self.transfer_bmesh.verts.ensure_lookup_table()
#         self.transfer_bmesh.faces.ensure_lookup_table()
#
#         # UV-space sampling: build a synthetic 2D mesh in XY from active UVs.
#         if self.uv_space:
#             uv_layer = self.mesh.uv_layers.active
#             if not uv_layer:
#                 raise RuntimeError("UV space sampling requires an active UV map on the source and target meshes.")
#
#             uv_data = uv_layer.data
#             uv_bm = bmesh.new()
#             self.vertex_map = {}
#
#             loop_count = len(self.mesh.loops)
#             uv_verts = [None] * loop_count
#             for li in range(loop_count):
#                 uv = uv_data[li].uv
#                 v = uv_bm.verts.new((uv.x, uv.y, 0.0))
#                 uv_verts[li] = v
#
#             uv_bm.verts.ensure_lookup_table()
#
#             for poly in self.mesh.polygons:
#                 loop_indices = range(poly.loop_start, poly.loop_start + poly.loop_total)
#                 try:
#                     face_verts = [uv_verts[li] for li in loop_indices]
#                     uv_bm.faces.new(face_verts)
#                 except ValueError:
#                     # Duplicate face (can happen with non-manifold UVs); skip
#                     pass
#
#             uv_bm.faces.ensure_lookup_table()
#             bmesh.ops.triangulate(uv_bm, faces=uv_bm.faces)
#
#             for li, loop in enumerate(self.mesh.loops):
#                 uv_vid = uv_verts[li].index
#                 self.vertex_map.setdefault(uv_vid, []).append(loop.vertex_index)
#
#             self.transfer_bmesh.free()
#             self.transfer_bmesh = uv_bm
#             self.transfer_bmesh.verts.ensure_lookup_table()
#             self.transfer_bmesh.faces.ensure_lookup_table()
#         else:
#             self.vertex_map = {i: [i] for i in range(len(self.transfer_bmesh.verts))}
#
#         if self.triangulate and not self.uv_space:
#             bmesh.ops.triangulate(self.transfer_bmesh, faces=self.transfer_bmesh.faces)
#
#         self.bvhtree = BVHTree.FromBMesh(self.transfer_bmesh)
#
#
# # -----------------------------------------------------------------------------
# # Transfer engine
# # -----------------------------------------------------------------------------
#
# class MeshDataTransfer:
#     def __init__(
#         self,
#         source,
#         target,
#         uv_space=False,
#         deformed_source=False,
#         deformed_target=False,
#         world_space=False,
#         search_method="RAYCAST",
#         vertex_group=None,
#         invert_vertex_group=False,
#         exclude_locked_groups=False,
#         exclude_muted_shapekeys=False,
#         snap_to_closest=False,
#         snap_to_closest_shape_key=False,
#         transfer_drivers=False,
#         source_arm=None,
#         target_arm=None,
#         restrict_to_selection=False,
#     ):
#         self.vertex_group = vertex_group
#         self.invert_vertex_group = invert_vertex_group
#         self.exclude_muted_shapekeys = exclude_muted_shapekeys
#         self.exclude_locked_groups = exclude_locked_groups
#         self.snap_to_closest = snap_to_closest
#         self.snap_to_closest_shapekey = snap_to_closest_shape_key
#         self.restrict_to_selection = restrict_to_selection
#
#         self.uv_space = bool(uv_space)
#         self.world_space = bool(world_space)
#
#         self.search_method = str(search_method)
#         if self.uv_space:
#             self.search_method = "CLOSEST"
#
#         self.source = MeshData(source, uv_space=self.uv_space, deformed=deformed_source, world_space=self.world_space)
#         self.target = MeshData(target, uv_space=self.uv_space, deformed=deformed_target, world_space=self.world_space)
#
#         self.transfer_drivers = bool(transfer_drivers)
#         self.source_arm = source_arm
#         self.target_arm = target_arm
#
#         self._projection_ready = False
#         self.missed_projections = None
#         self.ray_casted = None
#         self.hit_faces = None
#         self.related_ids = None
#         self.has_zero_area_faces = False
#         self.barycentric_coords = None
#
#     def free(self):
#         if self.target:
#             self.target.free()
#         if self.source:
#             self.source.free()
#
#     # ---- Lazy cache ----
#
#     def ensure_projection_cache(self):
#         if self.search_method == "TOPOLOGY":
#             return
#         if self._projection_ready:
#             return
#
#         self.source.ensure_mesh_data()
#         self.target.ensure_mesh_data()
#
#         self.cast_verts()
#         self.has_zero_area_faces = self.check_zero_area_triangles(self.hit_faces)
#         self.barycentric_coords = self.get_barycentric_coords(self.ray_casted, self.hit_faces)
#         self._projection_ready = True
#
#     # ---- Masks ----
#
#     def get_vertices_mask(self):
#         selection = None
#         if self.restrict_to_selection:
#             selection = self.target.get_selected_verts()
#
#         if self.vertex_group:
#             v_group = self.target.get_vertex_group_weights(self.vertex_group)
#             if v_group is None:
#                 return selection
#             if self.invert_vertex_group:
#                 v_group = 1.0 - v_group
#             if self.restrict_to_selection and selection is not None:
#                 v_group = v_group * selection
#             return v_group
#
#         return selection
#
#     # ---- Projection primitives ----
#
#     @staticmethod
#     def transform_vertices_array(array: np.ndarray, mat: np.ndarray) -> np.ndarray:
#         verts_co_4d = np.ones(shape=(array.shape[0], 4), dtype=np.float32)
#         verts_co_4d[:, :-1] = array
#         out = np.einsum("ij,aj->ai", mat, verts_co_4d)
#         return out[:, :3]
#
#     def snap_coords_to_source_verts(self, coords, source_coords):
#         source_size = len(self.source.mesh.vertices)
#         kd = kdtree.KDTree(source_size)
#         for i, co in enumerate(source_coords):
#             kd.insert(Vector(co), i)
#         kd.balance()
#
#         snapped_coords = coords.copy()
#         for i in range(len(coords)):
#             co = Vector(coords[i])
#             snapped = kd.find(co)
#             snapped_coords[i] = snapped[0]
#         return snapped_coords
#
#     def cast_verts(self):
#         self.target.transfer_bmesh.verts.ensure_lookup_table()
#         v_count = len(self.target.mesh.vertices)
#
#         self.ray_casted = np.zeros((v_count, 3), dtype=np.float32)
#         self.hit_faces = np.zeros((v_count, 3, 3), dtype=np.float32)
#         self.related_ids = np.zeros((v_count, 3), dtype=np.int64)
#
#         self.source.transfer_bmesh.faces.ensure_lookup_table()
#         self.missed_projections = np.ones((v_count, 3), dtype=bool)
#
#         v_normal = Vector((0.0, 0.0, 1.0))
#         for v in self.target.transfer_bmesh.verts:
#             v_ids = self.target.vertex_map.get(v.index, [v.index])
#
#             if self.search_method == "CLOSEST":
#                 projection = self.source.bvhtree.find_nearest(v.co)
#             else:
#                 if not self.uv_space:
#                     v_normal = v.normal
#                 projection = self.source.bvhtree.ray_cast(v.co, v_normal)
#                 if not projection[0]:
#                     projection = self.source.bvhtree.ray_cast(v.co, (v_normal * -1.0))
#
#             if projection[0]:
#                 face = self.source.transfer_bmesh.faces[projection[2]]
#                 tri = (face.verts[0].co, face.verts[1].co, face.verts[2].co)
#
#                 v1_id, v2_id, v3_id = face.verts[0].index, face.verts[1].index, face.verts[2].index
#                 v1_id = self.source.vertex_map[v1_id][0]
#                 v2_id = self.source.vertex_map[v2_id][0]
#                 v3_id = self.source.vertex_map[v3_id][0]
#                 v_array = np.array([v1_id, v2_id, v3_id], dtype=np.int64)
#
#                 for v_id in v_ids:
#                     self.ray_casted[v_id] = projection[0]
#                     self.missed_projections[v_id] = False
#                     self.hit_faces[v_id] = tri
#                     self.related_ids[v_id] = v_array
#             else:
#                 for v_id in v_ids:
#                     self.ray_casted[v_id] = v.co
#
#         return self.ray_casted, self.hit_faces, self.related_ids
#
#     @staticmethod
#     def check_zero_area_triangles(triangles: np.ndarray) -> bool:
#         v1 = triangles[:, 1] - triangles[:, 0]
#         v2 = triangles[:, 2] - triangles[:, 0]
#         area_vectors = np.cross(v1, v2)
#         area_magnitudes = np.linalg.norm(area_vectors, axis=1)
#         zero_area = np.isclose(area_magnitudes, 0)
#         return bool(zero_area.any())
#
#     @staticmethod
#     def get_barycentric_coords(points: np.ndarray, triangles: np.ndarray) -> np.ndarray:
#         v0 = triangles[:, 1] - triangles[:, 0]
#         v1 = triangles[:, 2] - triangles[:, 0]
#         v2 = points - triangles[:, 0]
#
#         d00 = np.einsum("ij,ij->i", v0, v0)
#         d01 = np.einsum("ij,ij->i", v0, v1)
#         d11 = np.einsum("ij,ij->i", v1, v1)
#         d20 = np.einsum("ij,ij->i", v2, v0)
#         d21 = np.einsum("ij,ij->i", v2, v1)
#
#         denom = (d00 * d11 - d01 * d01)
#         v = (d11 * d20 - d01 * d21) / denom
#         w = (d00 * d21 - d01 * d20) / denom
#         u = 1.0 - v - w
#
#         bary = np.stack((u, v, w), axis=1).astype(np.float32)
#         return bary
#
#     @staticmethod
#     def calculate_barycentric_location(tri_points: np.ndarray, bary_coords: np.ndarray) -> np.ndarray:
#         return (
#             tri_points[:, 0] * bary_coords[:, [0]] +
#             tri_points[:, 1] * bary_coords[:, [1]] +
#             tri_points[:, 2] * bary_coords[:, [2]]
#         )
#
#     def get_transferred_vert_coords(self, transfer_coord: np.ndarray) -> np.ndarray:
#         indexes = self.related_ids.ravel()
#         sorted_coords = transfer_coord[indexes]
#         sorted_coords.shape = self.hit_faces.shape
#         transferred = self.calculate_barycentric_location(sorted_coords, self.barycentric_coords)
#         return transferred
#
#     # ---- Transfer operations ----
#
#     def _topology_validate(self):
#         if self.source.v_count != self.target.v_count:
#             raise RuntimeError(
#                 f"Topology transfer requires identical vertex counts. "
#                 f"Source: {self.source.v_count}, Target: {self.target.v_count}"
#             )
#
#     def transfer_vertex_position(self, as_shape_key=False, shape_key_name="Transferred Position"):
#         undeformed_verts = self.target.get_verts_position()
#         base_coords = self.source.get_verts_position()
#
#         if self.search_method == "TOPOLOGY":
#             self._topology_validate()
#             transferred_position = base_coords.copy()
#         else:
#             self.ensure_projection_cache()
#             transferred_position = self.get_transferred_vert_coords(base_coords)
#             if self.has_zero_area_faces:
#                 transferred_position = np.where(np.isnan(transferred_position), undeformed_verts, transferred_position)
#
#         if self.world_space:
#             mat = np.array(self.target.obj.matrix_world.inverted()) @ np.array(self.source.obj.matrix_world)
#             transferred_position = self.transform_vertices_array(transferred_position, mat)
#
#         transferred_position = np.where(self.missed_projections, undeformed_verts, transferred_position)
#
#         masked_vertices = self.get_vertices_mask()
#         if masked_vertices is not None:
#             inverted_mask = 1.0 - masked_vertices
#             transferred_position = transferred_position * masked_vertices + undeformed_verts * inverted_mask
#
#         if self.snap_to_closest:
#             transferred_position = self.snap_coords_to_source_verts(transferred_position, base_coords)
#
#         if as_shape_key:
#             self.target.set_position_as_shape_key(shape_key_name=shape_key_name, co=transferred_position, activate=True)
#         else:
#             self.target.set_verts_position(transferred_position)
#
#         return True
#
#     def transfer_vertex_groups(self):
#         source_groups = self.source.get_vertex_groups_names(ignore_locked=self.exclude_locked_groups)
#         if not source_groups:
#             return False
#
#         if self.search_method == "TOPOLOGY":
#             self._topology_validate()
#
#         masked_vertices = self.get_vertices_mask()
#         for group_name in source_groups:
#             weights = self.source.get_vertex_group_weights(group_name)
#             if weights is None:
#                 continue
#
#             if self.search_method == "TOPOLOGY":
#                 transferred = weights.copy()
#             else:
#                 self.ensure_projection_cache()
#                 wcoord = np.zeros((len(weights), 3), dtype=np.float32)
#                 wcoord[:, 0] = weights[:, 0]
#                 transferred_coord = self.get_transferred_vert_coords(wcoord)
#                 transferred = transferred_coord[:, [0]]
#
#             if masked_vertices is not None:
#                 inverted_mask = 1.0 - masked_vertices
#                 transferred = transferred * masked_vertices + (weights if self.search_method == "TOPOLOGY" else 0.0) * inverted_mask
#
#             self.target.set_vertex_group_weights(group_name, transferred)
#
#         return True
#
#     def transfer_shape_keys(self):
#         shape_keys = self.source.get_shape_keys_vert_pos(exclude_muted=self.exclude_muted_shapekeys)
#         if not shape_keys:
#             return False
#
#         if self.search_method == "TOPOLOGY":
#             self._topology_validate()
#         else:
#             self.ensure_projection_cache()
#
#         undeformed_verts = self.target.get_verts_position()
#         base_coords = self.source.get_verts_position()
#
#         if self.search_method == "TOPOLOGY":
#             base_transferred_position = base_coords.copy()
#         else:
#             base_transferred_position = self.get_transferred_vert_coords(base_coords)
#             if self.has_zero_area_faces:
#                 base_transferred_position = np.where(np.isnan(base_transferred_position), undeformed_verts, base_transferred_position)
#
#         if self.world_space:
#             mat = np.array(self.target.obj.matrix_world.inverted()) @ np.array(self.source.obj.matrix_world)
#             base_transferred_position = self.transform_vertices_array(base_transferred_position, mat)
#
#         base_transferred_position = np.where(self.missed_projections, undeformed_verts, base_transferred_position)
#
#         masked_vertices = self.get_vertices_mask()
#         target_shape_keys = self.target.get_shape_keys_vert_pos() or {}
#
#         for sk_name, sk_points in shape_keys.items():
#             slider_min = self.source.shape_keys[sk_name].slider_min
#             slider_max = self.source.shape_keys[sk_name].slider_max
#
#             sk_points_work = sk_points
#             if self.world_space:
#                 mat = np.array(self.source.obj.matrix_world)
#                 sk_points_work = self.transform_vertices_array(sk_points_work, mat)
#
#             if self.search_method == "TOPOLOGY":
#                 transferred_sk = sk_points_work.copy()
#             else:
#                 transferred_sk = self.get_transferred_vert_coords(sk_points_work)
#
#             if self.snap_to_closest_shapekey:
#                 transferred_sk = self.snap_coords_to_source_verts(transferred_sk, sk_points_work)
#
#             if self.world_space:
#                 mat = np.array(self.target.obj.matrix_world.inverted())
#                 transferred_sk = self.transform_vertices_array(transferred_sk, mat)
#
#             transferred_sk = np.where(self.missed_projections, undeformed_verts, transferred_sk)
#
#             if self.has_zero_area_faces:
#                 transferred_sk = np.where(np.isnan(transferred_sk), undeformed_verts, transferred_sk)
#
#             # Blend with target existing SK if present
#             if sk_name in target_shape_keys:
#                 existing = target_shape_keys[sk_name]
#                 transferred_sk = existing + (transferred_sk - base_transferred_position)
#
#             if masked_vertices is not None:
#                 inverted_mask = 1.0 - masked_vertices
#                 transferred_sk = transferred_sk * masked_vertices + undeformed_verts * inverted_mask
#
#             self.target.set_position_as_shape_key(shape_key_name=sk_name, co=transferred_sk, activate=False)
#
#             try:
#                 self.target.shape_keys[sk_name].slider_min = slider_min
#                 self.target.shape_keys[sk_name].slider_max = slider_max
#             except Exception:
#                 pass
#
#         if self.transfer_drivers:
#             self.transfer_shape_keys_drivers()
#
#         return True
#
#     def transfer_shape_keys_drivers(self):
#         if not self.source.shape_keys_drivers:
#             return False
#         if not _is_armature_object(self.source_arm) or not _is_armature_object(self.target_arm):
#             return False
#
#         src_drivers = self.source.shape_keys_drivers
#         if not src_drivers:
#             return False
#
#         if not self.target.mesh.shape_keys:
#             self.target.obj.shape_key_add(name="Basis", from_mix=False)
#
#         for d in src_drivers:
#             try:
#                 dst_anim = self.target.mesh.shape_keys.animation_data
#                 if not dst_anim:
#                     self.target.mesh.shape_keys.animation_data_create()
#                     dst_anim = self.target.mesh.shape_keys.animation_data
#
#                 new_fcurve = dst_anim.drivers.new(data_path=d.data_path, index=d.array_index)
#
#                 new_drv = new_fcurve.driver
#                 new_drv.type = d.driver.type
#                 new_drv.expression = d.driver.expression
#
#                 while new_drv.variables:
#                     new_drv.variables.remove(new_drv.variables[0])
#
#                 for var in d.driver.variables:
#                     nv = new_drv.variables.new()
#                     nv.name = var.name
#                     nv.type = var.type
#                     for t_i, t in enumerate(var.targets):
#                         nt = nv.targets[t_i]
#                         nt.id_type = t.id_type
#                         nt.transform_type = t.transform_type
#                         nt.transform_space = t.transform_space
#                         nt.bone_target = t.bone_target
#                         nt.data_path = t.data_path
#
#                         if t.id == self.source_arm:
#                             nt.id = self.target_arm
#                         elif t.id == self.source.mesh.shape_keys:
#                             nt.id = self.target.mesh.shape_keys
#                         else:
#                             nt.id = t.id
#
#                 for m in d.modifiers:
#                     nm = new_fcurve.modifiers.new(type=m.type)
#                     for attr in dir(m):
#                         if attr.startswith("_"):
#                             continue
#                         if attr in {"bl_rna", "rna_type", "type"}:
#                             continue
#                         try:
#                             setattr(nm, attr, getattr(m, attr))
#                         except Exception:
#                             pass
#             except Exception:
#                 continue
#
#         return True
#
#     def transfer_uvs(self):
#         """
#         Transfer UVs using Data Transfer modifier.
#         This path does NOT require BVH/projection cache.
#         """
#         current_active = bpy.context.view_layer.objects.active
#         current_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
#
#         try:
#             _safe_mode_set("OBJECT")
#             _set_active_object(self.target.obj)
#
#             source_seams = self.source.seam_edges
#             self.mark_seam_islands(self.source.obj)
#
#             transfer_source = self.source.obj
#             transfer_target = self.target.obj
#
#             if self.search_method == "CLOSEST":
#                 loop_mapping = 'POLYINTERP_NEAREST'
#                 poly_mapping = 'NEAREST'
#                 edge_mapping = "EDGEINTERP_VNORPROJ"
#             elif self.search_method == "RAYCAST":
#                 loop_mapping = 'POLYINTERP_LNORPROJ'
#                 poly_mapping = 'POLYINTERP_PNORPROJ'
#                 edge_mapping = "EDGEINTERP_VNORPROJ"
#             else:  # TOPOLOGY
#                 loop_mapping = "TOPOLOGY"
#                 poly_mapping = "TOPOLOGY"
#                 edge_mapping = "TOPOLOGY"
#
#             data_transfer = transfer_target.modifiers.new(name="Data Transfer", type="DATA_TRANSFER")
#             data_transfer.use_object_transform = self.world_space
#             data_transfer.data_types_edges = {"SEAM"}
#             data_transfer.object = transfer_source
#             data_transfer.use_loop_data = True
#             data_transfer.use_edge_data = True
#             data_transfer.edge_mapping = edge_mapping
#             data_transfer.loop_mapping = loop_mapping
#             data_transfer.data_types_loops = {"UV"}
#             data_transfer.use_poly_data = True
#
#             source_active_uv = transfer_source.data.uv_layers.active
#             dest_active_uv = transfer_target.data.uv_layers.active
#             if not source_active_uv or not dest_active_uv:
#                 raise RuntimeError("UV transfer requires an active UV map on both source and target.")
#
#             data_transfer.layers_uv_select_src = source_active_uv.name
#             data_transfer.layers_uv_select_dst = dest_active_uv.name
#             data_transfer.poly_mapping = poly_mapping
#
#             temp_group_name = None
#             if self.vertex_group or self.restrict_to_selection:
#                 masked_vertex = self.get_vertices_mask()
#                 if masked_vertex is not None:
#                     mask_v_group = transfer_target.vertex_groups.new()
#                     temp_group_name = mask_v_group.name
#                     self.target.set_vertex_group_weights(temp_group_name, masked_vertex)
#                     data_transfer.vertex_group = temp_group_name
#
#             bpy.ops.object.datalayout_transfer(modifier=data_transfer.name)
#             bpy.ops.object.modifier_apply(modifier=data_transfer.name)
#
#             self.source.seam_edges = source_seams
#
#             if temp_group_name:
#                 vg = transfer_target.vertex_groups.get(temp_group_name)
#                 if vg:
#                     transfer_target.vertex_groups.remove(vg)
#
#             return True
#         finally:
#             if current_active:
#                 _set_active_object(current_active)
#             _safe_mode_set(current_mode)
#
#     @staticmethod
#     def mark_seam_islands(obj):
#         """Mark seams from UV islands (best-effort), restoring context/state."""
#         current_active = bpy.context.view_layer.objects.active
#         current_mode = bpy.context.object.mode if bpy.context.object else "OBJECT"
#         scene = bpy.context.scene
#
#         try:
#             _safe_mode_set("OBJECT")
#             _set_active_object(obj)
#             _safe_mode_set("EDIT")
#
#             uv_sync_state = scene.tool_settings.use_uv_select_sync
#             scene.tool_settings.use_uv_select_sync = True
#
#             mesh = obj.data
#             verts = mesh.vertices
#             edges = mesh.edges
#
#             current_selection = [False] * len(verts)
#             verts.foreach_get("select", current_selection)
#
#             edges.foreach_set("select", [True] * len(edges))
#             bpy.ops.uv.mark_seam(clear=True)
#             bpy.ops.uv.seams_from_islands(mark_seams=True, mark_sharp=False)
#
#             verts.foreach_set("select", current_selection)
#             scene.tool_settings.use_uv_select_sync = uv_sync_state
#         finally:
#             if current_active:
#                 _set_active_object(current_active)
#             _safe_mode_set(current_mode)
#
#
# # -----------------------------------------------------------------------------
# # Settings
# # -----------------------------------------------------------------------------
#
# def _update_search_method(self, context):
#     obj = context.object
#     if not obj:
#         return
#     p = obj.skv_mesh_data_transfer
#     # Prevent UV-space search method when transferring UVs via Data Transfer.
#     if p.search_method == "UVS" and p.attributes_to_transfer == "UVS":
#         p.search_method = "CLOSEST"
#
#
# class SKV_MeshDataSettings(PropertyGroup):
#     mesh_object_space: EnumProperty(
#         items=[('WORLD', 'World', '', 1), ('LOCAL', 'Local', '', 2)],
#         name="Object Space",
#         default='LOCAL',
#     )
#
#     search_method: EnumProperty(
#         items=[
#             ('CLOSEST', 'Closest', "Closest Point on Surface", 1),
#             ('RAYCAST', 'Raycast', "Raycast along vertex normal", 2),
#             ('TOPOLOGY', 'Topology', "Match by vertex index (requires identical topology)", 3),
#             ('UVS', 'UVs (Active UV)', "Sample transfer in UV space (active UV)", 4),
#         ],
#         name="Search Method",
#         default='CLOSEST',
#         update=_update_search_method,
#     )
#
#     attributes_to_transfer: EnumProperty(
#         items=[
#             ("SHAPE", "Vertex Position", "", 1),
#             ("UVS", "UVs", "", 2),
#             ("SHAPE_KEYS", "Shape Keys", "", 3),
#             ("VERTEX_GROUPS", "Vertex Groups", "", 4),
#         ],
#         name="Transfer",
#         default="SHAPE_KEYS",
#         update=_update_search_method,
#     )
#
#     mesh_source: PointerProperty(
#         name="Source",
#         type=bpy.types.Object,
#         poll=_mesh_poll,
#     )
#
#     arm_source: PointerProperty(
#         name="Source Armature",
#         type=bpy.types.Object,
#         poll=_arm_poll,
#     )
#
#     arm_target: PointerProperty(
#         name="Target Armature",
#         type=bpy.types.Object,
#         poll=_arm_poll,
#     )
#
#     vertex_group_filter: StringProperty(
#         name="Vertex Group Filter",
#         default="",
#     )
#
#     invert_vertex_group_filter: BoolProperty(
#         name="Invert Filter",
#         default=False,
#     )
#
#     restrict_to_edit_selection: BoolProperty(
#         name="Restrict to Selection",
#         default=False,
#     )
#
#     transfer_shape_as_key: BoolProperty(
#         name="As Shape Key",
#         default=False,
#     )
#
#     transfer_modified_source: BoolProperty(
#         name="Use Deformed Source",
#         default=False,
#     )
#
#     transfer_modified_target: BoolProperty(
#         name="Use Deformed Target",
#         default=False,
#     )
#
#     exclude_muted_shapekeys: BoolProperty(
#         name="Exclude Muted Shape Keys",
#         default=False,
#     )
#
#     exclude_locked_groups: BoolProperty(
#         name="Exclude Locked Groups",
#         default=False,
#     )
#
#     snap_to_closest: BoolProperty(
#         name="Snap to Closest Vertex",
#         default=False,
#     )
#
#     snap_to_closest_shapekey: BoolProperty(
#         name="Snap Shape Keys to Closest",
#         default=False,
#     )
#
#     transfer_shapekeys_drivers: BoolProperty(
#         name="Transfer Shape Key Drivers",
#         default=False,
#     )
#
#
# # -----------------------------------------------------------------------------
# # Operators (namespaced)
# # -----------------------------------------------------------------------------
#
# class SKV_OT_TransferMeshData(Operator):
#     bl_idname = "skv.transfer_mesh_data"
#     bl_label = "Transfer Mesh Data"
#     bl_options = {'REGISTER', 'UNDO'}
#
#     @classmethod
#     def poll(cls, context):
#         obj = context.active_object
#         if not _is_mesh_object(obj):
#             return False
#         if bpy.context.object.mode != "OBJECT":
#             return False
#         p = getattr(obj, "skv_mesh_data_transfer", None)
#         return p is not None and p.mesh_source is not None
#
#     def execute(self, context):
#         active = context.active_object
#         p = active.skv_mesh_data_transfer
#
#         source = p.mesh_source
#         if not _is_mesh_object(source):
#             self.report({'ERROR'}, "Invalid source object (must be Mesh).")
#             return {'CANCELLED'}
#
#         world_space = (p.mesh_object_space == 'WORLD')
#         search_method = p.search_method
#
#         uv_space = (search_method == 'UVS')
#         deformed_source = p.transfer_modified_source
#         deformed_target = p.transfer_modified_target
#
#         mask_vertex_group = p.vertex_group_filter if p.vertex_group_filter else None
#         invert_mask = p.invert_vertex_group_filter
#         restrict_to_selection = p.restrict_to_edit_selection
#
#         exclude_muted_shapekeys = p.exclude_muted_shapekeys
#         exclude_locked_groups = p.exclude_locked_groups
#         snap_to_closest = p.snap_to_closest
#         snap_to_closest_shape_key = p.snap_to_closest_shapekey
#         transfer_drivers = p.transfer_shapekeys_drivers
#
#         source_arm = p.arm_source
#         target_arm = p.arm_target
#
#         transfer_data = MeshDataTransfer(
#             source=source,
#             target=active,
#             world_space=world_space,
#             uv_space=uv_space,
#             deformed_source=deformed_source,
#             deformed_target=deformed_target,
#             invert_vertex_group=invert_mask,
#             search_method=search_method,
#             vertex_group=mask_vertex_group,
#             exclude_muted_shapekeys=exclude_muted_shapekeys,
#             exclude_locked_groups=exclude_locked_groups,
#             snap_to_closest=snap_to_closest,
#             snap_to_closest_shape_key=snap_to_closest_shape_key,
#             transfer_drivers=transfer_drivers,
#             source_arm=source_arm,
#             target_arm=target_arm,
#             restrict_to_selection=restrict_to_selection,
#         )
#
#         try:
#             if p.attributes_to_transfer == "SHAPE":
#                 transfer_data.transfer_vertex_position(as_shape_key=p.transfer_shape_as_key)
#             elif p.attributes_to_transfer == "UVS":
#                 transfer_data.transfer_uvs()
#             elif p.attributes_to_transfer == "SHAPE_KEYS":
#                 transfer_data.transfer_shape_keys()
#             elif p.attributes_to_transfer == "VERTEX_GROUPS":
#                 transfer_data.transfer_vertex_groups()
#         except RuntimeError as e:
#             self.report({'ERROR'}, str(e))
#             return {'CANCELLED'}
#         finally:
#             transfer_data.free()
#
#         return {'FINISHED'}
#
#
# class SKV_OT_TransferShapeKeyDrivers(Operator):
#     bl_idname = "skv.transfer_shape_key_drivers"
#     bl_label = "Transfer Shape Key Drivers"
#     bl_options = {'REGISTER', 'UNDO'}
#
#     @classmethod
#     def poll(cls, context):
#         obj = context.active_object
#         if not _is_mesh_object(obj):
#             return False
#         if bpy.context.object.mode != "OBJECT":
#             return False
#         p = getattr(obj, "skv_mesh_data_transfer", None)
#         if p is None:
#             return False
#         return p.mesh_source is not None and p.arm_source is not None and p.arm_target is not None
#
#     def execute(self, context):
#         active = context.active_object
#         p = active.skv_mesh_data_transfer
#
#         source = p.mesh_source
#         world_space = (p.mesh_object_space == 'WORLD')
#         search_method = p.search_method
#         uv_space = (search_method == 'UVS')
#
#         transfer_data = MeshDataTransfer(
#             source=source,
#             target=active,
#             world_space=world_space,
#             uv_space=uv_space,
#             deformed_source=p.transfer_modified_source,
#             deformed_target=False,
#             search_method=search_method,
#             vertex_group=p.vertex_group_filter if p.vertex_group_filter else None,
#             invert_vertex_group=p.invert_vertex_group_filter,
#             exclude_muted_shapekeys=p.exclude_muted_shapekeys,
#             snap_to_closest_shape_key=p.snap_to_closest_shapekey,
#             transfer_drivers=True,
#             source_arm=p.arm_source,
#             target_arm=p.arm_target,
#             restrict_to_selection=p.restrict_to_edit_selection,
#         )
#
#         try:
#             ok = transfer_data.transfer_shape_keys_drivers()
#             if not ok:
#                 self.report({'WARNING'}, "No drivers transferred (missing drivers or invalid armatures).")
#         finally:
#             transfer_data.free()
#
#         return {'FINISHED'}
#
#
# # -----------------------------------------------------------------------------
# # UI integration into SKV sidebar panel
# # -----------------------------------------------------------------------------
#
# def draw_transfer_ui(layout, context):
#     obj = context.active_object
#     if not _is_mesh_object(obj):
#         layout.label(text="Active object is not a Mesh", icon="ERROR")
#         return
#
#     p = getattr(obj, "skv_mesh_data_transfer", None)
#     if p is None:
#         layout.label(text="Transfer settings not available", icon="ERROR")
#         return
#
#     col = layout.column(align=True)
#     col.prop(p, "mesh_source")
#     col.prop(p, "mesh_object_space", expand=True)
#
#     col.separator()
#     col.prop(p, "attributes_to_transfer", text="")
#     col.separator()
#     col.prop(p, "search_method", text="")
#
#     col.separator()
#     box = col.box()
#     box.label(text="Mask / Selection")
#     box.prop(p, "vertex_group_filter", text="Vertex Group")
#     row = box.row(align=True)
#     row.prop(p, "invert_vertex_group_filter")
#     row.prop(p, "restrict_to_edit_selection")
#
#     col.separator()
#     box2 = col.box()
#     box2.label(text="Options")
#     box2.prop(p, "transfer_modified_source")
#     box2.prop(p, "transfer_modified_target")
#     box2.prop(p, "exclude_muted_shapekeys")
#     box2.prop(p, "exclude_locked_groups")
#     box2.prop(p, "snap_to_closest")
#     box2.prop(p, "snap_to_closest_shapekey")
#
#     if p.attributes_to_transfer == "SHAPE":
#         box2.prop(p, "transfer_shape_as_key")
#     if p.attributes_to_transfer == "SHAPE_KEYS":
#         box2.prop(p, "transfer_shapekeys_drivers")
#
#     col.separator()
#     col.operator("skv.transfer_mesh_data", icon="MOD_DATA_TRANSFER")
#
#     col.separator()
#     rig = col.box()
#     rig.label(text="Rigging Helpers")
#     rig.prop(p, "arm_source")
#     rig.prop(p, "arm_target")
#     rig.operator("skv.transfer_shape_key_drivers", icon="DRIVER")
#
#
# # -----------------------------------------------------------------------------
# # Registration (hooked by main addon)
# # -----------------------------------------------------------------------------
#
# CLASSES = (
#     SKV_MeshDataSettings,
#     SKV_OT_TransferMeshData,
#     SKV_OT_TransferShapeKeyDrivers,
# )
