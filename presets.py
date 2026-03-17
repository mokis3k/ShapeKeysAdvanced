import bpy
from bpy.types import Operator, PropertyGroup, UIList, Menu
from bpy.props import IntProperty, FloatProperty, StringProperty, CollectionProperty

from .common import (
    get_active_object,
    get_shape_key_data,
    is_initialized,
    tag_redraw_view3d,
    clear_selection_ui,
    ensure_init_setup_write,
    kd_selected_set,
    _is_basis_name,
    InternalValueChangeGuard,
)

_PRESET_ITEM_SYNC_GUARD = False
_GLOBAL_PRESET_APPLY_GUARD = False


def get_active_global_preset(scene):
    if not scene or not hasattr(scene, "skv_global_presets") or not hasattr(scene, "skv_global_preset_index"):
        return None
    idx = int(scene.skv_global_preset_index)
    if 0 <= idx < len(scene.skv_global_presets):
        return scene.skv_global_presets[idx]
    return None


def sync_preset_item_values(context, preset) -> None:
    global _PRESET_ITEM_SYNC_GUARD
    _PRESET_ITEM_SYNC_GUARD = True
    try:
        for it in preset.items:
            obj = bpy.data.objects.get(it.object_name) if it.object_name else None
            if not obj or getattr(obj, "type", None) != "MESH":
                continue
            key_data = get_shape_key_data(obj)
            if not key_data or not getattr(key_data, "key_blocks", None):
                continue
            kb = key_data.key_blocks.get(it.key_name)
            if not kb:
                continue
            try:
                it.value = float(kb.value)
            except Exception:
                pass
    finally:
        _PRESET_ITEM_SYNC_GUARD = False


def _preset_item_value_update(self, context):
    global _PRESET_ITEM_SYNC_GUARD
    if _PRESET_ITEM_SYNC_GUARD:
        return

    obj_name = (self.object_name or "").strip()
    key_name = (self.key_name or "").strip()
    if not obj_name or not key_name:
        return

    obj = bpy.data.objects.get(obj_name)
    if not obj or getattr(obj, "type", None) != "MESH":
        return

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if getattr(key_data, "library", None) is not None:
        return

    kb = key_data.key_blocks.get(key_name)
    if not kb:
        return

    try:
        new_val = float(self.value)
    except Exception:
        return

    with InternalValueChangeGuard():
        try:
            kb.value = new_val
        except Exception:
            return

        try:
            if hasattr(key_data, "skv_key_defaults"):
                updated = False
                for d in key_data.skv_key_defaults:
                    if d.name == kb.name:
                        d.value = float(new_val)
                        updated = True
                        break
                if not updated:
                    d = key_data.skv_key_defaults.add()
                    d.name = kb.name
                    d.value = float(new_val)
        except Exception:
            pass

    tag_redraw_view3d(context)


def global_preset_apply(preset, context) -> None:
    global _GLOBAL_PRESET_APPLY_GUARD
    if _GLOBAL_PRESET_APPLY_GUARD:
        return

    _GLOBAL_PRESET_APPLY_GUARD = True
    try:
        factor = float(preset.value)
        for it in preset.items:
            obj = bpy.data.objects.get(it.object_name) if it.object_name else None
            if not obj or getattr(obj, "type", None) != "MESH":
                continue

            key_data = get_shape_key_data(obj)
            if not key_data or not getattr(key_data, "key_blocks", None):
                continue
            if getattr(key_data, "library", None) is not None:
                continue

            kb = key_data.key_blocks.get(it.key_name)
            if not kb:
                continue

            try:
                new_val = factor * float(it.max_value)
            except Exception:
                continue

            with InternalValueChangeGuard():
                try:
                    kb.value = new_val
                except Exception:
                    continue

                global _PRESET_ITEM_SYNC_GUARD
                _PRESET_ITEM_SYNC_GUARD = True
                try:
                    it.value = float(new_val)
                except Exception:
                    pass
                finally:
                    _PRESET_ITEM_SYNC_GUARD = False

                try:
                    if hasattr(key_data, "skv_key_defaults"):
                        updated = False
                        for d in key_data.skv_key_defaults:
                            if d.name == kb.name:
                                d.value = float(new_val)
                                updated = True
                                break
                        if not updated:
                            d = key_data.skv_key_defaults.add()
                            d.name = kb.name
                            d.value = float(new_val)
                except Exception:
                    pass
    finally:
        _GLOBAL_PRESET_APPLY_GUARD = False

    tag_redraw_view3d(context)


