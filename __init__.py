bl_info = {
    "name": "Shape Keys Viewer",
    "author": "xtafr001",
    "version": (0, 5, 5),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > ShapeKeys",
    "description": "Shape keys grouping, selection tools, presets, and shape keys transfer.",
    "category": "Object",
}

import bpy
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


class SKV_OT_InitRescan(Operator):
    bl_idname = "skv.init_rescan"
    bl_label = "Scan/Rescan"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        from .common import ensure_init_setup_write

        obj = context.active_object
        if not obj or obj.type != "MESH":
            self.report({"WARNING"}, "Active object is not a mesh.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data:
            self.report({"WARNING"}, "No shape keys datablock on this object.")
            return {"CANCELLED"}

        if not has_group_storage(key_data):
            self.report({"ERROR"}, "Group storage is not available on this Key datablock.")
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_Props(PropertyGroup):
    keys_index: IntProperty(name="Keys Index", default=-1, min=-1)
    search: StringProperty(name="Search", default="")
    show_select: BoolProperty(name="Select", default=False, update=show_select_update)

    groups_open: BoolProperty(name="Groups", default=True)
    keys_open: BoolProperty(name="Keys", default=True)
    presets_open: BoolProperty(name="Presets", default=False)
    transfer_open: BoolProperty(name="Shape Keys Transfer", default=False)

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

    last_affix_name: StringProperty(name="Last Affix Name", default="")
    last_affix_pending: BoolProperty(name="Last Affix Pending", default=False)


class SKV_PT_ShapeKeysPanel(Panel):
    bl_label = "Shape Keys Viewer"
    bl_idname = "SKV_PT_shape_keys_viewer_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ShapeKeys"

    def draw(self, context):
        layout = self.layout
        props = context.scene.skv_props

        obj = context.active_object

        box_ctx = layout.box()
        box_ctx.label(text="OBJECT", icon="OBJECT_DATA")

        if not obj or obj.type != "MESH":
            box_ctx.label(text="No active mesh object")
            return

        row2 = box_ctx.row(align=True)
        row2.label(text=obj.name, icon="MESH_DATA")

        key_data = get_shape_key_data(obj)
        can_groups = bool(key_data) and has_group_storage(key_data)
        initialized = is_initialized(key_data) if can_groups else False

        if initialized:
            row2.operator("skv.init_rescan", text="", icon="FILE_REFRESH")

        current_group = get_selected_group_name(key_data) if initialized else INIT_GROUP_NAME

        if can_groups:
            if not initialized:
                layout.separator()
                layout.operator("skv.init_rescan", text="Scan", icon="FILE_REFRESH")
            else:
                box_ws = layout.box()
                box_ws.label(text="GROUP WORKSPACE", icon="GROUP")

                box_groups = box_ws.box()
                headg = box_groups.row(align=True)
                icong = "TRIA_DOWN" if props.groups_open else "TRIA_RIGHT"
                headg.prop(props, "groups_open", text="", emboss=False, icon=icong)
                headg.label(text="Groups")

                if props.groups_open:
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

                box_keys = box_ws.box()
                head = box_keys.row(align=True)
                icon = "TRIA_DOWN" if props.keys_open else "TRIA_RIGHT"
                head.prop(props, "keys_open", text="", emboss=False, icon=icon)
                head.label(text=f"Keys in '{current_group}'")

                if props.keys_open:
                    group_count = count_keys_in_group(key_data, current_group)

                    if group_count > 0:
                        row = box_keys.row(align=True)
                        row.prop(props, "search", text="", icon="VIEWZOOM")
                        row.operator("skv.search_clear", text="", icon="X")

                        row = box_keys.row(align=True)
                        row.prop(props, "show_select", text="Select", toggle=True)
                        row.menu("SKV_MT_select_actions", text="", icon="DOWNARROW_HLT")

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

                boxp = layout.box()
                headp = boxp.row(align=True)
                iconp = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
                headp.prop(props, "presets_open", text="", emboss=False, icon=iconp)
                headp.label(text="PRESETS")

                if props.presets_open:
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
            layout.box().label(text="Groups/Presets require an object with Shape Keys.", icon="INFO")

        boxt = layout.box()
        headt = boxt.row(align=True)
        icont = "TRIA_DOWN" if props.transfer_open else "TRIA_RIGHT"
        headt.prop(props, "transfer_open", text="", emboss=False, icon=icont)
        headt.label(text="SHAPE KEYS TRANSFER")

        if props.transfer_open:
            meshDataTransfer.draw_transfer_ui(boxt, context)


_LOCAL_CLASSES = (
    SKV_OT_SearchClear,
    SKV_OT_InitRescan,
    SKV_Props,
    SKV_PT_ShapeKeysPanel,
)

_ALL_CLASSES = _LOCAL_CLASSES + groups.CLASSES + presets.CLASSES + meshDataTransfer.CLASSES


def register():
    for cls in _ALL_CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.skv_props = PointerProperty(type=SKV_Props)

    bpy.types.Key.skv_groups = CollectionProperty(type=groups.SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(name="Group Index", default=0, min=0)

    bpy.types.Key.skv_selected = CollectionProperty(type=groups.SKV_SelectedName)
    bpy.types.Key.skv_key_groups = CollectionProperty(type=groups.SKV_KeyGroupEntry)

    bpy.types.Key.skv_presets = CollectionProperty(type=presets.SKV_Preset)
    bpy.types.Key.skv_preset_index = IntProperty(name="Preset Index", default=0, min=0)

    bpy.types.Object.skv_shape_keys_transfer = PointerProperty(
        type=meshDataTransfer.SKV_ShapeKeysTransferSettings
    )


def unregister():
    del bpy.types.Object.skv_shape_keys_transfer

    del bpy.types.Key.skv_preset_index
    del bpy.types.Key.skv_presets
    del bpy.types.Key.skv_key_groups
    del bpy.types.Key.skv_selected
    del bpy.types.Key.skv_groups
    del bpy.types.Key.skv_group_index
    del bpy.types.Scene.skv_props

    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
