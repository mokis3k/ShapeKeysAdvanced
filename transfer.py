# ─────────────────────────────────────────────────────────────────────────────
# transfer.py - Shape Key Transfer
#
# Public API (used by other addon modules):
#   Transfer               - transfer engine class (used in groups.py)
#   SKV_TransferSettings   - PropertyGroup stored on bpy.types.Object
#   CLASSES                - tuple of Blender classes to register
#
# Everything else in this file is a private implementation detail.
# ─────────────────────────────────────────────────────────────────────────────

import bpy
import bmesh
from bpy.types import PropertyGroup
from bpy.props import StringProperty
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import barycentric_transform


# ══════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS - validation
# ══════════════════════════════════════════════════════════════════════════════

def _is_valid_mesh_object(obj):
    # Object must exist, be a MESH, and have mesh data.
    return obj is not None and getattr(obj, "type", None) == "MESH" and obj.data is not None


def _is_target_data_editable(obj):
    # Reject linked (library) mesh or shape key datablocks.
    # Writing into linked data would silently fail or corrupt the library override.
    mesh = obj.data
    if mesh is None:
        return False
    if getattr(mesh, "library", None) is not None:
        return False
    if mesh.shape_keys is not None and getattr(mesh.shape_keys, "library", None) is not None:
        return False
    return True


def _clamp01(value):
    return max(0.0, min(1.0, value))


# ══════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS - geometry
# ══════════════════════════════════════════════════════════════════════════════

def _get_basis_coords_local(obj):
    # Local-space coordinates of the neutral (Basis) shape, independent of
    # the currently active shape key. Falls back to mesh.vertices if there
    # are no shape keys.
    mesh = obj.data
    if mesh.shape_keys and len(mesh.shape_keys.key_blocks) > 0:
        return [Vector(d.co) for d in mesh.shape_keys.key_blocks[0].data]
    return [v.co.copy() for v in mesh.vertices]


def _get_sk_coords_local(obj, sk_name):
    # Local-space coordinates of a named shape key on the object.
    return [Vector(d.co) for d in obj.data.shape_keys.key_blocks[sk_name].data]


def _to_world(matrix_world, coords_local):
    return [matrix_world @ co for co in coords_local]


def _to_local(matrix_world_inv, coords_world):
    return [matrix_world_inv @ co for co in coords_world]


_DEGENERATE_EPS = 1e-10


def _is_degenerate_tri(a, b, c):
    # True if the triangle has zero (or near-zero) area.
    return (b - a).cross(c - a).length_squared < _DEGENERATE_EPS


