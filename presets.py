# presets.py
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
# Global preset apply
# -----------------------------
_GLOBAL_PRESET_APPLY_GUARD = False


def global_preset_apply(preset, context) -> None:
    global _GLOBAL_PRESET_APPLY_GUARD
    if _GLOBAL_PRESET_APPLY_GUARD:
        return

    _GLOBAL_PRESET_APPLY_GUARD = True
    try:
        factor = float(preset.value)
        for it in preset.items:
            obj_name = (it.object_name or "").strip()
            key_name = (it.key_name or "").strip()
            if not obj_name or not key_name:
                continue

            obj = bpy.data.objects.get(obj_name)
            if not obj or getattr(obj, "type", None) != "MESH":
                continue

            key_data = get_shape_key_data(obj)
            if not key_data or not getattr(key_data, "key_blocks", None):
                continue
            if getattr(key_data, "library", None) is not None:
                continue

            kb = key_data.key_blocks.get(key_name)
            if not kb:
                continue

            try:
                kb.value = factor * float(it.max_value)
            except Exception:
                pass
    finally:
        _GLOBAL_PRESET_APPLY_GUARD = False

    tag_redraw_view3d(context)


def global_preset_value_update(self, context):
    global_preset_apply(self, context)


def get_active_global_preset(scene):
    if not scene or not hasattr(scene, "skv_global_presets") or not hasattr(scene, "skv_global_preset_index"):
        return None
    idx = int(scene.skv_global_preset_index)
    if 0 <= idx < len(scene.skv_global_presets):
        return scene.skv_global_presets[idx]
    return None


# -----------------------------
# Data Model (local presets)
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
# Data Model (global presets)
# -----------------------------
class SKV_GlobalPresetItem(PropertyGroup):
    object_name: StringProperty(name="Object", default="")
    key_name: StringProperty(name="Shape Key", default="")
    max_value: FloatProperty(name="Max", default=1.0)


class SKV_GlobalPreset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Global Preset")
    value: FloatProperty(
        name="Value",
        default=0.0,
        min=0.0,
        max=1.0,
        update=global_preset_value_update,
    )
    items: CollectionProperty(type=SKV_GlobalPresetItem)
    items_index: IntProperty(name="Items Index", default=-1, min=-1)


# -----------------------------
# UI Lists (local)
# -----------------------------
class SKV_UL_Presets(UIList):
    bl_idname = "SKV_UL_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)
        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

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
        preset = data
        key_data = getattr(preset, "id_data", None)
        if not key_data or not getattr(key_data, "key_blocks", None):
            layout.label(text=item.name)
            return

        kb = key_data.key_blocks.get(item.name) if item.name else None
        if kb is None:
            layout.label(text=item.name, icon="ERROR")
            return

        layout.prop(kb, "value", text=item.name, slider=True)


# -----------------------------
# UI Lists (global)
# -----------------------------
class SKV_UL_GlobalPresets(UIList):
    bl_idname = "SKV_UL_global_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)
        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

        op = row.operator("skv.global_preset_capture_max_index", text="", icon="COPYDOWN", emboss=True)
        op.preset_index = index


class SKV_UL_GlobalPresetKeySliders(UIList):
    bl_idname = "SKV_UL_global_preset_key_sliders"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj_name = (item.object_name or "").strip()
        key_name = (item.key_name or "").strip()
        label = f"{obj_name}: {key_name}" if obj_name and key_name else "Invalid item"

        obj = bpy.data.objects.get(obj_name) if obj_name else None
        key_data = get_shape_key_data(obj) if obj else None
        kb = key_data.key_blocks.get(key_name) if (key_data and key_name) else None

        if not obj or not key_data or not kb:
            layout.label(text=label, icon="ERROR")
            return

        layout.prop(kb, "value", text=label, slider=True)


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


class SKV_MT_AddToGlobalPreset(Menu):
    bl_label = "Add to global preset"
    bl_idname = "SKV_MT_add_to_global_preset"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        if not hasattr(scene, "skv_global_presets") or not scene.skv_global_presets:
            layout.label(text="No global presets")
            return

        for i, p in enumerate(scene.skv_global_presets):
            op = layout.operator("skv.add_selected_to_global_preset", text=p.name, icon="PRESET")
            op.preset_index = i


# -----------------------------
# Operators (local)
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

        from .common import kd_get_group

        grp = kd_get_group(key_data, self.key_name)
        if has_group_storage(key_data):
            for i, g in enumerate(key_data.skv_groups):
                if g.name == grp:
                    key_data.skv_group_index = i
                    break

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
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            props.last_affix_pending = False
        else:
            self.name = "New Preset"
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
        preset.value = 1.0

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_PresetAddEmpty(Operator):
    bl_idname = "skv.preset_add_empty"
    bl_label = "Create Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="New Preset")

    def invoke(self, context, event):
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            props.last_affix_pending = False
        else:
            self.name = "New Preset"
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


