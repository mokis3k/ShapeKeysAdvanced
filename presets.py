import bpy
from bpy.types import Operator, PropertyGroup, UIList, Menu
from bpy.props import (
    IntProperty,
    FloatProperty,
    StringProperty,
    CollectionProperty,
)

from .common import (
    get_active_object,
    get_shape_key_data,
    has_group_storage,
    is_initialized,
    enum_groups_for_active_object,
    tag_redraw_view3d,
    clear_selection_ui,
    ensure_init_setup_write,
    kd_selected_set,
    _is_basis_name,
    preset_apply,
    preset_value_update,
    get_active_preset,
)


# -----------------------------
# Data Model (presets)
# -----------------------------
class SKV_PresetItem(PropertyGroup):
    name: StringProperty(name="Shape Key", default="")
    max_value: FloatProperty(name="Max", default=1.0)


class SKV_Preset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Preset")
    value: FloatProperty(
        name="Value",
        default=0.0,
        min=0.0,
        max=1.0,
        update=preset_value_update,
    )
    items: CollectionProperty(type=SKV_PresetItem)
    items_index: IntProperty(name="Items Index", default=-1, min=-1)


# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_Presets(UIList):
    bl_idname = "SKV_UL_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)
        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

        # Capture Max button per preset row
        op = row.operator("skv.preset_capture_max_index", text="", icon="COPYDOWN", emboss=True)
        op.preset_index = index


class SKV_UL_PresetItems(UIList):
    bl_idname = "SKV_UL_preset_items"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        split = layout.split(factor=0.99, align=True)
        left = split.row(align=True)
        left.alignment = "LEFT"
        op = left.operator("skv.preset_focus_key", text=item.name, icon="SHAPEKEY_DATA", emboss=False)
        op.key_name = item.name
        split.separator()


class SKV_UL_PresetKeySliders(UIList):
    bl_idname = "SKV_UL_preset_key_sliders"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # data is SKV_Preset (PropertyGroup). Its id_data is bpy.types.Key datablock.
        preset = data
        key_data = getattr(preset, "id_data", None)
        if not key_data or not getattr(key_data, "key_blocks", None):
            layout.label(text=item.name)
            return

        kb = key_data.key_blocks.get(item.name) if item.name else None
        if kb is None:
            layout.label(text=item.name, icon="ERROR")
            return

        # Slider controls actual Shape Key value
        layout.prop(kb, "value", text=item.name, slider=True)


# -----------------------------
# Menus
# -----------------------------
class SKV_MT_AddToPreset(Menu):
    bl_label = "Add to preset"
    bl_idname = "SKV_MT_add_to_preset"

    def draw(self, context):
        layout = self.layout
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not hasattr(key_data, "skv_presets") or not key_data.skv_presets:
            layout.label(text="No presets")
            return

        for i, p in enumerate(key_data.skv_presets):
            op = layout.operator("skv.add_selected_to_preset", text=p.name, icon="PRESET")
            op.preset_index = i


