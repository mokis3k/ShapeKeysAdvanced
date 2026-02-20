# __init__.py
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
    get_active_preset,
    INIT_GROUP_NAME,
)
from . import groups
from . import presets
from . import meshDataTransfer


# -----------------------------
# Poll helpers
# -----------------------------
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
# Automatic object sync + auto-scan
# -----------------------------
_SKV_SYNC_GUARD = False


def _auto_process_active_object(scene):
    """
    Sync selected object with scene selection and automatically initialize
    addon storage when a mesh object with shape keys becomes active.
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

        tag_redraw_view3d(ctx)
    finally:
        _SKV_SYNC_GUARD = False


@persistent
def _depsgraph_update_post(scene, depsgraph):
    _auto_process_active_object(scene)


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
    groups_module_open: BoolProperty(name="Groups", default=True)

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

    groups_open: BoolProperty(name="Groups", default=True)
    keys_open: BoolProperty(name="Keys", default=True)

    presets_open: BoolProperty(name="Presets", default=False)
    presets_scope: EnumProperty(
        name="Scope",
        items=[
            ("LOCAL", "Local", "Presets affect the active object only"),
            ("GLOBAL", "Global", "Presets affect multiple objects"),
        ],
        default="LOCAL",
    )

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
    # Used to prefill name fields in "Create new group" / "Create new preset" dialogs.
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

        current_group = get_selected_group_name(key_data) or INIT_GROUP_NAME

        # GROUP WORKSPACE
        box_ws = layout.box()
        head_ws = box_ws.row(align=True)
        icon_ws = "TRIA_DOWN" if props.groups_module_open else "TRIA_RIGHT"
        head_ws.prop(props, "groups_module_open", text="", emboss=False, icon=icon_ws)
        head_ws.label(text="SHAPE KEYS")

        if props.groups_module_open:
            # Groups list (static open)
            box_groups = box_ws.box()
            box_groups.label(text="Groups")

            rowg = box_groups.row()
            rowg.template_list(
                "SKV_UL_groups",
                "",
                key_data,
                "skv_groups",
                key_data,
                "skv_group_index",
                rows=5,
            )
            col = rowg.column(align=True)
            col.operator("skv.group_add", icon="ADD", text="")
            col.operator("skv.group_remove", icon="REMOVE", text="")
            col.separator()
            col.operator("skv.group_rename", icon="GREASEPENCIL", text="")

            # Keys list (static open)
            box_keys = box_ws.box()
            box_keys.label(text=f"Keys in '{current_group}'")

            group_count = count_keys_in_group(key_data, current_group)

            if group_count > 0:
                row = box_keys.row(align=True)
                row.prop(props, "search", text="", icon="VIEWZOOM")
                row.operator("skv.search_clear", text="", icon="X")

                row = box_keys.row(align=True)
                row.prop(props, "show_select", text="Select", toggle=True)
                if props.show_select:
                    row.menu("SKV_MT_select_actions", text="", icon="TRIA_RIGHT")

                if props.show_select:
                    row = box_keys.row(align=True)
                    row.operator("skv.select_visible", text="All").mode = "ALL"
                    row.operator("skv.select_visible", text="Clear").mode = "NONE"
                    row.operator("skv.select_visible", text="Invert").mode = "INVERT"

            box_keys.template_list(
                "SKV_UL_key_blocks",
                "",
                key_data,
                "key_blocks",
                props,
                "keys_index",
                rows=10,
            )

            if props.show_select and group_count > 0:
                row = box_keys.row(align=True)
                row.prop(props, "affix_type", text="")
                row.prop(props, "affix_value", text="")
                row.operator("skv.select_by_affix", text="Apply", icon="FILTER")

        # PRESETS
        boxp = layout.box()
        headp = boxp.row(align=True)
        iconp = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
        headp.prop(props, "presets_open", text="", emboss=False, icon=iconp)
        headp.label(text="PRESETS")

        if props.presets_open:
            scope_row = boxp.row(align=True)
            scope_row.prop(props, "presets_scope", expand=True)

            if props.presets_scope == "LOCAL":
                row = boxp.row()
                row.template_list(
                    "SKV_UL_presets",
                    "",
                    key_data,
                    "skv_presets",
                    key_data,
                    "skv_preset_index",
                    rows=4,
                )
                col = row.column(align=True)
                col.operator("skv.preset_add_empty", icon="ADD", text="")
                col.operator("skv.preset_remove", icon="REMOVE", text="")
                col.separator()
                col.operator("skv.preset_rename", icon="GREASEPENCIL", text="")

                preset = get_active_preset(key_data)
                if preset:
                    boxp.separator()
                    boxp.label(text="Preset Keys")
                    rows = min(10, max(3, len(preset.items))) if preset.items else 3
                    boxp.template_list(
                        "SKV_UL_preset_key_sliders",
                        "",
                        preset,
                        "items",
                        preset,
                        "items_index",
                        rows=rows,
                    )

            else:
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
                    boxp.separator()
                    boxp.label(text="Global Preset Keys")
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

    # Global presets storage on Scene
    bpy.types.Scene.skv_global_presets = CollectionProperty(type=presets.SKV_GlobalPreset)
    bpy.types.Scene.skv_global_preset_index = IntProperty(name="Global Preset Index", default=0, min=0)

    # Transfer-to dialog storage (Scene-level datablock properties support eyedropper).
    bpy.types.Scene.skv_transfer_source_name = StringProperty(options={"SKIP_SAVE"})
    bpy.types.Scene.skv_transfer_target = PointerProperty(type=bpy.types.Object, poll=_poll_transfer_target)

    bpy.types.Key.skv_groups = CollectionProperty(type=groups.SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(name="Group Index", default=0, min=0)

    bpy.types.Key.skv_selected = CollectionProperty(type=groups.SKV_SelectedName)
    bpy.types.Key.skv_key_groups = CollectionProperty(type=groups.SKV_KeyGroupEntry)

    bpy.types.Key.skv_presets = CollectionProperty(type=presets.SKV_Preset)
    bpy.types.Key.skv_preset_index = IntProperty(name="Preset Index", default=0, min=0)

    bpy.types.Object.skv_mesh_data_transfer = PointerProperty(type=meshDataTransfer.SKV_MeshDataSettings)

    _ensure_handler_installed()
    _auto_process_active_object(bpy.context.scene)


def unregister():
    _ensure_handler_removed()

    del bpy.types.Object.skv_mesh_data_transfer

    del bpy.types.Key.skv_preset_index
    del bpy.types.Key.skv_presets
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