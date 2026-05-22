bl_info = {
    "name": "Shape Keys Viewer",
    "author": "xtafr001",
    "version": (0, 5, 5),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > ShapeKeys",
    "description": "Shape keys grouping, selection tools, presets, and mesh data transfer.",
    "category": "Object",
}

import re
import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    PointerProperty,
    CollectionProperty,
    EnumProperty,
)

from .common import (
    enum_groups_for_active_object,
    get_shape_key_data,
    has_group_storage,
    is_initialized,
    get_selected_group_name,
    count_keys_in_group,
    tag_redraw_view3d,
    INIT_GROUP_NAME,
    kd_selected_set,
    kd_get_group,
    kd_set_selected,
    kd_clear_selected,
    is_internal_value_change,
)
from . import groups
from . import presets
from . import meshDataTransfer


def _poll_mesh_object(self, obj):
    # Accept only mesh objects.
    return bool(obj) and getattr(obj, "type", None) == "MESH"


def _poll_transfer_target(scene, obj):
    # Accept only mesh objects and exclude the current source mesh (stored on Scene).
    if not obj or getattr(obj, "type", None) != "MESH":
        return False
    src_name = getattr(scene, "skv_transfer_source_name", "")
    return (not src_name) or (obj.name != src_name)


def _is_quick_shape_key_name(name: str) -> bool:
    return bool(re.fullmatch(r"Shape Key(?: \d+)?", name or ""))


def _iter_quick_shape_keys(key_data):
    if not key_data or not getattr(key_data, "key_blocks", None):
        return []
    return [kb for kb in key_data.key_blocks if _is_quick_shape_key_name(kb.name)]


def _next_quick_shape_key_name(key_data) -> str:
    if not key_data or not getattr(key_data, "key_blocks", None):
        return "Shape Key"

    existing = {kb.name for kb in key_data.key_blocks}
    if "Shape Key" not in existing:
        return "Shape Key"

    index = 1
    while f"Shape Key {index}" in existing:
        index += 1
    return f"Shape Key {index}"


# -----------------------------
# Default value capture (for Active Shape Keys)
# -----------------------------
def _defaults_rebuild(key_data) -> None:
    # Rebuild default values snapshot for the current key blocks.
    try:
        key_data.skv_key_defaults.clear()
    except Exception:
        return

    # Reset "active keys" tracking on rescan/init.
    try:
        key_data.skv_active_keys.clear()
    except Exception:
        pass

    kb_iter = getattr(key_data, "key_blocks", None) or []
    for kb in kb_iter:
        it = key_data.skv_key_defaults.add()
        it.name = kb.name
        try:
            it.value = float(kb.value)
        except Exception:
            it.value = 0.0


def _defaults_get(key_data, key_name: str):
    # Returns default value for given key_name, or None if missing.
    for it in getattr(key_data, "skv_key_defaults", []) or []:
        if it.name == key_name:
            return float(it.value)
    return None


def _defaults_ensure(key_data) -> None:
    # Ensure defaults exist and match current key blocks set.
    kb = getattr(key_data, "key_blocks", None)
    if not kb:
        return

    defaults = getattr(key_data, "skv_key_defaults", None)
    if defaults is None:
        return

    if len(defaults) == 0:
        _defaults_rebuild(key_data)
        return

    if len(defaults) != len(kb):
        _defaults_rebuild(key_data)
        return

    for i, k in enumerate(kb):
        if defaults[i].name != k.name:
            _defaults_rebuild(key_data)
            return


def _active_keys_contains(key_data, key_name: str) -> bool:
    for it in getattr(key_data, "skv_active_keys", []) or []:
        if it.name == key_name:
            return True
    return False


def _active_keys_add_if_needed(key_data, key_name: str) -> None:
    if not key_name or _active_keys_contains(key_data, key_name):
        return
    try:
        it = key_data.skv_active_keys.add()
        it.name = key_name
    except Exception:
        return

    # Auto-expand Active Shape Keys block when a new active key is detected.
    try:
        scn = bpy.context.scene
        props = getattr(scn, "skv_props", None)
        if props:
            props.active_keys_open = True
    except Exception:
        pass