def global_preset_value_update(self, context):
    global_preset_apply(self, context)


def _autokf_get_entry(key_data, key_name: str, create: bool = False):
    if not key_data or not hasattr(key_data, "skv_auto_keyframes"):
        return None
    for it in key_data.skv_auto_keyframes:
        if it.name == key_name:
            return it
    if not create:
        return None
    it = key_data.skv_auto_keyframes.add()
    it.name = key_name
    return it


def _iter_preset_key_blocks(preset):
    for it in getattr(preset, "items", []):
        obj = bpy.data.objects.get(it.object_name) if it.object_name else None
        if not obj or getattr(obj, "type", None) != "MESH":
            continue

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            continue

        kb = key_data.key_blocks.get(it.key_name)
        if not kb:
            continue

        yield key_data, kb


def _preset_all_muted(preset) -> bool:
    found = False
    for _key_data, kb in _iter_preset_key_blocks(preset):
        found = True
        if not bool(getattr(kb, "mute", False)):
            return False
    return found and True


def _preset_all_autokey_enabled(preset) -> bool:
    found = False
    for key_data, kb in _iter_preset_key_blocks(preset):
        found = True
        entry = _autokf_get_entry(key_data, kb.name, create=False)
        if not (entry and bool(entry.enabled)):
            return False
    return found and True


class SKV_GlobalPresetItem(PropertyGroup):
    object_name: StringProperty(name="Object", default="")
    key_name: StringProperty(name="Shape Key", default="")
    max_value: FloatProperty(name="Max", default=1.0)

    value: FloatProperty(
        name="Value",
        default=0.0,
        min=-10.0,
        max=10.0,
        soft_min=0.0,
        soft_max=1.0,
        update=_preset_item_value_update,
    )


class SKV_GlobalPreset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Preset")
    value: FloatProperty(
        name="Value",
        default=0.0,
        min=0.0,
        max=1.0,
        update=global_preset_value_update,
    )
    items: CollectionProperty(type=SKV_GlobalPresetItem)
    items_index: IntProperty(name="Items Index", default=-1, min=-1)


