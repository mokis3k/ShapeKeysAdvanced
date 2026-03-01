bl_info = {
    "name": "Shape Keys Viewer",
    "author": "xtafr001",
    "version": (0, 5, 5),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > ShapeKeys",
    "description": "Shape keys grouping, selection tools, presets, and mesh data transfer.",
    "category": "Object",
}

import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator, Panel, PropertyGroup
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
    show_select_update,
    get_shape_key_data,
    has_group_storage,
    is_initialized,
    get_selected_group_name,
    count_keys_in_group,
    tag_redraw_view3d,
    INIT_GROUP_NAME,
    kd_selected_set,
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

    if getattr(props, "last_active_object_name", "") == desired_name:
        return

    _SKV_SYNC_GUARD = True
    try:
        props.last_active_object_name = desired_name
        props.object_pick = desired

        # Reset scan status on any change first.
        props.scan_status = ""
        # Collapse Active Shape Keys after scanning; it auto-expands when a key becomes active.
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


class SKV_Props(PropertyGroup):
    keys_index: IntProperty(name="Keys Index", default=-1, min=-1)
    search: StringProperty(name="Search", default="")
    show_select: BoolProperty(name="Select", default=False, update=show_select_update)

    # Group workspace top-level container (collapsible)
    groups_module_open: BoolProperty(name="Shape Keys", default=True)

    # Groups and Keys blocks (collapsible)
    groups_open: BoolProperty(name="Groups", default=True)
    keys_open: BoolProperty(name="Keys", default=True)
    active_keys_open: BoolProperty(name="Active Shape Keys", default=True)

    # Current mesh selected in the scene (synced via depsgraph handler).
    object_pick: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
    )

    # Stores the last seen active object name to avoid repeated re-init.
    last_active_object_name: StringProperty(
        name="Last Active Object Name",
        default="",
        options={"SKIP_SAVE"},
    )

    # Status shown under object label (skip saving to .blend).
    scan_status: StringProperty(name="Scan Status", default="", options={"SKIP_SAVE"})

    presets_open: BoolProperty(name="Presets", default=False)

    transfer_open: BoolProperty(name="Shape Keys Transfer", default=False, update=transfer_open_update)
    move_to_group: EnumProperty(name="Move To", items=enum_groups_for_active_object)

    affix_type: EnumProperty(
        name="Type",
        items=[
            ("PREFIX", "Prefix", "Select by prefix"),
            ("SUFFIX", "Suffix", "Select by suffix"),
        ],
        default="PREFIX",
    )
    affix_value: StringProperty(
        name="Value",
        default="",
        description="Comma/semicolon separated list (e.g. L_, R_ or _L, _R)",
    )

    # Tracks last "Apply" input from Prefix/Suffix selector.
    # Used to prefill name fields in "Create group" / preset dialogs.
    last_affix_name: StringProperty(name="Last Affix Name", default="")
    last_affix_pending: BoolProperty(name="Last Affix Pending", default=False)