_SKV_ACTIVE_KEYS_LAST_FRAME = None


def _active_keys_update_from_values(obj) -> None:
    # Add keys that differ from defaults to the active list (do not auto-remove).
    # Only treat manual edits as "active":
    # - ignore updates during animation playback and frame changes (evaluation/scrub)
    # - ignore programmatic changes made by addon operators (guarded)
    global _SKV_ACTIVE_KEYS_LAST_FRAME

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if not has_group_storage(key_data) or not is_initialized(key_data):
        return

    if is_internal_value_change():
        return

    # Ignore playback-driven changes.
    try:
        scr = bpy.context.screen
        if scr and getattr(scr, "is_animation_playing", False):
            return
    except Exception:
        pass

    # Ignore changes caused by frame evaluation (scrub / frame step).
    try:
        cur_frame = int(bpy.context.scene.frame_current)
    except Exception:
        cur_frame = None

    if cur_frame is not None:
        if _SKV_ACTIVE_KEYS_LAST_FRAME is None:
            _SKV_ACTIVE_KEYS_LAST_FRAME = cur_frame
        elif cur_frame != _SKV_ACTIVE_KEYS_LAST_FRAME:
            _SKV_ACTIVE_KEYS_LAST_FRAME = cur_frame
            return

    _defaults_ensure(key_data)

    eps = 1e-6
    for kb in key_data.key_blocks:
        d = _defaults_get(key_data, kb.name)
        if d is None:
            continue
        try:
            if abs(float(kb.value) - float(d)) > eps:
                _active_keys_add_if_needed(key_data, kb.name)
        except Exception:
            continue


# -----------------------------
# Automatic object sync + auto-scan
# -----------------------------
_SKV_SYNC_GUARD = False


def _shape_key_signature(obj) -> str:
    if not obj or getattr(obj, "type", None) != "MESH":
        return ""

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return "NO_SHAPE_KEYS"

    try:
        names = [kb.name for kb in key_data.key_blocks]
    except Exception:
        return "SHAPE_KEYS_UNKNOWN"

    return "|".join(names)


def _auto_process_active_object(scene):
    """
    Sync selected object with scene selection and automatically initialize
    addon storage when a mesh object with shape keys becomes active.
    Also captures default shape key values for 'Active Shape Keys' block.
    """
    global _SKV_SYNC_GUARD
    if _SKV_SYNC_GUARD:
        return

    props = getattr(scene, "skv_props", None)
    if not props:
        return

    ctx = bpy.context
    active = getattr(ctx, "active_object", None)

    desired = active if (active and getattr(active, "type", None) == "MESH") else None
    desired_name = desired.name if desired else ""
    desired_signature = _shape_key_signature(desired)

    if (
        getattr(props, "last_active_object_name", "") == desired_name
        and getattr(props, "last_shape_key_signature", "") == desired_signature
    ):
        return

    _SKV_SYNC_GUARD = True
    try:
        previous_name = getattr(props, "last_active_object_name", "")
        object_changed = previous_name != desired_name

        props.last_active_object_name = desired_name
        props.last_shape_key_signature = desired_signature
        props.object_pick = desired

        # Reset scan status on any object/signature change first.
        props.scan_status = ""
        # Collapse Active Shape Keys only after object switch; it auto-expands when a key becomes active.
        if object_changed:
            props.active_keys_open = False

        if not desired:
            tag_redraw_view3d(ctx)
            return

        key_data = get_shape_key_data(desired)
        if not key_data or not getattr(key_data, "key_blocks", None):
            props.scan_status = "No Shape Keys found."
            tag_redraw_view3d(ctx)
            return

        if not has_group_storage(key_data):
            tag_redraw_view3d(ctx)
            return

        if getattr(key_data, "library", None) is not None:
            tag_redraw_view3d(ctx)
            return

        if not is_initialized(key_data):
            from .common import ensure_init_setup_write

            ensure_init_setup_write(desired)

        # (Re)build defaults snapshot on first run or if mismatched.
        _defaults_ensure(key_data)

        tag_redraw_view3d(ctx)
    finally:
        _SKV_SYNC_GUARD = False