class SKV_OT_preset_capture_max_index(Operator):
    bl_idname = "skv.preset_capture_max_index"
    bl_label = "Capture Max (Index)"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            return {"CANCELLED"}
        preset = scene.skv_global_presets[idx]

        for it in preset.items:
            obj = bpy.data.objects.get(it.object_name) if it.object_name else None
            if not obj:
                continue
            key_data = get_shape_key_data(obj)
            if not key_data or not getattr(key_data, "key_blocks", None):
                continue
            kb = key_data.key_blocks.get(it.key_name)
            if not kb:
                continue
            try:
                it.max_value = float(kb.value)
            except Exception:
                pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_preset_item_capture_max(Operator):
    bl_idname = "skv.preset_item_capture_max"
    bl_label = "Capture Max (Item)"
    bl_options = {"REGISTER", "UNDO"}

    item_index: IntProperty(name="Item Index", default=0)

    def execute(self, context):
        scene = context.scene
        preset = get_active_global_preset(scene)
        if not preset:
            return {"CANCELLED"}

        idx = int(self.item_index)
        if idx < 0 or idx >= len(preset.items):
            return {"CANCELLED"}

        it = preset.items[idx]

        obj = bpy.data.objects.get(it.object_name) if it.object_name else None
        if not obj or getattr(obj, "type", None) != "MESH":
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(it.key_name)
        if not kb:
            return {"CANCELLED"}

        try:
            it.max_value = float(kb.value)
        except Exception:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_preset_toggle_visibility(Operator):
    bl_idname = "skv.preset_toggle_visibility"
    bl_label = "Toggle Preset Visibility"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        preset = scene.skv_global_presets[idx]
        target_mute = not _preset_all_muted(preset)

        changed = False
        for _key_data, kb in _iter_preset_key_blocks(preset):
            try:
                kb.mute = target_mute
                changed = True
            except Exception:
                pass

        if not changed:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_preset_toggle_auto_keyframe(Operator):
    bl_idname = "skv.preset_toggle_auto_keyframe"
    bl_label = "Toggle Preset Auto Keyframe"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        preset = scene.skv_global_presets[idx]
        target_enabled = not _preset_all_autokey_enabled(preset)

        changed = False
        frame = int(context.scene.frame_current)
        for key_data, kb in _iter_preset_key_blocks(preset):
            if getattr(key_data, "library", None) is not None:
                continue
            entry = _autokf_get_entry(key_data, kb.name, create=True)
            if not entry:
                continue
            try:
                entry.enabled = target_enabled
                entry.last_frame = frame
                try:
                    entry.last_value = float(kb.value)
                except Exception:
                    entry.last_value = 0.0

                if target_enabled:
                    try:
                        key_data.keyframe_insert(data_path=f'key_blocks["{kb.name}"].value', frame=frame)
                    except Exception:
                        pass

                changed = True
            except Exception:
                pass

        if not changed:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_UL_global_presets(UIList):
    bl_idname = "SKV_UL_global_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)
        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

        vis_icon = "HIDE_ON" if _preset_all_muted(preset) else "HIDE_OFF"
        opv = row.operator("skv.preset_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.preset_index = index

        kf_icon = "KEYFRAME_HLT" if _preset_all_autokey_enabled(preset) else "KEYFRAME"
        opk = row.operator("skv.preset_toggle_auto_keyframe", text="", icon=kf_icon, emboss=False)
        opk.preset_index = index


class SKV_UL_global_preset_key_sliders(UIList):
    bl_idname = "SKV_UL_global_preset_key_sliders"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        it = item
        row = layout.row(align=True)

        label = f"{it.object_name}: {it.key_name}" if it.object_name else it.key_name
        row.label(text=label, icon="SHAPEKEY_DATA")
        row.prop(it, "value", text="", slider=True)

        op = row.operator("skv.preset_item_capture_max", text="", icon="COPYDOWN", emboss=True)
        op.item_index = index


class SKV_MT_add_to_preset(Menu):
    bl_label = "Add to preset"
    bl_idname = "SKV_MT_add_to_preset"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        if not hasattr(scene, "skv_global_presets") or len(scene.skv_global_presets) == 0:
            layout.label(text="No presets")
            return
        for i, p in enumerate(scene.skv_global_presets):
            op = layout.operator("skv.add_selected_to_preset", text=p.name, icon="PRESET")
            op.preset_index = i


class SKV_OT_add_selected_to_preset(Operator):
    bl_idname = "skv.add_selected_to_preset"
    bl_label = "Add Selected To Preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0, min=0)

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not getattr(key_data, "key_blocks", None):
            return False
        if getattr(key_data, "library", None) is not None:
            return False
        scene = context.scene
        if not hasattr(scene, "skv_global_presets") or len(scene.skv_global_presets) == 0:
            return False
        return any(not _is_basis_name(key_data, n) for n in kd_selected_set(key_data))

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
        selected = [n for n in selected if n and not _is_basis_name(key_data, n)]
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        preset = scene.skv_global_presets[self.preset_index]
        existing = {(it.object_name, it.key_name) for it in preset.items if it.object_name and it.key_name}

        kb_map = key_data.key_blocks
        added = 0
        for kname in selected:
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
                global _PRESET_ITEM_SYNC_GUARD
                _PRESET_ITEM_SYNC_GUARD = True
                try:
                    it.value = float(kb.value)
                finally:
                    _PRESET_ITEM_SYNC_GUARD = False
            except Exception:
                it.max_value = 0.0
                _PRESET_ITEM_SYNC_GUARD = True
                try:
                    it.value = 0.0
                finally:
                    _PRESET_ITEM_SYNC_GUARD = False

            existing.add(key_pair)
            added += 1

        if added == 0:
            self.report({"INFO"}, "Nothing added (already present or invalid).")
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetAddEmpty(Operator):
    bl_idname = "skv.global_preset_add_empty"
    bl_label = "Add Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        preset = scene.skv_global_presets.add()
        preset.name = "Preset"
        preset.value = 0.0
        scene.skv_global_preset_index = max(0, len(scene.skv_global_presets) - 1)

        try:
            context.scene.skv_props.presets_open = True
        except Exception:
            pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetRemove(Operator):
    bl_idname = "skv.global_preset_remove"
    bl_label = "Remove Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        idx = int(getattr(scene, "skv_global_preset_index", 0))
        if 0 <= idx < len(scene.skv_global_presets):
            scene.skv_global_presets.remove(idx)
            scene.skv_global_preset_index = max(0, min(idx, len(scene.skv_global_presets) - 1))
            tag_redraw_view3d(context)
            return {"FINISHED"}
        return {"CANCELLED"}


class SKV_OT_GlobalPresetRename(Operator):
    bl_idname = "skv.global_preset_rename"
    bl_label = "Rename Preset"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="New Name", default="Preset")

    def invoke(self, context, event):
        scene = context.scene
        idx = int(getattr(scene, "skv_global_preset_index", 0))
        if 0 <= idx < len(scene.skv_global_presets):
            self.new_name = scene.skv_global_presets[idx].name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        idx = int(getattr(scene, "skv_global_preset_index", 0))
        if 0 <= idx < len(scene.skv_global_presets):
            scene.skv_global_presets[idx].name = (self.new_name or "Preset").strip() or "Preset"
            tag_redraw_view3d(context)
            return {"FINISHED"}
        return {"CANCELLED"}