# -----------------------------
# Panel
# -----------------------------
class SKV_PT_ShapeKeysPanel(Panel):
    bl_label = "Shape Keys Viewer"
    bl_idname = "SKV_PT_shape_keys_viewer_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"

    def draw(self, context):
        layout = self.layout
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        # CONTEXT
        box_ctx = layout.box()
        row = box_ctx.row(align=True)
        row.label(text="OBJECT", icon="OBJECT_DATA")

        row2 = box_ctx.row(align=True)
        row2.label(text=obj.name if obj else "No selected object", icon="MESH_DATA")

        if props.scan_status:
            box_ctx.separator()
            box_ctx.label(text=props.scan_status, icon="INFO")

        # Stop here if no suitable object selected.
        if not obj:
            return

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return
        if not has_group_storage(key_data):
            return
        if not is_initialized(key_data):
            return

        _defaults_ensure(key_data)

        current_group = get_selected_group_name(key_data) or INIT_GROUP_NAME

        # Selection state for enabling action buttons
        selected_names = set(kd_selected_set(key_data))
        has_selected_valid = any(n and n != "Basis" for n in selected_names)

        # presets presence gate
        has_presets = hasattr(context.scene, "skv_global_presets") and (len(context.scene.skv_global_presets) > 0)

        # SHAPE KEYS (workspace)
        box_ws = layout.box()
        head_ws = box_ws.row(align=True)
        icon_ws = "TRIA_DOWN" if props.groups_module_open else "TRIA_RIGHT"
        head_ws.prop(props, "groups_module_open", text="", emboss=False, icon=icon_ws)
        head_ws.label(text="SHAPE KEYS")

        if props.groups_module_open:
            # ---- Groups (collapsible) ----
            box_groups = box_ws.box()
            hg = box_groups.row(align=True)
            ig = "TRIA_DOWN" if props.groups_open else "TRIA_RIGHT"
            hg.prop(props, "groups_open", text="", emboss=False, icon=ig)
            hg.label(text="Groups" if props.groups_open else f"Group: {current_group}")

            if props.groups_open:
                rowg = box_groups.row()
                rowg.template_list(
                    "SKV_UL_groups",
                    "",
                    key_data,
                    "skv_groups",
                    key_data,
                    "skv_group_index",
                    rows=3,
                )
                col = rowg.column(align=True)
                col.operator("skv.group_add", icon="ADD", text="")
                col.operator("skv.group_remove", icon="REMOVE", text="")
                col.separator()
                col.operator("skv.group_rename", icon="GREASEPENCIL", text="")

            # ---- Keys in group (collapsible) ----
            box_keys = box_ws.box()
            hk = box_keys.row(align=True)
            ik = "TRIA_DOWN" if props.keys_open else "TRIA_RIGHT"
            hk.prop(props, "keys_open", text="", emboss=False, icon=ik)
            hk.label(text=f'Shape Keys in {current_group}')

            if props.keys_open:
                group_count = count_keys_in_group(key_data, current_group)

                if group_count > 0:
                    row = box_keys.row(align=True)
                    row.prop(props, "search", text="", icon="VIEWZOOM")
                    row.operator("skv.search_clear", text="", icon="X")

                    row = box_keys.row(align=True)
                    row.prop(props, "show_select", text="Select", toggle=True)

                    if props.show_select:
                        # All/Clear/Invert first
                        row = box_keys.row(align=True)
                        row.operator("skv.select_visible", text="All").mode = "ALL"
                        row.operator("skv.select_visible", text="Clear").mode = "NONE"
                        row.operator("skv.select_visible", text="Invert").mode = "INVERT"

                        # Prefix/Suffix under All/Clear/Invert
                        row = box_keys.row(align=True)
                        row.prop(props, "affix_type", text="")
                        row.prop(props, "affix_value", text="")
                        row.operator("skv.select_by_affix", text="Apply", icon="FILTER")

                box_keys.template_list(
                    "SKV_UL_key_blocks",
                    "",
                    key_data,
                    "key_blocks",
                    props,
                    "keys_index",
                    rows=15,
                )

                # Actions moved BELOW the list; disabled if no selected shape keys
                if props.show_select and group_count > 0:
                    box_keys.separator()

                    r1 = box_keys.row(align=True)
                    r1.enabled = has_selected_valid
                    r1.menu("SKV_MT_move_to_group", text="Move to group", icon="FILE_FOLDER")
                    r1.operator("skv.create_group_from_selected", text="Create group", icon="NEWFOLDER")

                    r2 = box_keys.row(align=True)
                    r2.enabled = has_selected_valid
                    r2m = r2.row(align=True)
                    r2m.enabled = has_selected_valid and has_presets
                    r2m.menu("SKV_MT_add_to_preset", text="Add to preset", icon="PRESET")
                    r2.operator("skv.global_preset_add_from_selected", text="Create preset", icon="PRESET")

                    r3 = box_keys.row(align=True)
                    r3.enabled = has_selected_valid
                    r3.operator("skv.transfer_to", text="Transfer to...", icon="EXPORT")
                    r3.operator("skv.reset_group_values", text="Zero selected values", icon="RECOVER_LAST")

            # ---- Active Shape Keys (collapsible) ----
            box_active = box_ws.box()
            ha = box_active.row(align=True)
            ia = "TRIA_DOWN" if props.active_keys_open else "TRIA_RIGHT"
            ha.prop(props, "active_keys_open", text="", emboss=False, icon=ia)
            ha.label(text="Active Shape Keys")

            if props.active_keys_open:
                if getattr(key_data, "skv_active_keys", None) or len(key_data.skv_active_keys) != 0:
                    box_active.template_list(
                        "SKV_UL_active_keys",
                        "",
                        key_data,
                        "skv_active_keys",
                        key_data,
                        "skv_active_keys_index",
                        rows=5,
                    )

        # PRESETS
        boxp = layout.box()
        headp = boxp.row(align=True)
        iconp = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
        headp.prop(props, "presets_open", text="", emboss=False, icon=iconp)
        headp.label(text="PRESETS")

        if props.presets_open:
            scene = context.scene

            row = boxp.row()
            row.template_list(
                "SKV_UL_global_presets",
                "",
                scene,
                "skv_global_presets",
                scene,
                "skv_global_preset_index",
                rows=4,
            )
            col = row.column(align=True)
            col.operator("skv.global_preset_add_empty", icon="ADD", text="")
            col.operator("skv.global_preset_remove", icon="REMOVE", text="")
            col.separator()
            col.operator("skv.global_preset_rename", icon="GREASEPENCIL", text="")

            gpreset = presets.get_active_global_preset(scene)
            if gpreset:
                # Keep proxy values in sync for the preset keys UI list.
                try:
                    presets.sync_preset_item_values(context, gpreset)
                except Exception:
                    pass

                boxp.separator()
                boxp.label(text="Preset Keys")
                rows = min(10, max(3, len(gpreset.items))) if gpreset.items else 3
                boxp.template_list(
                    "SKV_UL_global_preset_key_sliders",
                    "",
                    gpreset,
                    "items",
                    gpreset,
                    "items_index",
                    rows=rows,
                )