@persistent
def _depsgraph_update_post(scene, depsgraph):
    # 1) keep selection->addon sync + auto-init
    _auto_process_active_object(scene)
    # 2) update active-keys list from value changes (do not remove)
    try:
        obj = getattr(bpy.context, "active_object", None)
        if obj and getattr(obj, "type", None) == "MESH":
            _active_keys_update_from_values(obj)
    except Exception:
        pass
    # 3) auto keyframe (enabled per shape key)
    try:
        props = getattr(scene, "skv_props", None)
        obj = getattr(props, "object_pick", None) if props else None
        if not obj:
            obj = getattr(bpy.context, "active_object", None)
        if obj and getattr(obj, "type", None) == "MESH":
            key_data = get_shape_key_data(obj)
            if key_data and hasattr(key_data, "skv_auto_keyframes") and len(key_data.skv_auto_keyframes) > 0:
                frame = int(scene.frame_current)
                eps = 1e-6

                if getattr(key_data, "library", None) is None and getattr(key_data, "key_blocks", None):
                    for it in key_data.skv_auto_keyframes:
                        if not getattr(it, "enabled", False):
                            continue
                        name = (it.name or "").strip()
                        if not name:
                            continue
                        kb = key_data.key_blocks.get(name)
                        if not kb:
                            continue

                        try:
                            cur_val = float(kb.value)
                        except Exception:
                            cur_val = 0.0

                        last_frame = int(getattr(it, "last_frame", -999999))
                        last_val = float(getattr(it, "last_value", cur_val))

                        if frame != last_frame:
                            it.last_frame = frame
                            it.last_value = cur_val
                            continue

                        if abs(cur_val - last_val) > eps:
                            try:
                                key_data.keyframe_insert(data_path=f'key_blocks["{kb.name}"].value', frame=frame)
                            except Exception:
                                pass
                            it.last_value = cur_val
    except Exception:
        pass


def _ensure_handler_installed():
    h = bpy.app.handlers.depsgraph_update_post
    if _depsgraph_update_post not in h:
        h.append(_depsgraph_update_post)


def _ensure_handler_removed():
    h = bpy.app.handlers.depsgraph_update_post
    if _depsgraph_update_post in h:
        h.remove(_depsgraph_update_post)


# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_quick_shape_keys(UIList):
    bl_idname = "SKV_UL_quick_shape_keys"

    def filter_items(self, context, data, propname):
        key_data = data
        flt_flags = []
        flt_neworder = []
        bitflag = self.bitflag_filter_item

        for kb in getattr(key_data, propname):
            flt_flags.append(bitflag if _is_quick_shape_key_name(kb.name) else 0)

        return flt_flags, flt_neworder

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        kb = item
        row = layout.row(align=True)
        row.label(text=kb.name)
        row.prop(kb, "value", text="", slider=True)

        op = row.operator("skv.quick_shape_key_delete", text="", icon="TRASH", emboss=False)
        op.key_index = index