class SKV_OT_GlobalPresetAddFromSelected(Operator):
    bl_idname = "skv.global_preset_add_from_selected"
    bl_label = "Create Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="Preset")

    def invoke(self, context, event):
        self.name = "Preset"
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

        ensure_init_setup_write(obj)

        name = (self.name or "Preset").strip() or "Preset"
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
                _PRESET_ITEM_SYNC_GUARD = True
                try:
                    it.value = float(kb.value)
                finally:
                    _PRESET_ITEM_SYNC_GUARD = False
            except Exception:
                it.max_value = 0.0
                _PRESET_ITEM_SYNC_GUARD = True
                try:
                    it.value = 0.0
                finally:
                    _PRESET_ITEM_SYNC_GUARD = False
            added += 1

        if added == 0:
            scene.skv_global_presets.remove(len(scene.skv_global_presets) - 1)
            self.report({"INFO"}, "No valid shape keys selected for preset.")
            return {"CANCELLED"}

        scene.skv_global_preset_index = len(scene.skv_global_presets) - 1
        preset.value = 1.0

        try:
            context.scene.skv_props.presets_open = True
        except Exception:
            pass

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES = (
    SKV_GlobalPresetItem,
    SKV_GlobalPreset,
    SKV_OT_preset_capture_max_index,
    SKV_OT_preset_item_capture_max,
    SKV_OT_preset_toggle_visibility,
    SKV_OT_preset_toggle_auto_keyframe,
    SKV_UL_global_presets,
    SKV_UL_global_preset_key_sliders,
    SKV_MT_add_to_preset,
    SKV_OT_add_selected_to_preset,
    SKV_OT_GlobalPresetAddEmpty,
    SKV_OT_GlobalPresetRemove,
    SKV_OT_GlobalPresetRename,
    SKV_OT_GlobalPresetAddFromSelected,
)