# -----------------------------
# Registration
# -----------------------------
_LOCAL_CLASSES = (
    SKV_OT_SearchClear,
    SKV_Props,
    SKV_PT_ShapeKeysPanel,
)

_ALL_CLASSES = _LOCAL_CLASSES + groups.CLASSES + presets.CLASSES + meshDataTransfer.CLASSES


def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.skv_props = PointerProperty(type=SKV_Props)

    # Presets storage on Scene (global-only)
    bpy.types.Scene.skv_global_presets = CollectionProperty(type=presets.SKV_GlobalPreset)
    bpy.types.Scene.skv_global_preset_index = IntProperty(name="Preset Index", default=0, min=0)

    # Transfer-to dialog storage (Scene-level datablock properties support eyedropper).
    bpy.types.Scene.skv_transfer_source_name = StringProperty(options={"SKIP_SAVE"})
    bpy.types.Scene.skv_transfer_target = PointerProperty(type=bpy.types.Object, poll=_poll_transfer_target)

    bpy.types.Key.skv_groups = CollectionProperty(type=groups.SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(name="Group Index", default=0, min=0)

    bpy.types.Key.skv_selected = CollectionProperty(type=groups.SKV_SelectedName)
    bpy.types.Key.skv_key_groups = CollectionProperty(type=groups.SKV_KeyGroupEntry)

    # Default shape key values snapshot (for Active Shape Keys)
    bpy.types.Key.skv_key_defaults = CollectionProperty(type=groups.SKV_KeyDefaultEntry)

    # Active keys tracking (persistent until user removes)
    bpy.types.Key.skv_active_keys = CollectionProperty(type=groups.SKV_ActiveKeyEntry)
    bpy.types.Key.skv_active_keys_index = IntProperty(name="Active Keys Index", default=-1, min=-1)

    # Auto keyframe tracking per shape key (stored on Key datablock)
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