# -----------------------------
# Operators
# -----------------------------
class SKV_OT_SearchClear(Operator):
    bl_idname = "skv.search_clear"
    bl_label = "Clear Search"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if hasattr(context.scene, "skv_props"):
            context.scene.skv_props.search = ""
            context.scene.skv_props.keys_index = -1
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_QuickShapeKeyAdd(Operator):
    bl_idname = "skv.quick_shape_key_add"
    bl_label = "Add Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = getattr(context.scene.skv_props, "object_pick", None) if hasattr(context.scene, "skv_props") else None
        return bool(obj and getattr(obj, "type", None) == "MESH")

    def execute(self, context):
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)
        if not obj or getattr(obj, "type", None) != "MESH":
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if key_data and getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        view_layer = context.view_layer
        try:
            view_layer.objects.active = obj
        except Exception:
            pass

        try:
            obj.select_set(True)
        except Exception:
            pass

        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        # Ensure Basis exists.
        if not get_shape_key_data(obj):
            obj.shape_key_add(name="Basis", from_mix=False)

        key_data = get_shape_key_data(obj)
        new_name = _next_quick_shape_key_name(key_data)

        # Add new shape key.
        new_kb = obj.shape_key_add(name=new_name, from_mix=False)
        key_data = get_shape_key_data(obj)

        if key_data and getattr(key_data, "key_blocks", None):
            try:
                obj.active_shape_key_index = len(key_data.key_blocks) - 1
                props.quick_keys_index = len(key_data.key_blocks) - 1
            except Exception:
                pass

        try:
            new_kb.value = 1.0
        except Exception:
            pass

        props.quick_shape_key_editing = True
        props.quick_shape_key_name = getattr(new_kb, "name", "")
        props.quick_keys_open = True

        try:
            bpy.ops.object.mode_set(mode="SCULPT")
        except Exception:
            self.report({"WARNING"}, "Shape key created, but failed to switch to Sculpt Mode.")
            tag_redraw_view3d(context)
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_QuickShapeKeyDelete(Operator):
    bl_idname = "skv.quick_shape_key_delete"
    bl_label = "Delete Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    key_index: IntProperty(name="Key Index", default=-1)

    @classmethod
    def poll(cls, context):
        obj = getattr(context.scene.skv_props, "object_pick", None) if hasattr(context.scene, "skv_props") else None
        key_data = get_shape_key_data(obj) if obj else None
        return bool(obj and getattr(obj, "type", None) == "MESH" and key_data and getattr(key_data, "key_blocks", None))

    def execute(self, context):
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)
        key_data = get_shape_key_data(obj) if obj else None

        if not obj or getattr(obj, "type", None) != "MESH" or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        idx = int(self.key_index)
        if not (0 <= idx < len(key_data.key_blocks)):
            return {"CANCELLED"}

        kb = key_data.key_blocks[idx]
        key_name = kb.name
        if not _is_quick_shape_key_name(key_name):
            return {"CANCELLED"}

        try:
            context.view_layer.objects.active = obj
        except Exception:
            pass

        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        try:
            obj.shape_key_remove(kb)
        except Exception:
            self.report({"ERROR"}, "Failed to delete shape key.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        try:
            if key_data and getattr(key_data, "key_blocks", None):
                props.quick_keys_index = min(max(0, idx - 1), len(key_data.key_blocks) - 1)
            else:
                props.quick_keys_index = -1
        except Exception:
            pass

        if getattr(props, "quick_shape_key_name", "") == key_name:
            props.quick_shape_key_editing = False
            props.quick_shape_key_name = ""

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_QuickShapeKeyFix(Operator):
    bl_idname = "skv.quick_shape_key_fix"
    bl_label = "Fix"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = context.scene.skv_props if hasattr(context.scene, "skv_props") else None
        return bool(props and getattr(props, "quick_shape_key_editing", False))

    def execute(self, context):
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        if obj and getattr(obj, "type", None) == "MESH":
            try:
                context.view_layer.objects.active = obj
            except Exception:
                pass

        try:
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

        props.quick_shape_key_editing = False
        props.quick_shape_key_name = ""

        tag_redraw_view3d(context)
        return {"FINISHED"}


# -----------------------------
# Scene Props (UI state)
# -----------------------------
def transfer_open_update(self, context):
    # Clear last transfer status when the module is collapsed.
    if not getattr(self, "transfer_open", False):
        obj = getattr(context, "active_object", None)
        if obj and hasattr(obj, "skv_mesh_data_transfer"):
            try:
                obj.skv_mesh_data_transfer.transfer_status = ""
            except Exception:
                pass


# def affix_select_update(self, context):
#     obj = getattr(self, "object_pick", None)
#     if not obj:
#         return
#
#     key_data = get_shape_key_data(obj)
#     if not key_data or not getattr(key_data, "key_blocks", None):
#         return
#     if not has_group_storage(key_data) or not is_initialized(key_data):
#         return
#
#     kd_clear_selected(key_data)
#
#     text = (getattr(self, "affix_value", "") or "").strip()
#     if not text:
#         tag_redraw_view3d(context)
#         return
#
#     text_l = text.lower()
#     mode = getattr(self, "affix_type", "PREFIX")
#     group_name = get_selected_group_name(key_data)
#     matched = []
#
#     for kb in key_data.key_blocks:
#         if kb.name == "Basis":
#             continue
#         if kd_get_group(key_data, kb.name) != group_name:
#             continue
#
#         kb_name_l = kb.name.lower()
#
#         if mode == "PREFIX" and kb_name_l.startswith(text_l):
#             matched.append(kb.name)
#         elif mode == "SUFFIX" and kb_name_l.endswith(text_l):
#             matched.append(kb.name)
#
#     for name in matched:
#         kd_set_selected(key_data, name, True)
#
#     if matched:
#         self.last_affix_name = text
#         self.last_affix_pending = True
#
#     tag_redraw_view3d(context)


class SKV_Props(PropertyGroup):
    keys_index: IntProperty(name="Keys Index", default=-1, min=-1)
    search: StringProperty(name="Search", default="")

    groups_module_open: BoolProperty(name="Shape Keys", default=True)
    groups_open: BoolProperty(name="Groups", default=True)
    keys_open: BoolProperty(name="Keys", default=True)
    active_keys_open: BoolProperty(name="Active Shape Keys", default=True)
    quick_keys_open: BoolProperty(name="Quick Shape Keys", default=True)
    quick_keys_index: IntProperty(name="Quick Shape Keys Index", default=-1, min=-1)

    object_pick: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
    )

    last_active_object_name: StringProperty(
        name="Last Active Object Name",
        default="",
        options={"SKIP_SAVE"},
    )
    last_shape_key_signature: StringProperty(
        name="Last Shape Key Signature",
        default="",
        options={"SKIP_SAVE"},
    )

    scan_status: StringProperty(name="Scan Status", default="", options={"SKIP_SAVE"})

    presets_open: BoolProperty(name="Presets", default=False)

    quick_shape_key_editing: BoolProperty(name="Quick Shape Key Editing", default=False)
    quick_shape_key_name: StringProperty(name="Quick Shape Key Name", default="")

    transfer_open: BoolProperty(name="Shape Keys Transfer", default=False, update=transfer_open_update)
    transfer_inheritance: BoolProperty(
        name="Inherit presets",
        default=False,
        description="Add transferred shape keys to the same presets for the target object",
    )
    move_to_group: EnumProperty(name="Move To", items=enum_groups_for_active_object)