def _build_source_surface(obj, basis_world_coords):
    """
    Build a triangulated copy of the source surface in WORLD space and a
    BVH tree for it. Degenerate (zero-area) triangles are excluded at
    construction time rather than being handled as a per-vertex fallback,
    so a query never lands on a degenerate triangle in the first place.

    bmesh.ops.triangulate does not add new vertices, so triangle vertex
    indices match the indices used in shape key data.

    Returns:
        bvh         - BVHTree for nearest-point queries (None if no usable
                      triangles exist)
        triangles   - list[(i0, i1, i2)] vertex indices per (non-degenerate)
                      triangle
        degenerate_count - number of triangles excluded for being degenerate
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()

        if len(bm.faces) == 0:
            return None, [], 0

        # Apply world-space Basis coordinates, regardless of the object's
        # current active shape key.
        for i, v in enumerate(bm.verts):
            v.co = basis_world_coords[i]

        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        if len(bm.faces) == 0:
            return None, [], 0

        verts_co = [v.co.copy() for v in bm.verts]

        triangles = []
        degenerate_count = 0
        for f in bm.faces:
            idxs = tuple(fv.index for fv in f.verts)
            if len(idxs) != 3:
                continue
            a, b, c = verts_co[idxs[0]], verts_co[idxs[1]], verts_co[idxs[2]]
            if _is_degenerate_tri(a, b, c):
                degenerate_count += 1
                continue
            triangles.append(idxs)

        if not triangles:
            return None, [], degenerate_count

        bvh = BVHTree.FromPolygons(verts_co, triangles)
    finally:
        bm.free()

    return bvh, triangles, degenerate_count


def _build_projection_cache(target_basis_world, bvh, triangles):
    """
    Project every target vertex (world space) onto the source surface
    (also world space). Built once and reused for every shape key.

    Returns:
        cache             - list of dicts:
            {'ok': True,  'proj': Vector, 'tri': (i0, i1, i2)}  - success
            {'ok': False}                                         - fallback
        projection_failed - count of vertices with no nearest point found
    """
    cache = []
    projection_failed = 0
    for co in target_basis_world:
        loc, _normal, tri_idx, _dist = bvh.find_nearest(co)
        if loc is None or tri_idx is None:
            cache.append({'ok': False})
            projection_failed += 1
        else:
            cache.append({
                'ok':   True,
                'proj': loc.copy(),
                'tri':  triangles[tri_idx],
            })
    return cache, projection_failed


def _compute_new_sk_coords(cache, src_basis_world, src_sk_world,
                           tgt_basis_world, weights, existing_world):
    """
    Compute new WORLD-space shape key coordinates for every target vertex.
    All inputs and outputs are in world space; the caller converts the
    result back to the target's local space.

    For vertex i:
        proj    = nearest point on the source Basis surface (world space)
        sk_proj = barycentric_transform(proj, basis_tri -> sk_tri)
        delta   = sk_proj - proj                  (deformation, world units)
        new_co  = tgt_basis_world[i] + delta * weight

    Args:
        cache            - result of _build_projection_cache
        src_basis_world  - source Basis coordinates, world space
        src_sk_world     - source shape key coordinates, world space
        tgt_basis_world  - target Basis coordinates, world space
        weights          - mask weights per target vertex (None = full
                           transfer everywhere)
        existing_world   - current world-space coordinates of the target
                           shape key, if it already existed (None -> falls
                           back to tgt_basis_world outside the mask)

    Returns:
        result            - list[Vector], new vertex positions (world space)
        barycentric_failed - count of vertices where the barycentric
                             transform could not be computed (should be
                             rare/zero, since degenerate triangles are
                             already excluded from the BVH)
    """
    result = []
    barycentric_failed = 0

    for i, rec in enumerate(cache):
        tgt_co = tgt_basis_world[i]
        w = weights[i] if weights is not None else 1.0

        # Reference position outside the mask area:
        #   existing shape key  -> its previous (world-space) coordinates
        #   new shape key       -> target base shape
        ref_co = existing_world[i] if existing_world is not None else tgt_co

        # No projection found, or vertex fully outside the mask -> fallback.
        if not rec['ok'] or w == 0.0:
            result.append(ref_co.copy())
            continue

        proj = rec['proj']
        i0, i1, i2 = rec['tri']

        ba = src_basis_world[i0]; bb = src_basis_world[i1]; bc = src_basis_world[i2]
        sa = src_sk_world[i0];    sb = src_sk_world[i1];    sc = src_sk_world[i2]

        # Defensive check only: the source surface build already excludes
        # degenerate basis triangles, so this should normally never trigger.
        if _is_degenerate_tri(ba, bb, bc):
            barycentric_failed += 1
            result.append(ref_co.copy())
            continue

        try:
            sk_proj = barycentric_transform(proj, ba, bb, bc, sa, sb, sc)
        except Exception:
            barycentric_failed += 1
            result.append(ref_co.copy())
            continue

        delta             = sk_proj - proj
        fully_transferred = tgt_co + delta

        if w >= 1.0:
            result.append(fully_transferred)
        else:
            result.append(ref_co.lerp(fully_transferred, w))

    return result, barycentric_failed


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC TRANSFER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class Transfer:
    """
    Transfers shape keys from one Mesh object to another using a spatial
    surface correspondence (BVH nearest point + barycentric coordinates).

    All projection and deformation math runs in WORLD space: source and
    target geometry is transformed by their respective matrix_world before
    matching, and results are converted back to the target's local space
    before being written. This keeps the result correct even when source
    and target have different location / rotation / scale.

    Usage:

        t  = Transfer(source=source_obj, target=target_obj, vertex_group=None)
        ok = t.transfer_shape_keys(shapekey_names=['smile', 'blink'])
        print(t.last_stats)   # diagnostics for the call above
        t.free()              # optional, releases cached BVH / projection data

    The BVH tree and projection cache are built once, on the first call to
    transfer_shape_keys(), and reused on subsequent calls on the same
    instance (note: this means the instance snapshots source/target
    transforms and Basis geometry at that point; recreate the Transfer if
    either object's transform or base mesh changes between calls).

    After each transfer_shape_keys() call, self.last_stats holds counters
    useful for diagnosing "missing" vertices:
        transferred_keys     - shape keys successfully written
        skipped_keys         - shape keys skipped (data mismatch / write failure)
        projection_failed    - target vertices with no nearest source point
        degenerate_triangles - source triangles excluded as zero-area
        barycentric_failed   - vertices where barycentric transform failed
                               (defensive case, normally 0)
        masked_vertices      - target vertices with mask weight == 0

    Note on performance: this implementation uses plain Python loops and
    mathutils.Vector rather than numpy. This is fine for typical character
    meshes and a handful of shape keys, but transfer time scales with
    vertex count x shape key count, so very dense meshes with many shape
    keys will be noticeably slower than a vectorized implementation.
    """

    def __init__(self, source, target, vertex_group=None):
        """
        source        - Blender Object (Mesh) to read shape keys from
        target        - Blender Object (Mesh) to write shape keys to
        vertex_group  - name of a vertex group on target used as a transfer
                        mask (str or None)
        """
        self._source  = source
        self._target  = target
        self._vg_name = vertex_group or ""

        # Heavy resources, built lazily by _prepare().
        self._bvh             = None
        self._triangles        = None
        self._src_mat          = None
        self._tgt_mat          = None
        self._tgt_mat_inv      = None
        self._src_basis_world  = None
        self._tgt_basis_world  = None
        self._cache            = None
        self._weights          = None
        self._base_stats       = None
        self._ready            = False

        self.last_stats = {}

    # ── Private ──────────────────────────────────────────────────────────

    def _prepare(self):
        # Build the BVH tree and projection cache in world space.
        # No-op if already prepared.
        if self._ready:
            return True

        source = self._source
        target = self._target

        if not _is_valid_mesh_object(source):
            return False
        if not _is_valid_mesh_object(target):
            return False

        if not source.data.shape_keys or len(source.data.shape_keys.key_blocks) < 2:
            return False

        if len(source.data.polygons) == 0:
            return False

        if len(target.data.vertices) == 0:
            return False

        if not _is_target_data_editable(target):
            return False

        src_basis_local = _get_basis_coords_local(source)
        tgt_basis_local = _get_basis_coords_local(target)

        # Guard against corrupted/non-standard basis data.
        if len(src_basis_local) != len(source.data.vertices):
            return False
        if len(tgt_basis_local) != len(target.data.vertices):
            return False

        # Snapshot world matrices once; reused for every shape key.
        src_mat     = source.matrix_world.copy()
        tgt_mat     = target.matrix_world.copy()
        tgt_mat_inv = tgt_mat.inverted_safe()

        src_basis_world = _to_world(src_mat, src_basis_local)
        tgt_basis_world = _to_world(tgt_mat, tgt_basis_local)

        bvh, triangles, degenerate_count = _build_source_surface(source, src_basis_world)
        if bvh is None or not triangles:
            return False

        cache, projection_failed = _build_projection_cache(tgt_basis_world, bvh, triangles)

        weights = None
        masked_vertices = 0
        if self._vg_name:
            vg = target.vertex_groups.get(self._vg_name)
            if vg is not None:
                weights = []
                for idx in range(len(tgt_basis_world)):
                    try:
                        w = vg.weight(idx)
                    except RuntimeError:
                        w = 0.0
                    w = _clamp01(w)
                    weights.append(w)
                    if w == 0.0:
                        masked_vertices += 1

        self._src_mat         = src_mat
        self._tgt_mat         = tgt_mat
        self._tgt_mat_inv     = tgt_mat_inv
        self._src_basis_world = src_basis_world
        self._tgt_basis_world = tgt_basis_world
        self._bvh             = bvh
        self._triangles        = triangles
        self._cache            = cache
        self._weights           = weights
        self._base_stats = {
            "projection_failed":    projection_failed,
            "degenerate_triangles": degenerate_count,
            "masked_vertices":      masked_vertices,
        }
        self._ready = True
        return True

    # ── Public ───────────────────────────────────────────────────────────

    def transfer_shape_keys(self, shapekey_names=None):
        """
        Transfer shape keys from source to target.

        shapekey_names - list of shape key names to transfer;
                         None = all source shape keys except Basis.

        Returns True if at least one shape key was transferred.
        Diagnostics for this call are available afterwards in self.last_stats.
        """
        self.last_stats = {
            "transferred_keys":     0,
            "skipped_keys":         0,
            "projection_failed":    0,
            "degenerate_triangles": 0,
            "barycentric_failed":   0,
            "masked_vertices":      0,
        }

        if not self._prepare():
            return False

        self.last_stats.update(self._base_stats)

        source   = self._source
        target   = self._target
        src_mesh = source.data
        tgt_mesh = target.data

        key_blocks = src_mesh.shape_keys.key_blocks
        basis_name = key_blocks[0].name

        candidates = [kb for kb in key_blocks if kb.name != basis_name]
        if shapekey_names is not None:
            name_set   = set(shapekey_names)
            candidates = [kb for kb in candidates if kb.name in name_set]

        if not candidates:
            return False

        # Ensure the target has a Basis shape key.
        if not tgt_mesh.shape_keys:
            try:
                target.shape_key_add(name='Basis', from_mix=False)
            except Exception:
                return False

        # ── Save target state (value + mute) before touching anything ──────
        saved_state = {}
        for kb in tgt_mesh.shape_keys.key_blocks:
            saved_state[kb.name] = {"value": kb.value, "mute": kb.mute}

        saved_active = int(getattr(target, "active_shape_key_index", 0))

        tgt_mesh.shape_keys.key_blocks[0].value = 0.0

        n_tgt_basis = len(self._tgt_basis_world)
        transferred = 0

        for src_kb in candidates:
            src_sk_local = _get_sk_coords_local(source, src_kb.name)

            # Source shape key data must match the source basis length.
            if len(src_sk_local) != len(self._src_basis_world):
                self.last_stats["skipped_keys"] += 1
                continue

            src_sk_world = _to_world(self._src_mat, src_sk_local)

            tgt_kb        = tgt_mesh.shape_keys.key_blocks.get(src_kb.name)
            existing_world = None
            prev_value    = 0.0
            prev_mute     = False

            if tgt_kb is not None:
                # Existing target shape key data must match the target basis length.
                if len(tgt_kb.data) != n_tgt_basis:
                    self.last_stats["skipped_keys"] += 1
                    continue
                saved      = saved_state.get(src_kb.name, {})
                prev_value = saved.get("value", 0.0)
                prev_mute  = saved.get("mute", False)
                if self._weights is not None:
                    existing_local = [Vector(d.co) for d in tgt_kb.data]
                    existing_world = _to_world(self._tgt_mat, existing_local)

            new_coords_world, bary_failed = _compute_new_sk_coords(
                self._cache, self._src_basis_world, src_sk_world,
                self._tgt_basis_world, self._weights, existing_world,
            )
            self.last_stats["barycentric_failed"] += bary_failed

            if tgt_kb is None:
                try:
                    tgt_kb = target.shape_key_add(name=src_kb.name, from_mix=False)
                except Exception:
                    self.last_stats["skipped_keys"] += 1
                    continue

            if len(tgt_kb.data) != len(new_coords_world):
                self.last_stats["skipped_keys"] += 1
                continue

            new_coords_local = _to_local(self._tgt_mat_inv, new_coords_world)

            try:
                for i, co in enumerate(new_coords_local):
                    tgt_kb.data[i].co = co
            except Exception:
                # Rare RNA/data write failure - skip this key rather than
                # leaving it half-written.
                self.last_stats["skipped_keys"] += 1
                continue

            try:
                tgt_kb.slider_min = src_kb.slider_min
                tgt_kb.slider_max = src_kb.slider_max
            except Exception:
                # Slider range is secondary; don't let it block the transfer.
                pass

            tgt_kb.value = prev_value
            tgt_kb.mute  = prev_mute

            transferred += 1
            self.last_stats["transferred_keys"] += 1

        # ── Restore target state ────────────────────────────────────────────
        for kb in tgt_mesh.shape_keys.key_blocks:
            saved = saved_state.get(kb.name)
            if saved is not None:
                kb.value = saved["value"]
                kb.mute  = saved["mute"]

        tgt_mesh.shape_keys.key_blocks[0].value = 0.0

        try:
            n_keys = len(tgt_mesh.shape_keys.key_blocks)
            target.active_shape_key_index = min(saved_active, max(0, n_keys - 1))
        except Exception:
            pass

        if transferred > 0:
            try:
                target.data.update()
            except Exception:
                pass

        return transferred > 0

    def free(self):
        # Release cached BVH / projection data. Safe to call multiple times.
        self._bvh             = None
        self._triangles        = None
        self._cache            = None
        self._weights           = None
        self._src_mat           = None
        self._tgt_mat           = None
        self._tgt_mat_inv       = None
        self._src_basis_world   = None
        self._tgt_basis_world   = None
        self._base_stats        = None
        self._ready             = False


# ══════════════════════════════════════════════════════════════════════════════
#  PROPERTYGROUP (per-object settings)
# ══════════════════════════════════════════════════════════════════════════════

class SKV_TransferSettings(PropertyGroup):
    """
    Per-object transfer settings.
    Registered as bpy.types.Object.skv_transfer_settings in __init__.py.

    Currently unused by the UI; kept minimal so it does not conflict with
    the rest of the addon's architecture.
    """

    vertex_group: StringProperty(
        name="Mask Vertex Group",
        description=(
            "Vertex group on this object used as a transfer mask "
            "(empty = full transfer to all vertices)"
        ),
        default="",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  REGISTRATION EXPORT
# ══════════════════════════════════════════════════════════════════════════════

# Blender classes to register, combined in __init__.py with other modules'
# CLASSES tuples:
#   _ALL_CLASSES = _LOCAL_CLASSES + groups.CLASSES + presets.CLASSES + transfer.CLASSES
CLASSES = (
    SKV_TransferSettings,
)