# -----------------------------
# Operators
# -----------------------------
class SKV_OT_PresetFocusKey(Operator):
    bl_idname = "skv.preset_focus_key"
    bl_label = "Focus Preset Key"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        # Switch group to the one containing this shape key
        from .common import kd_get_group
        grp = kd_get_group(key_data, self.key_name)
        if has_group_storage(key_data):
            for i, g in enumerate(key_data.skv_groups):
                if g.name == grp:
                    key_data.skv_group_index = i
                    break

        # Exact focus without substring search:
        # 1) Set search empty
        # 2) Set active index to exact key index
        if hasattr(context.scene, "skv_props"):
            context.scene.skv_props.search = ""
            try:
                idx = key_data.key_blocks.keys().index(self.key_name)
            except Exception:
                idx = -1
            context.scene.skv_props.keys_index = idx

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetAddFromSelected(Operator):
    bl_idname = "skv.preset_add_from_selected"
    bl_label = "Create Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="New Preset")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}
        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        selected = kd_selected_set(key_data)
        if not selected:
            self.report({"INFO"}, "Select shape keys first (Select mode).")
            return {"CANCELLED"}

        name = self.name.strip()
        if not name:
            self.report({"WARNING"}, "Preset name is empty.")
            return {"CANCELLED"}

        existing = {p.name for p in key_data.skv_presets}
        base = name
        suffix = 1
        while name in existing:
            suffix += 1
            name = f"{base} {suffix}"

        preset = key_data.skv_presets.add()
        preset.name = name
        preset.items.clear()

        kb_map = key_data.key_blocks
        added = 0
        for kname in selected:
            if _is_basis_name(key_data, kname):
                continue
            kb = kb_map.get(kname)
            if not kb:
                continue
            it = preset.items.add()
            it.name = kname
            try:
                it.max_value = float(kb.value)
            except Exception:
                it.max_value = 0.0
            added += 1

        if added == 0:
            key_data.skv_presets.remove(len(key_data.skv_presets) - 1)
            self.report({"INFO"}, "No valid shape keys selected for preset.")
            return {"CANCELLED"}

        key_data.skv_preset_index = len(key_data.skv_presets) - 1
        preset.value = 1.0  # triggers update -> apply

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetAddEmpty(Operator):
    bl_idname = "skv.preset_add_empty"
    bl_label = "Create Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="New Preset")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        name = self.name.strip()
        if not name:
            self.report({"WARNING"}, "Preset name is empty.")
            return {"CANCELLED"}

        existing = {p.name for p in key_data.skv_presets}
        base = name
        suffix = 1
        while name in existing:
            suffix += 1
            name = f"{base} {suffix}"

        preset = key_data.skv_presets.add()
        preset.name = name
        preset.items.clear()
        preset.value = 0.0

        key_data.skv_preset_index = len(key_data.skv_presets) - 1

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetRemove(Operator):
    bl_idname = "skv.preset_remove"
    bl_label = "Remove Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not hasattr(key_data, "skv_presets"):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        idx = int(key_data.skv_preset_index)
        if not (0 <= idx < len(key_data.skv_presets)):
            return {"CANCELLED"}

        key_data.skv_presets.remove(idx)
        if key_data.skv_preset_index >= len(key_data.skv_presets):
            key_data.skv_preset_index = max(0, len(key_data.skv_presets) - 1)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetRename(Operator):
    bl_idname = "skv.preset_rename"
    bl_label = "Rename Preset"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not hasattr(key_data, "skv_presets"):
            return {"CANCELLED"}
        preset = get_active_preset(key_data)
        if not preset:
            return {"CANCELLED"}
        self.new_name = preset.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data:
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        preset = get_active_preset(key_data)
        if not preset:
            return {"CANCELLED"}

        nn = self.new_name.strip()
        if not nn:
            self.report({"WARNING"}, "Preset name is empty.")
            return {"CANCELLED"}

        existing = {p.name for p in key_data.skv_presets if p != preset}
        base = nn
        suffix = 1
        while nn in existing:
            suffix += 1
            nn = f"{base} {suffix}"

        preset.name = nn
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetCaptureMax(Operator):
    bl_idname = "skv.preset_capture_max"
    bl_label = "Capture Max"
    bl_description = "Overwrite preset maxima from current shape key values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        preset = get_active_preset(key_data)
        if not preset:
            self.report({"INFO"}, "No active preset.")
            return {"CANCELLED"}

        kb_map = key_data.key_blocks
        changed = 0
        for it in preset.items:
            kb = kb_map.get(it.name)
            if not kb:
                continue
            try:
                it.max_value = float(kb.value)
                changed += 1
            except Exception:
                pass

        if changed == 0:
            self.report({"INFO"}, "Nothing captured.")
            return {"CANCELLED"}

        preset_apply(preset, context)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetCaptureMaxIndex(Operator):
    bl_idname = "skv.preset_capture_max_index"
    bl_label = "Capture Max"
    bl_description = "Overwrite preset maxima from current shape key values (for a specific preset)"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=-1, min=-1)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        idx = int(self.preset_index)
        if idx < 0 or idx >= len(key_data.skv_presets):
            self.report({"INFO"}, "Invalid preset.")
            return {"CANCELLED"}

        key_data.skv_preset_index = idx
        return bpy.ops.skv.preset_capture_max()


class SKV_OT_AddSelectedToPreset(Operator):
    bl_idname = "skv.add_selected_to_preset"
    bl_label = "Add Selected To Preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0, min=0)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not hasattr(key_data, "skv_presets") or not key_data.skv_presets:
            self.report({"INFO"}, "No presets.")
            return {"CANCELLED"}

        if self.preset_index < 0 or self.preset_index >= len(key_data.skv_presets):
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_selected_set(key_data)
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        preset = key_data.skv_presets[self.preset_index]
        existing = {it.name for it in preset.items if it.name}

        added = 0
        kb_map = key_data.key_blocks
        for kname in selected:
            if not kname or kname in existing:
                continue
            if _is_basis_name(key_data, kname):
                continue
            kb = kb_map.get(kname)
            if not kb:
                continue
            it = preset.items.add()
            it.name = kname
            try:
                it.max_value = float(kb.value)
            except Exception:
                it.max_value = 0.0
            existing.add(kname)
            added += 1

        if added == 0:
            self.report({"INFO"}, "Nothing added (already present or invalid).")
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES = (
    SKV_PresetItem,
    SKV_Preset,
    SKV_UL_Presets,
    SKV_UL_PresetItems,
    SKV_UL_PresetKeySliders,
    SKV_MT_AddToPreset,
    SKV_OT_PresetFocusKey,
    SKV_OT_PresetAddFromSelected,
    SKV_OT_PresetAddEmpty,
    SKV_OT_PresetRemove,
    SKV_OT_PresetRename,
    SKV_OT_PresetCaptureMax,
    SKV_OT_PresetCaptureMaxIndex,
    SKV_OT_AddSelectedToPreset,
)