#     affix_type: EnumProperty(
#         name="Type",
#         items=[
#             ("PREFIX", "Prefix", "Select by prefix"),
#             ("SUFFIX", "Suffix", "Select by suffix"),
#         ],
#         default="PREFIX",
#         update=affix_select_update,
#     )
#     affix_value: StringProperty(
#         name="Value",
#         default="",
#         description="Comma/semicolon separated list (e.g. L_, R_ or _L, _R)",
#         update=affix_select_update,
#     )

    last_affix_name: StringProperty(name="Last Affix Name", default="")
    last_affix_pending: BoolProperty(name="Last Affix Pending", default=False)


class SKV_PT_ObjectPanel(Panel):
    bl_label = "Object"
    bl_idname = "SKV_PT_object_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"
    bl_order = 0

    def draw_header(self, context):
        self.layout.label(text="", icon="OBJECT_DATA")

    def draw(self, context):
        layout = self.layout
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        row = layout.row(align=True)
        row.label(text=obj.name if obj else "No selected object", icon="MESH_DATA")


class SKV_PT_ShapeKeysPanel(Panel):
    bl_label = "Shape Keys"
    bl_idname = "SKV_PT_shape_keys_viewer_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"
    bl_order = 10

    def draw_header(self, context):
        self.layout.label(text="", icon="SHAPEKEY_DATA")

    def draw(self, context):
        layout = self.layout
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        if not obj:
            layout.label(text="No selected object", icon="INFO")
            return

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            layout.label(text="No shape keys found", icon="INFO")

            row = layout.row(align=True)
            row.operator("skv.transfer_from", text="Transfer from...", icon="IMPORT")
            return
        if not has_group_storage(key_data):
            return
        if not is_initialized(key_data):
            return

        _defaults_ensure(key_data)

        current_group = get_selected_group_name(key_data) or INIT_GROUP_NAME

        selected_names = set(kd_selected_set(key_data))
        has_selected_valid = any(n and n != "Basis" for n in selected_names)
        has_presets = hasattr(context.scene, "skv_global_presets") and (len(context.scene.skv_global_presets) > 0)
        groups_count = len(key_data.skv_groups) if getattr(key_data, "skv_groups", None) else 0
        can_move_to_group = groups_count > 1

        groups_col = layout.column(align=True)
        hg = groups_col.row(align=True)
        ig = "TRIA_DOWN" if props.groups_open else "TRIA_RIGHT"
        hg.prop(props, "groups_open", text="", emboss=False, icon=ig)
        hg.label(text="Groups" if props.groups_open else f"Group: {current_group}")

        if props.groups_open:
            rowg = groups_col.row()
            rowg.template_list(
                "SKV_UL_groups",
                "",
                key_data,
                "skv_groups",
                key_data,
                "skv_group_index",
                rows=max(3, min(groups_count, 5)),
            )
            col = rowg.column(align=True)
            col.operator("skv.group_add", icon="ADD", text="")
            col.operator("skv.group_remove", icon="REMOVE", text="")

        keys_col = layout.column(align=True)
        hk = keys_col.row(align=True)
        ik = "TRIA_DOWN" if props.keys_open else "TRIA_RIGHT"
        hk.prop(props, "keys_open", text="", emboss=False, icon=ik)
        hk.label(text=f'Shape Keys in {current_group}')

        if props.keys_open:
            group_count = count_keys_in_group(key_data, current_group)

            if group_count > 0:
                row = keys_col.row(align=True)
                row.prop(props, "search", text="", icon="VIEWZOOM")
                row.operator("skv.search_clear", text="", icon="X")

                row = keys_col.row(align=True)
                row.operator("skv.select_visible", text="All").mode = "ALL"
                row.operator("skv.select_visible", text="Clear").mode = "CLEAR"
                row.operator("skv.select_visible", text="Invert").mode = "INVERT"

            key_rows = max(1, min(group_count, 5))
            keys_col.template_list(
                "SKV_UL_key_blocks",
                "",
                key_data,
                "key_blocks",
                props,
                "keys_index",
                rows=key_rows,
            )

            # Prefix/suffix selection UI is temporarily disabled.
            # if group_count > 0:
            #     row = keys_col.row(align=True)
            #     row.prop(props, "affix_type", text="")
            #     row.prop(props, "affix_value", text="", icon="FILTER")

            if has_selected_valid and group_count > 0:
                keys_col.separator()

                r1 = keys_col.row(align=True)
                r1m = r1.row(align=True)
                r1m.enabled = can_move_to_group
                r1m.menu("SKV_MT_move_to_group", text="Move to group", icon="FILE_FOLDER")
                r1.operator("skv.create_group_from_selected", text="Create group", icon="NEWFOLDER")

                r2 = keys_col.row(align=True)
                r2m = r2.row(align=True)
                r2m.enabled = has_presets
                r2m.menu("SKV_MT_add_to_preset", text="Add to preset", icon="PRESET")
                r2.operator("skv.global_preset_add_from_selected", text="Create preset", icon="PRESET")

                r3 = keys_col.row(align=True)
                r3.operator("skv.reset_group_values", text="Zero selected values", icon="RECOVER_LAST")
                r3.operator("skv.transfer_to", text="Transfer to...", icon="EXPORT")

        active_col = layout.column(align=True)
        ha = active_col.row(align=True)
        ia = "TRIA_DOWN" if props.active_keys_open else "TRIA_RIGHT"
        ha.prop(props, "active_keys_open", text="", emboss=False, icon=ia)
        ha.label(text="Active Shape Keys")

        if props.active_keys_open:
            active_count = len(key_data.skv_active_keys) if getattr(key_data, "skv_active_keys", None) else 0
            if active_count > 0:
                active_rows = max(1, min(active_count, 5))
                active_col.template_list(
                    "SKV_UL_active_keys",
                    "",
                    key_data,
                    "skv_active_keys",
                    key_data,
                    "skv_active_keys_index",
                    rows=active_rows,
                )