# -----------------------------
# Operators (global)
# -----------------------------
class SKV_OT_GlobalPresetAddEmpty(Operator):
    bl_idname = "skv.global_preset_add_empty"
    bl_label = "Create Global Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="New Global Preset")

    def invoke(self, context, event):
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            props.last_affix_pending = False
        else:
            self.name = "New Global Preset"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        name = self.name.strip()
        if not name:
            self.report({"WARNING"}, "Preset name is empty.")
            return {"CANCELLED"}

        existing = {p.name for p in scene.skv_global_presets}
        base = name
        suffix = 1
        while name in existing:
            suffix += 1
            name = f"{base} {suffix}"

        preset = scene.skv_global_presets.add()
        preset.name = name
        preset.items.clear()
        preset.value = 0.0

        scene.skv_global_preset_index = len(scene.skv_global_presets) - 1
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetRemove(Operator):
    bl_idname = "skv.global_preset_remove"
    bl_label = "Remove Global Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        idx = int(scene.skv_global_preset_index)
        if not (0 <= idx < len(scene.skv_global_presets)):
            return {"CANCELLED"}

        scene.skv_global_presets.remove(idx)
        if scene.skv_global_preset_index >= len(scene.skv_global_presets):
            scene.skv_global_preset_index = max(0, len(scene.skv_global_presets) - 1)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetRename(Operator):
    bl_idname = "skv.global_preset_rename"
    bl_label = "Rename Global Preset"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        preset = get_active_global_preset(context.scene)
        if not preset:
            return {"CANCELLED"}
        self.new_name = preset.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        preset = get_active_global_preset(scene)
        if not preset:
            return {"CANCELLED"}

        nn = self.new_name.strip()
        if not nn:
            self.report({"WARNING"}, "Preset name is empty.")
            return {"CANCELLED"}

        existing = {p.name for p in scene.skv_global_presets if p != preset}
        base = nn
        suffix = 1
        while nn in existing:
            suffix += 1
            nn = f"{base} {suffix}"

        preset.name = nn
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetCaptureMax(Operator):
    bl_idname = "skv.global_preset_capture_max"
    bl_label = "Capture Max (Global)"
    bl_description = "Overwrite global preset maxima from current shape key values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        if not preset:
            self.report({"INFO"}, "No active global preset.")
            return {"CANCELLED"}

        changed = 0
        for it in preset.items:
            obj = bpy.data.objects.get((it.object_name or "").strip())
            if not obj or getattr(obj, "type", None) != "MESH":
                continue
            key_data = get_shape_key_data(obj)
            if not key_data or not getattr(key_data, "key_blocks", None):
                continue
            if getattr(key_data, "library", None) is not None:
                continue
            kb = key_data.key_blocks.get((it.key_name or "").strip())
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

        global_preset_apply(preset, context)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetCaptureMaxIndex(Operator):
    bl_idname = "skv.global_preset_capture_max_index"
    bl_label = "Capture Max (Global)"
    bl_description = "Overwrite global preset maxima from current shape key values (for a specific global preset)"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=-1, min=-1)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            self.report({"INFO"}, "Invalid preset.")
            return {"CANCELLED"}

        scene.skv_global_preset_index = idx
        return bpy.ops.skv.global_preset_capture_max()


class SKV_OT_AddSelectedToGlobalPreset(Operator):
    bl_idname = "skv.add_selected_to_global_preset"
    bl_label = "Add Selected To Global Preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0, min=0)

    def execute(self, context):
        scene = context.scene
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        if self.preset_index < 0 or self.preset_index >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_selected_set(key_data)
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        preset = scene.skv_global_presets[self.preset_index]
        existing = {(it.object_name, it.key_name) for it in preset.items if it.object_name and it.key_name}

        kb_map = key_data.key_blocks
        added = 0
        for kname in selected:
            if not kname:
                continue
            if _is_basis_name(key_data, kname):
                continue
            key_pair = (obj.name, kname)
            if key_pair in existing:
                continue
            kb = kb_map.get(kname)
            if not kb:
                continue

            it = preset.items.add()
            it.object_name = obj.name
            it.key_name = kname
            try:
                it.max_value = float(kb.value)
            except Exception:
                it.max_value = 0.0

            existing.add(key_pair)
            added += 1

        if added == 0:
            self.report({"INFO"}, "Nothing added (already present or invalid).")
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetAddFromSelected(Operator):
    bl_idname = "skv.global_preset_add_from_selected"
    bl_label = "Create Global Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="New Global Preset")

    def invoke(self, context, event):
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            props.last_affix_pending = False
        else:
            self.name = "New Global Preset"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
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

        existing = {p.name for p in scene.skv_global_presets}
        base = name
        suffix = 1
        while name in existing:
            suffix += 1
            name = f"{base} {suffix}"

        preset = scene.skv_global_presets.add()
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
            it.object_name = obj.name
            it.key_name = kname
            try:
                it.max_value = float(kb.value)
            except Exception:
                it.max_value = 0.0
            added += 1

        if added == 0:
            scene.skv_global_presets.remove(len(scene.skv_global_presets) - 1)
            self.report({"INFO"}, "No valid shape keys selected for global preset.")
            return {"CANCELLED"}

        scene.skv_global_preset_index = len(scene.skv_global_presets) - 1
        preset.value = 1.0

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES = (
    # local
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
    # global
    SKV_GlobalPresetItem,
    SKV_GlobalPreset,
    SKV_UL_GlobalPresets,
    SKV_UL_GlobalPresetKeySliders,
    SKV_MT_AddToGlobalPreset,
    SKV_OT_GlobalPresetAddEmpty,
    SKV_OT_GlobalPresetAddFromSelected,
    SKV_OT_GlobalPresetRemove,
    SKV_OT_GlobalPresetRename,
    SKV_OT_GlobalPresetCaptureMax,
    SKV_OT_GlobalPresetCaptureMaxIndex,
    SKV_OT_AddSelectedToGlobalPreset,
)