class SKV_PT_QuickShapeKeyPanel(Panel):
    bl_label = "Quick Shape Key"
    bl_idname = "SKV_PT_quick_shape_key_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"
    bl_order = 20

    def draw_header(self, context):
        self.layout.label(text="", icon="SCULPTMODE_HLT")

    def draw(self, context):
        layout = self.layout
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        if not obj:
            layout.label(text="No selected object", icon="INFO")
            return

        if getattr(obj, "type", None) != "MESH":
            layout.label(text="Active object is not a mesh", icon="INFO")
            return

        key_data = get_shape_key_data(obj)
        quick_keys = _iter_quick_shape_keys(key_data)

        col = layout.column(align=True)

        if props.quick_shape_key_editing:
            if props.quick_shape_key_name:
                col.label(text=f"Editing: {props.quick_shape_key_name}")
            col.operator("skv.quick_shape_key_fix", text="Fix", icon="CHECKMARK")
        else:
            col.operator("skv.quick_shape_key_add", text="Add Shape Key", icon="ADD")

        if quick_keys:
            col.separator()

            header = col.row(align=True)
            icon = "TRIA_DOWN" if props.quick_keys_open else "TRIA_RIGHT"
            header.prop(props, "quick_keys_open", text="", emboss=False, icon=icon)
            header.label(text="Quick Shape Keys")

            if props.quick_keys_open:
                quick_rows = max(1, min(len(quick_keys), 5))
                col.template_list(
                    "SKV_UL_quick_shape_keys",
                    "",
                    key_data,
                    "key_blocks",
                    props,
                    "quick_keys_index",
                    rows=quick_rows,
                )


class SKV_PT_PresetsPanel(Panel):
    bl_label = "Presets"
    bl_idname = "SKV_PT_shape_keys_presets_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"
    bl_order = 30

    def draw_header(self, context):
        self.layout.label(text="", icon="PRESET")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        props = scene.skv_props

        if len(scene.skv_global_presets) == 0:
            layout.label(text="No presets found", icon="INFO")
            layout.operator("skv.global_preset_add_empty", text="Add Preset", icon="ADD")
            return

        gpreset = presets.get_active_global_preset(scene)
        preset_name = gpreset.name if gpreset else "None"

        presets_col = layout.column(align=True)

        hp = presets_col.row(align=True)
        ip = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
        hp.prop(props, "presets_open", text="", emboss=False, icon=ip)
        hp.label(text="Presets" if props.presets_open else f"Preset: {preset_name}")

        if props.presets_open:
            row = presets_col.row()
            row.template_list(
                "SKV_UL_global_presets",
                "",
                scene,
                "skv_global_presets",
                scene,
                "skv_global_preset_index",
                rows=max(1, min(len(scene.skv_global_presets), 5)),
            )
            col = row.column(align=True)
            col.operator("skv.global_preset_add_empty", icon="ADD", text="")
            col.operator("skv.global_preset_remove", icon="REMOVE", text="")

        gpreset = presets.get_active_global_preset(scene)
        if gpreset:
            try:
                presets.sync_preset_item_values(context, gpreset)
            except Exception:
                pass

            grouped_items = list(presets.iter_preset_items_grouped(gpreset))
            if grouped_items:
                presets_col.separator()
                presets_col.label(text=f"Shape Keys in {preset_name}")

                for object_name, object_items, controller_item in grouped_items:
                    if not controller_item:
                        continue

                    header = presets_col.row(align=True)
                    header.prop(
                        controller_item,
                        "object_open",
                        text="",
                        emboss=False,
                        icon=("TRIA_DOWN" if controller_item.object_open else "TRIA_RIGHT"),
                    )
                    header.label(text=object_name, icon="OBJECT_DATA")

                    if controller_item.object_open:
                        presets.set_preset_list_filter_object(object_name)
                        presets_col.template_list(
                            "SKV_UL_global_preset_key_sliders",
                            object_name,
                            gpreset,
                            "items",
                            gpreset,
                            "items_index",
                            rows=max(1, min(len(object_items), 5)),
                        )

                presets.set_preset_list_filter_object("")


_LOCAL_CLASSES = (
    SKV_UL_quick_shape_keys,
    SKV_OT_SearchClear,
    SKV_OT_QuickShapeKeyAdd,
    SKV_OT_QuickShapeKeyDelete,
    SKV_OT_QuickShapeKeyFix,
    SKV_Props,
    SKV_PT_ObjectPanel,
    SKV_PT_ShapeKeysPanel,
    SKV_PT_QuickShapeKeyPanel,
    SKV_PT_PresetsPanel,
)

_ALL_CLASSES = _LOCAL_CLASSES + groups.CLASSES + presets.CLASSES + meshDataTransfer.CLASSES


def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.skv_props = PointerProperty(type=SKV_Props)

    bpy.types.Scene.skv_global_presets = CollectionProperty(type=presets.SKV_GlobalPreset)
    bpy.types.Scene.skv_global_preset_index = IntProperty(name="Preset Index", default=0, min=0)

    bpy.types.Scene.skv_transfer_source_name = StringProperty(options={"SKIP_SAVE"})
    bpy.types.Scene.skv_transfer_target = PointerProperty(type=bpy.types.Object, poll=_poll_transfer_target)

    bpy.types.Key.skv_groups = CollectionProperty(type=groups.SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(name="Group Index", default=0, min=0)

    bpy.types.Key.skv_selected = CollectionProperty(type=groups.SKV_SelectedName)
    bpy.types.Key.skv_key_groups = CollectionProperty(type=groups.SKV_KeyGroupEntry)

    bpy.types.Key.skv_key_defaults = CollectionProperty(type=groups.SKV_KeyDefaultEntry)

    bpy.types.Key.skv_active_keys = CollectionProperty(type=groups.SKV_ActiveKeyEntry)
    bpy.types.Key.skv_active_keys_index = IntProperty(name="Active Keys Index", default=-1, min=-1)

    bpy.types.Key.skv_auto_keyframes = CollectionProperty(type=groups.SKV_AutoKeyframeEntry)

    bpy.types.Object.skv_mesh_data_transfer = PointerProperty(type=meshDataTransfer.SKV_MeshDataSettings)

    _ensure_handler_installed()
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is not None:
        _auto_process_active_object(scene)


def unregister():
    _ensure_handler_removed()

    del bpy.types.Object.skv_mesh_data_transfer

    if hasattr(bpy.types.Key, "skv_active_keys_index"):
        del bpy.types.Key.skv_active_keys_index
    if hasattr(bpy.types.Key, "skv_active_keys"):
        del bpy.types.Key.skv_active_keys
    if hasattr(bpy.types.Key, "skv_key_defaults"):
        del bpy.types.Key.skv_key_defaults

    if hasattr(bpy.types.Key, "skv_auto_keyframes"):
        del bpy.types.Key.skv_auto_keyframes

    del bpy.types.Key.skv_key_groups
    del bpy.types.Key.skv_selected
    del bpy.types.Key.skv_groups
    del bpy.types.Key.skv_group_index

    if hasattr(bpy.types.Scene, "skv_transfer_target"):
        del bpy.types.Scene.skv_transfer_target
    if hasattr(bpy.types.Scene, "skv_transfer_source_name"):
        del bpy.types.Scene.skv_transfer_source_name

    if hasattr(bpy.types.Scene, "skv_global_preset_index"):
        del bpy.types.Scene.skv_global_preset_index
    if hasattr(bpy.types.Scene, "skv_global_presets"):
        del bpy.types.Scene.skv_global_presets

    del bpy.types.Scene.skv_props

    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()