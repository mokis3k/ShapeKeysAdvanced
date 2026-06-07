import bpy
from bpy.types import Operator, PropertyGroup, UIList, Menu
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    FloatProperty,
    CollectionProperty,
)

from .common import (
    INIT_GROUP_NAME,
    get_active_object,
    get_shape_key_data,
    has_group_storage,
    group_names,
    get_fallback_group_name,
    is_initialized,
    get_selected_group_name,
    enum_groups_for_active_object,
    tag_redraw_view3d,
    parse_tokens,
    clear_selection_ui,
    kd_get_group,
    kd_set_group,
    kd_selected_set,
    kd_is_selected,
    kd_set_selected,
    kd_clear_selected,
    count_keys_in_group,
    ensure_init_setup_write,
    InternalValueChangeGuard,
    skv_shape_key_list_sync_active,
    skv_sync_shape_key_list_indices,
)


def _make_unique_name(base_name: str, existing_names) -> str:
    base = (base_name or "").strip()
    if not base:
        base = "New Group"

    existing = set(existing_names or [])
    if base not in existing:
        return base

    index = 1
    while f"{base} {index}" in existing:
        index += 1
    return f"{base} {index}"


def _make_transfer_group_name(source_group_name: str, source_obj, target_key_data) -> str:
    # Build deterministic target group name for transferred shape keys.
    # Reuse it if it already exists on target.
    base_name = (source_group_name or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME
    source_name = getattr(source_obj, "name", "") if source_obj else ""
    transfer_name = f"{base_name} ({source_name})" if source_name else base_name

    existing = set(group_names(target_key_data))
    if transfer_name in existing:
        return transfer_name

    return _make_unique_name(transfer_name, existing)


def _ensure_group_exists(key_data, group_name: str) -> bool:
    # Ensure that a group with the given name exists in target key data.
    if not key_data or not has_group_storage(key_data):
        return False

    group_name = (group_name or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME

    for g in key_data.skv_groups:
        if g.name == group_name:
            return True

    try:
        g = key_data.skv_groups.add()
        g.name = group_name
        try:
            g.last_name = group_name
        except Exception:
            pass
        return True
    except Exception:
        return False


def inherit_transferred_groups(source_obj, target_obj, key_names) -> int:
    # Copy source group mapping to target for transferred shape keys.
    if not source_obj or not target_obj or source_obj == target_obj:
        return 0
    if getattr(source_obj, "type", None) != "MESH" or getattr(target_obj, "type", None) != "MESH":
        return 0

    names = [n for n in (key_names or []) if n and n != "Basis"]
    if not names:
        return 0

    source_key_data = get_shape_key_data(source_obj)
    target_key_data = get_shape_key_data(target_obj)

    if not source_key_data or not target_key_data:
        return 0
    if not getattr(source_key_data, "key_blocks", None) or not getattr(target_key_data, "key_blocks", None):
        return 0
    if not has_group_storage(source_key_data) or not has_group_storage(target_key_data):
        return 0
    if getattr(target_key_data, "library", None) is not None:
        return 0

    ensure_init_setup_write(target_obj)

    source_to_target_group = {}
    changed = 0

    for key_name in names:
        if not source_key_data.key_blocks.get(key_name):
            continue
        if not target_key_data.key_blocks.get(key_name):
            continue

        source_group = kd_get_group(source_key_data, key_name)

        if source_group not in source_to_target_group:
            target_group = _make_transfer_group_name(source_group, source_obj, target_key_data)
            if not _ensure_group_exists(target_key_data, target_group):
                continue
            source_to_target_group[source_group] = target_group

        kd_set_group(target_key_data, key_name, source_to_target_group[source_group])
        changed += 1

    ensure_init_setup_write(target_obj)
    _remove_empty_transfer_fallback_group(target_key_data)
    return changed


def _remove_empty_transfer_fallback_group(target_key_data) -> None:
    # Remove the auto-created fallback group if transferred groups already exist
    # and the fallback group has no non-Basis shape keys assigned to it.
    if not target_key_data or not has_group_storage(target_key_data):
        return

    groups = getattr(target_key_data, "skv_groups", None)
    if not groups or len(groups) <= 1:
        return

    fallback_index = -1
    for i, g in enumerate(groups):
        if g.name == INIT_GROUP_NAME:
            fallback_index = i
            break

    if fallback_index < 0:
        return

    for kb in getattr(target_key_data, "key_blocks", []) or []:
        if kb.name == "Basis":
            continue
        if kd_get_group(target_key_data, kb.name) == INIT_GROUP_NAME:
            return

    try:
        groups.remove(fallback_index)
    except Exception:
        return

    try:
        if target_key_data.skv_group_index >= len(groups):
            target_key_data.skv_group_index = max(0, len(groups) - 1)
    except Exception:
        pass

    try:
        if hasattr(target_key_data, "skv_last_group_index"):
            target_key_data.skv_last_group_index = int(target_key_data.skv_group_index)
    except Exception:
        pass


def _remove_existing_target_shape_keys(target_obj, key_names) -> int:
    # Remove target shape keys with names matching transferred keys.
    if not target_obj or getattr(target_obj, "type", None) != "MESH":
        return 0

    target_key_data = get_shape_key_data(target_obj)
    if not target_key_data or not getattr(target_key_data, "key_blocks", None):
        return 0

    if getattr(target_key_data, "library", None) is not None:
        return 0

    names = [n for n in (key_names or []) if n and n != "Basis"]
    if not names:
        return 0

    removed = 0

    try:
        active_index = int(getattr(target_obj, "active_shape_key_index", 0))
    except Exception:
        active_index = 0

    for key_name in names:
        kb = target_key_data.key_blocks.get(key_name)
        if not kb or kb.name == "Basis":
            continue

        try:
            target_obj.shape_key_remove(kb)
            removed += 1
        except Exception:
            continue

    try:
        target_key_data = get_shape_key_data(target_obj)
        if target_key_data and getattr(target_key_data, "key_blocks", None):
            target_obj.active_shape_key_index = min(active_index, len(target_key_data.key_blocks) - 1)
    except Exception:
        pass

    return removed


# -----------------------------
# Data Model (groups + selection + group mapping)
# -----------------------------

def group_name_update(self, context):
    # Keep shape key -> group mapping synchronized after inline group rename.
    key_data = getattr(self, "id_data", None)
    if not key_data or not has_group_storage(key_data):
        return

    old_name = (getattr(self, "last_name", "") or "").strip()
    new_name = (getattr(self, "name", "") or "").strip()

    if not new_name:
        try:
            self.name = old_name or "Group"
        except Exception:
            pass
        return

    if not old_name:
        try:
            self.last_name = new_name
        except Exception:
            pass
        return

    if old_name == new_name:
        return

    # Prevent duplicate group names on inline rename.
    try:
        duplicate = False
        for g in key_data.skv_groups:
            if g == self:
                continue
            if g.name == new_name:
                duplicate = True
                break

        if duplicate:
            self.name = old_name
            return
    except Exception:
        pass


    if getattr(key_data, "library", None) is not None:
        try:
            self.name = old_name
        except Exception:
            pass
        return

    try:
        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == old_name:
                kd_set_group(key_data, kb.name, new_name)
    except Exception:
        pass

    try:
        self.last_name = new_name
    except Exception:
        pass

    tag_redraw_view3d(context)

class SKV_Group(PropertyGroup):
    name: StringProperty(name="Name", default="Group", update=group_name_update)
    last_name: StringProperty(name="Last Name", default="", options={"SKIP_SAVE"})


class SKV_SelectedName(PropertyGroup):
    name: StringProperty(name="Name", default="")


class SKV_KeyGroupEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    group: StringProperty(name="Group", default=INIT_GROUP_NAME)


class SKV_KeyDefaultEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    value: FloatProperty(name="Default Value", default=0.0)


class SKV_ActiveKeyEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")


class SKV_AutoKeyframeEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    enabled: BoolProperty(name="Enabled", default=False)
    last_frame: IntProperty(name="Last Frame", default=-999999)
    last_value: FloatProperty(name="Last Value", default=0.0)


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

def group_index_update(self, context):
    # Clear selected shape keys only when the active group index actually changes.
    key_data = self
    if not key_data or not hasattr(key_data, "skv_selected"):
        return

    current_index = int(getattr(key_data, "skv_group_index", 0))
    previous_index = int(getattr(key_data, "skv_last_group_index", -1))

    if current_index == previous_index:
        return

    try:
        key_data.skv_last_group_index = current_index
    except Exception:
        pass

    try:
        key_data.skv_selected.clear()
    except Exception:
        pass

    tag_redraw_view3d(context)

def active_keys_index_update(self, context):
    # Sync Active Shape Keys list selection to all shape key lists.
    if skv_shape_key_list_sync_active():
        return

    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if not obj or not key_data or not hasattr(key_data, "skv_active_keys"):
        return

    idx = int(getattr(key_data, "skv_active_keys_index", -1))
    if 0 <= idx < len(key_data.skv_active_keys):
        key_name = key_data.skv_active_keys[idx].name
        skv_sync_shape_key_list_indices(
            context,
            obj,
            key_name,
            set_blender_active=True,
        )

def _set_object_active_shape_key(obj, key_name: str) -> bool:
    # Set Blender active shape key by key name.
    if not obj or getattr(obj, "type", None) != "MESH" or not key_name:
        return False

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return False

    for i, kb in enumerate(key_data.key_blocks):
        if kb.name == key_name:
            try:
                obj.active_shape_key_index = i
                return True
            except Exception:
                return False

    return False


def active_keys_index_update(self, context):
    # Sync Active Shape Keys list selection to all shape key lists.
    if skv_shape_key_list_sync_active():
        return

    key_data = self
    if not key_data or not hasattr(key_data, "skv_active_keys"):
        return

    idx = int(getattr(key_data, "skv_active_keys_index", -1))
    if idx < 0 or idx >= len(key_data.skv_active_keys):
        return

    key_name = key_data.skv_active_keys[idx].name
    if not key_name:
        return

    obj = None

    # Prefer current addon-picked object if it owns this Key datablock.
    try:
        props = getattr(context.scene, "skv_props", None)
        picked = getattr(props, "object_pick", None) if props else None
        if picked and get_shape_key_data(picked) is key_data:
            obj = picked
    except Exception:
        obj = None

    # Fallback: find mesh object that owns this Key datablock.
    if obj is None:
        try:
            for candidate in bpy.data.objects:
                if getattr(candidate, "type", None) == "MESH" and get_shape_key_data(candidate) is key_data:
                    obj = candidate
                    break
        except Exception:
            obj = None

    if not obj:
        return

    skv_sync_shape_key_list_indices(
        context,
        obj,
        key_name,
        set_blender_active=True,
    )

def _shape_key_value_data_path(key_name: str) -> str:
    # Build a valid RNA path for a shape key value FCurve.
    try:
        escaped = bpy.utils.escape_identifier(key_name)
    except Exception:
        escaped = (key_name or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'key_blocks["{escaped}"].value'


def _action_get_slot_for_datablock(action, datablock, create: bool = False):
    # Return the action slot assigned to the datablock animation data.
    if not action or not datablock:
        return None

    anim_data = getattr(datablock, "animation_data", None)
    slot = getattr(anim_data, "action_slot", None) if anim_data else None
    if slot:
        return slot

    slots = getattr(action, "slots", None)
    if slots is None:
        return None

    if len(slots) > 0:
        return slots[0]

    if not create:
        return None

    try:
        name = getattr(datablock, "name", "") or "ShapeKeys"
        slot = action.slots.new(id_type="KEY", name=name)
    except Exception:
        return None

    try:
        if anim_data:
            anim_data.action_slot = slot
    except Exception:
        pass

    return slot


def _action_get_channelbag(action, datablock, create: bool = False):
    # Return an ActionChannelbag for Blender 5.0+ slotted actions.
    if not action or not datablock:
        return None

    slot = _action_get_slot_for_datablock(action, datablock, create=create)
    if not slot:
        return None

    layers = getattr(action, "layers", None)
    if layers is None:
        return None

    if len(layers) > 0:
        layer = layers[0]
    elif create:
        try:
            layer = layers.new("Layer")
        except Exception:
            return None
    else:
        return None

    strips = getattr(layer, "strips", None)
    if strips is None:
        return None

    if len(strips) > 0:
        strip = strips[0]
    elif create:
        try:
            strip = strips.new(type="KEYFRAME")
        except Exception:
            return None
    else:
        return None

    try:
        return strip.channelbag(slot, ensure=create)
    except Exception:
        return None


def _iter_action_fcurves(action, datablock):
    # Yield FCurves from both legacy actions and Blender 5.0+ channelbags.
    if not action:
        return

    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        for fc in legacy_fcurves:
            yield fc
        return

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return

    for fc in getattr(channelbag, "fcurves", []) or []:
        yield fc


def _action_fcurves_find(action, datablock, data_path: str, index: int = 0):
    # Find FCurve in both legacy and Blender 5.0+ action APIs.
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            return legacy_fcurves.find(data_path, index=index)
        except Exception:
            return None

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return None

    try:
        return channelbag.fcurves.find(data_path, index=index)
    except Exception:
        return None


def _action_fcurves_remove(action, datablock, fcurve) -> None:
    # Remove FCurve from both legacy and Blender 5.0+ action APIs.
    if not action or not fcurve:
        return

    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            legacy_fcurves.remove(fcurve)
        except Exception:
            pass
        return

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return

    try:
        channelbag.fcurves.remove(fcurve)
    except Exception:
        pass


def _action_fcurves_new(action, datablock, data_path: str, index: int = 0, group_name: str = ""):
    # Create FCurve in both legacy and Blender 5.0+ action APIs.
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            if group_name:
                return legacy_fcurves.new(data_path=data_path, index=index, action_group=group_name)
            return legacy_fcurves.new(data_path=data_path, index=index)
        except Exception:
            return None

    channelbag = _action_get_channelbag(action, datablock, create=True)
    if not channelbag:
        return None

    try:
        if group_name:
            return channelbag.fcurves.new(data_path=data_path, index=index, group_name=group_name)
        return channelbag.fcurves.new(data_path=data_path, index=index)
    except TypeError:
        try:
            if group_name:
                return channelbag.fcurves.new(data_path=data_path, index=index, action_group=group_name)
            return channelbag.fcurves.new(data_path=data_path, index=index)
        except Exception:
            return None
    except Exception:
        return None


def _copy_keyframe_point(dst_kp, src_kp) -> None:
    # Copy keyframe point settings.
    try:
        dst_kp.co = src_kp.co
    except Exception:
        pass

    try:
        dst_kp.handle_left = src_kp.handle_left
        dst_kp.handle_right = src_kp.handle_right
    except Exception:
        pass

    for attr in (
        "interpolation",
        "easing",
        "amplitude",
        "back",
        "period",
        "handle_left_type",
        "handle_right_type",
    ):
        try:
            setattr(dst_kp, attr, getattr(src_kp, attr))
        except Exception:
            pass


def _copy_fcurve_keyframes(src_fc, dst_action, dst_datablock, dst_data_path: str) -> bool:
    # Replace target FCurve keyframes with source FCurve keyframes.
    if not src_fc or not dst_action or not dst_datablock or not dst_data_path:
        return False

    try:
        existing = _action_fcurves_find(
            dst_action,
            dst_datablock,
            dst_data_path,
            index=int(getattr(src_fc, "array_index", 0)),
        )
        if existing:
            _action_fcurves_remove(dst_action, dst_datablock, existing)
    except Exception:
        pass

    group_name = ""
    try:
        group_name = src_fc.group.name if src_fc.group else ""
    except Exception:
        group_name = ""

    dst_fc = _action_fcurves_new(
        dst_action,
        dst_datablock,
        dst_data_path,
        index=int(getattr(src_fc, "array_index", 0)),
        group_name=group_name,
    )
    if not dst_fc:
        return False

    try:
        dst_fc.extrapolation = src_fc.extrapolation
    except Exception:
        pass

    try:
        dst_fc.color_mode = src_fc.color_mode
        dst_fc.color = src_fc.color
    except Exception:
        pass

    src_points = list(getattr(src_fc, "keyframe_points", []) or [])
    if not src_points:
        return True

    try:
        dst_fc.keyframe_points.add(len(src_points))
    except Exception:
        return False

    try:
        for dst_kp, src_kp in zip(dst_fc.keyframe_points, src_points):
            _copy_keyframe_point(dst_kp, src_kp)
        dst_fc.update()
    except Exception:
        return False

    return True


def inherit_transferred_keyframes(source_obj, target_obj, key_names) -> int:
    # Copy shape key value keyframes from source to target for transferred keys.
    if not source_obj or not target_obj or source_obj == target_obj:
        return 0
    if getattr(source_obj, "type", None) != "MESH" or getattr(target_obj, "type", None) != "MESH":
        return 0

    names = [n for n in (key_names or []) if n]
    if not names:
        return 0

    source_key_data = get_shape_key_data(source_obj)
    target_key_data = get_shape_key_data(target_obj)

    if not source_key_data or not target_key_data:
        return 0
    if not getattr(source_key_data, "key_blocks", None) or not getattr(target_key_data, "key_blocks", None):
        return 0

    source_anim = getattr(source_key_data, "animation_data", None)
    source_action = getattr(source_anim, "action", None) if source_anim else None
    if not source_action:
        return 0

    try:
        target_key_data.animation_data_create()
    except Exception:
        return 0

    target_anim = getattr(target_key_data, "animation_data", None)
    if not target_anim:
        return 0

    if not target_anim.action:
        try:
            target_anim.action = bpy.data.actions.new(name=f"{target_obj.name}_ShapeKeysAction")
        except Exception:
            return 0

    target_action = target_anim.action
    copied = 0

    for key_name in names:
        if not source_key_data.key_blocks.get(key_name):
            continue
        if not target_key_data.key_blocks.get(key_name):
            continue

        src_path = _shape_key_value_data_path(key_name)
        dst_path = _shape_key_value_data_path(key_name)

        matched = False
        for src_fc in _iter_action_fcurves(source_action, source_key_data):
            if src_fc.data_path != src_path:
                continue

            if _copy_fcurve_keyframes(src_fc, target_action, target_key_data, dst_path):
                matched = True

        if matched:
            copied += 1

    return copied

# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_Groups(UIList):
    bl_idname = "SKV_UL_groups"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        g = item
        key_data = data

        try:
            if not getattr(g, "last_name", ""):
                g.last_name = g.name
        except Exception:
            pass

        row = layout.row(align=True)
        row.prop(g, "name", text="", emboss=False, icon="FILE_FOLDER")
        try:
            cnt = count_keys_in_group(key_data, g.name)
        except Exception:
            cnt = 0
        row.label(text=str(cnt))


class SKV_UL_key_blocks(UIList):
    bl_idname = "SKV_UL_key_blocks"

    def filter_items(self, context, data, propname):
        key_data = data
        props = context.scene.skv_props

        if not key_data:
            return [], []

        group_name = get_selected_group_name(key_data)
        tokens = [t.lower() for t in parse_tokens(getattr(props, "search", ""))]

        flt_flags = []
        flt_neworder = []

        bf = self.bitflag_filter_item
        for kb in getattr(key_data, propname):
            ok = True

            if kb.name == "Basis":
                ok = False

            if ok and group_name:
                if kd_get_group(key_data, kb.name) != group_name:
                    ok = False

            if ok and tokens:
                name_l = kb.name.lower()
                for t in tokens:
                    if t not in name_l:
                        ok = False
                        break

            flt_flags.append(bf if ok else 0)

        return flt_flags, flt_neworder

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        kb = item
        key_data = data
        row = layout.row(align=True)

        icon_id = "CHECKBOX_HLT" if kd_is_selected(key_data, kb.name) else "CHECKBOX_DEHLT"
        op = row.operator("skv.key_toggle_select", text="", icon=icon_id, emboss=False)
        op.key_index = index

        row.prop(kb, "name", text="", emboss=False)
        row.prop(kb, "value", text="", slider=True)

        vis_icon = "HIDE_ON" if kb.mute else "HIDE_OFF"
        opv = row.operator("skv.shape_key_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.key_name = kb.name

        entry = _autokf_get_entry(key_data, kb.name, create=False)
        kf_enabled = bool(entry.enabled) if entry else False
        kf_icon = "KEYFRAME_HLT" if kf_enabled else "KEYFRAME"
        opk = row.operator("skv.auto_keyframe_toggle", text="", icon=kf_icon, emboss=False)
        opk.key_name = kb.name


class SKV_UL_active_keys(UIList):
    bl_idname = "SKV_UL_active_keys"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        key_data = data
        entry = item
        kname = entry.name

        row = layout.row(align=True)

        kb = None
        try:
            kb = key_data.key_blocks.get(kname) if kname else None
        except Exception:
            kb = None

        if kb:
            row.prop(kb, "name", text="", emboss=False)
            row.prop(kb, "value", text="", slider=True)

            vis_icon = "HIDE_ON" if kb.mute else "HIDE_OFF"
            opv = row.operator("skv.shape_key_toggle_visibility", text="", icon=vis_icon, emboss=False)
            opv.key_name = kname

            entry_kf = _autokf_get_entry(key_data, kname, create=False)
            kf_enabled = bool(entry_kf.enabled) if entry_kf else False
            kf_icon = "KEYFRAME_HLT" if kf_enabled else "KEYFRAME"
            opk = row.operator("skv.auto_keyframe_toggle", text="", icon=kf_icon, emboss=False)
            opk.key_name = kname
        else:
            row.label(text=kname or "Invalid", icon="ERROR")

        op = row.operator("skv.active_key_remove", text="", icon="REMOVE", emboss=False)
        op.key_name = kname


# -----------------------------
# Menus
# -----------------------------
class SKV_MT_MoveToGroup(Menu):
    bl_label = "Move to group"
    bl_idname = "SKV_MT_move_to_group"

    def draw(self, context):
        layout = self.layout
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not has_group_storage(key_data) or not key_data.skv_groups:
            layout.label(text="No groups")
            return

        current_group = get_selected_group_name(key_data)

        has_target_groups = False
        for g in key_data.skv_groups:
            if g.name == current_group:
                continue
            has_target_groups = True
            op = layout.operator("skv.move_selected_to_group", text=g.name, icon="FILE_FOLDER")
            op.group = g.name

        if not has_target_groups:
            layout.label(text="No other groups")


class SKV_MT_SelectActions(Menu):
    bl_label = "Actions"
    bl_idname = "SKV_MT_select_actions"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Not used (actions are displayed inline).")


# -----------------------------
# Operators (required by CLASSES)
# -----------------------------
class SKV_OT_ActiveKeyRemove(Operator):
    bl_idname = "skv.active_key_remove"
    bl_label = "Remove From Active"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        return bool(key_data and hasattr(key_data, "skv_active_keys") and hasattr(key_data, "skv_key_defaults"))

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        name = (self.key_name or "").strip()
        if not name:
            return {"CANCELLED"}

        try:
            i = 0
            while i < len(key_data.skv_active_keys):
                if key_data.skv_active_keys[i].name == name:
                    key_data.skv_active_keys.remove(i)
                    break
                i += 1
        except Exception:
            pass

        try:
            kb = key_data.key_blocks.get(name)
        except Exception:
            kb = None

        if kb is not None and hasattr(key_data, "skv_key_defaults"):
            try:
                cur_val = float(kb.value)
            except Exception:
                cur_val = 0.0

            try:
                updated = False
                for it in key_data.skv_key_defaults:
                    if it.name == name:
                        it.value = cur_val
                        updated = True
                        break
                if not updated:
                    it = key_data.skv_key_defaults.add()
                    it.name = name
                    it.value = cur_val
            except Exception:
                pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ShapeKeyToggleVisibility(Operator):
    bl_idname = "skv.shape_key_toggle_visibility"
    bl_label = "Toggle Shape Key Visibility"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(self.key_name)
        if not kb:
            return {"CANCELLED"}

        kb.mute = not kb.mute
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_AutoKeyframeToggle(Operator):
    bl_idname = "skv.auto_keyframe_toggle"
    bl_label = "Toggle Auto Keyframe"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            return {"CANCELLED"}

        name = (self.key_name or "").strip()
        if not name:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(name)
        if not kb:
            return {"CANCELLED"}

        entry = _autokf_get_entry(key_data, name, create=True)
        entry.enabled = not bool(entry.enabled)

        frame = int(context.scene.frame_current)
        entry.last_frame = frame
        try:
            entry.last_value = float(kb.value)
        except Exception:
            entry.last_value = 0.0

        if entry.enabled:
            try:
                key_data.keyframe_insert(data_path=f'key_blocks["{name}"].value', frame=frame)
            except Exception:
                pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_KeyToggleSelect(Operator):
    bl_idname = "skv.key_toggle_select"
    bl_label = "Toggle Select"
    bl_options = {"REGISTER", "UNDO"}

    key_index: IntProperty(name="Key Index", default=-1)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        idx = int(self.key_index)
        if not (0 <= idx < len(key_data.key_blocks)):
            return {"CANCELLED"}

        kb = key_data.key_blocks[idx]
        if kb.name == "Basis":
            return {"CANCELLED"}

        kd_set_selected(key_data, kb.name, not kd_is_selected(key_data, kb.name))
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_SelectVisible(Operator):
    bl_idname = "skv.select_visible"
    bl_label = "Select Visible"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(name="Mode", default="ALL")

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        props = context.scene.skv_props
        group_name = get_selected_group_name(key_data)
        search_tokens = [t.lower() for t in parse_tokens(getattr(props, "search", ""))]

        visible_names = []
        for kb in key_data.key_blocks:
            if kb.name == "Basis":
                continue
            if kd_get_group(key_data, kb.name) != group_name:
                continue

            if search_tokens:
                name_l = kb.name.lower()
                if any(tok not in name_l for tok in search_tokens):
                    continue

            visible_names.append(kb.name)

        if self.mode == "ALL":
            for n in visible_names:
                kd_set_selected(key_data, n, True)

        elif self.mode == "CLEAR":
            for n in visible_names:
                kd_set_selected(key_data, n, False)

        elif self.mode == "INVERT":
            for n in visible_names:
                kd_set_selected(key_data, n, not kd_is_selected(key_data, n))

        tag_redraw_view3d(context)
        return {"FINISHED"}


# class SKV_OT_SelectByAffix(Operator):
#     bl_idname = "skv.select_by_affix"
#     bl_label = "Select by Affix"
#     bl_options = {"REGISTER", "UNDO"}
#
#     mode: StringProperty(name="Mode", default="PREFIX")
#
#     def execute(self, context):
#         obj = get_active_object(context)
#         if not obj:
#             return {"CANCELLED"}
#
#         key_data = get_shape_key_data(obj)
#         if not key_data or not key_data.key_blocks:
#             return {"CANCELLED"}
#
#         if not is_initialized(key_data):
#             self.report({"INFO"}, "Not initialized.")
#             return {"CANCELLED"}
#
#         props = context.scene.skv_props
#         text = (props.affix_value or "").strip()
#         if not text:
#             self.report({"INFO"}, "Enter prefix/suffix text.")
#             return {"CANCELLED"}
#
#         group_name = get_selected_group_name(key_data)
#         matched = []
#
#         for kb in key_data.key_blocks:
#             if kb.name == "Basis":
#                 continue
#             if kd_get_group(key_data, kb.name) != group_name:
#                 continue
#
#             if self.mode == "PREFIX" and kb.name.startswith(text):
#                 matched.append(kb.name)
#             elif self.mode == "SUFFIX" and kb.name.endswith(text):
#                 matched.append(kb.name)
#
#         if not matched:
#             self.report({"INFO"}, "No matching shape keys.")
#             return {"CANCELLED"}
#
#         for n in matched:
#             kd_set_selected(key_data, n, True)
#
#         props.last_affix_name = text
#         props.last_affix_pending = True
#
#         tag_redraw_view3d(context)
#         return {"FINISHED"}

class SKV_OT_MoveSelectedToGroup(Operator):
    bl_idname = "skv.move_selected_to_group"
    bl_label = "Move Selected To Group"
    bl_options = {"REGISTER", "UNDO"}

    group: StringProperty(name="Group", default=INIT_GROUP_NAME)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        group_name = (self.group or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME
        selected = kd_selected_set(key_data)
        for n in selected:
            if n and n != "Basis":
                kd_set_group(key_data, n, group_name)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ResetGroupValues(Operator):
    bl_idname = "skv.reset_group_values"
    bl_label = "Zero Values (Selected)"
    bl_description = "Set value=0 only for selected shape keys in the current group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        group_name = get_selected_group_name(key_data)
        selected = kd_selected_set(key_data)

        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        preexisting_active = set()
        try:
            if hasattr(key_data, "skv_active_keys"):
                preexisting_active = {it.name for it in key_data.skv_active_keys if it.name}
        except Exception:
            preexisting_active = set()

        changed = 0
        with InternalValueChangeGuard():
            for kb in key_data.key_blocks:
                if kb.name not in selected:
                    continue
                if kd_get_group(key_data, kb.name) != group_name:
                    continue

                try:
                    kb.value = 0.0
                except Exception:
                    continue

                try:
                    if hasattr(key_data, "skv_key_defaults"):
                        updated = False
                        for d in key_data.skv_key_defaults:
                            if d.name == kb.name:
                                d.value = 0.0
                                updated = True
                                break
                        if not updated:
                            d = key_data.skv_key_defaults.add()
                            d.name = kb.name
                            d.value = 0.0
                except Exception:
                    pass

                if kb.name not in preexisting_active:
                    try:
                        if hasattr(key_data, "skv_active_keys"):
                            i = 0
                            while i < len(key_data.skv_active_keys):
                                if key_data.skv_active_keys[i].name == kb.name:
                                    key_data.skv_active_keys.remove(i)
                                else:
                                    i += 1
                    except Exception:
                        pass

                changed += 1

        if changed == 0:
            self.report({"INFO"}, "No keys were reset.")
            return {"CANCELLED"}

        kd_clear_selected(key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GroupAdd(Operator):
    bl_idname = "skv.group_add"
    bl_label = "Add Group"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Name", default="")

    def invoke(self, context, event):
        self.name = "New Group"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        new_name = _make_unique_name(self.name, group_names(key_data))
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name
        g.last_name = new_name

        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GroupRemove(Operator):
    bl_idname = "skv.group_remove"
    bl_label = "Remove Group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        idx = int(key_data.skv_group_index)
        if not (0 <= idx < len(key_data.skv_groups)):
            return {"CANCELLED"}

        if len(key_data.skv_groups) <= 1:
            self.report({"WARNING"}, "Object must have at least one group.")
            return {"CANCELLED"}

        removed_name = key_data.skv_groups[idx].name

        fallback_name = ""
        for i, g in enumerate(key_data.skv_groups):
            if i != idx:
                fallback_name = g.name
                break

        fallback_name = fallback_name or get_fallback_group_name(key_data)

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == removed_name:
                kd_set_group(key_data, kb.name, fallback_name)

        key_data.skv_groups.remove(idx)

        if key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = max(0, len(key_data.skv_groups) - 1)

        try:
            if hasattr(key_data, "skv_last_group_index"):
                key_data.skv_last_group_index = int(key_data.skv_group_index)
        except Exception:
            pass

        ensure_init_setup_write(obj)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GroupRename(Operator):
    bl_idname = "skv.group_rename"
    bl_label = "Rename Group"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data) or not is_initialized(key_data):
            return {"CANCELLED"}

        idx = int(key_data.skv_group_index)
        if 0 <= idx < len(key_data.skv_groups):
            self.new_name = key_data.skv_groups[idx].name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        idx = int(key_data.skv_group_index)
        if not (0 <= idx < len(key_data.skv_groups)):
            return {"CANCELLED"}

        old = key_data.skv_groups[idx].name

        new = self.new_name.strip()
        if not new:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        names = group_names(key_data)
        if new in names and new != old:
            self.report({"WARNING"}, "Group with this name already exists.")
            return {"CANCELLED"}

        key_data.skv_groups[idx].name = new
        key_data.skv_groups[idx].last_name = new

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == old:
                kd_set_group(key_data, kb.name, new)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_CreateGroupFromSelected(Operator):
    bl_idname = "skv.create_group_from_selected"
    bl_label = "Create Group From Selected"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            props.last_affix_pending = False
        else:
            self.name = "New Group"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data) or not key_data.key_blocks:
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_selected_set(key_data)
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        new_name = _make_unique_name(self.name, group_names(key_data))
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name
        g.last_name = new_name

        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

        moved = 0
        for kb in key_data.key_blocks:
            if kb.name in selected:
                kd_set_group(key_data, kb.name, new_name)
                moved += 1

        if moved == 0:
            for i, gg in enumerate(key_data.skv_groups):
                if gg.name == new_name:
                    key_data.skv_groups.remove(i)
                    break
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_TransferTo(Operator):
    bl_idname = "skv.transfer_to"
    bl_label = "Transfer to"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        context.scene.skv_transfer_source_name = obj.name

        tgt = getattr(context.scene, "skv_transfer_target", None)
        if not tgt or getattr(tgt, "type", None) != "MESH" or tgt.name == obj.name:
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "skv_transfer_target", text="Target")
        layout.prop(context.scene.skv_props, "transfer_inheritance", text="Inherit presets")
        layout.prop(context.scene.skv_props, "transfer_keyframes_inheritance", text="Inherit keyframes")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        if not obj:
            return False
        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return False
        selected = list(kd_selected_set(key_data))
        return any(n != "Basis" for n in selected)

    def execute(self, context):
        from .transfer import MeshDataTransfer
        from . import presets

        source = get_active_object(context)
        if not source:
            return {"CANCELLED"}

        target = getattr(context.scene, "skv_transfer_target", None)
        if not target or getattr(target, "type", None) != "MESH":
            self.report({"ERROR"}, "Target mesh is not set")
            return {"CANCELLED"}

        if target.name == source.name:
            self.report({"ERROR"}, "Target must be different from source")
            return {"CANCELLED"}

        key_data = get_shape_key_data(source)
        selected_names = [n for n in kd_selected_set(key_data) if n != "Basis"]
        if not selected_names:
            self.report({"ERROR"}, "No selected Shape Keys")
            return {"CANCELLED"}

        _remove_existing_target_shape_keys(target, selected_names)

        mdt = MeshDataTransfer(source=source, target=target, vertex_group=None)
        ok = mdt.transfer_shape_keys(shapekey_names=selected_names)

        if not ok:
            self.report({"WARNING"}, "Nothing transferred")
            return {"CANCELLED"}

        target_key_data = get_shape_key_data(target)
        if target_key_data and getattr(target_key_data, "key_blocks", None):
            with InternalValueChangeGuard():
                for key_name in selected_names:
                    src_kb = key_data.key_blocks.get(key_name)
                    dst_kb = target_key_data.key_blocks.get(key_name)
                    if not src_kb or not dst_kb:
                        continue
                    try:
                        dst_kb.value = float(src_kb.value)
                    except Exception:
                        pass

        inherit_transferred_groups(source, target, selected_names)

        inherited_count = 0
        if getattr(context.scene.skv_props, "transfer_inheritance", False):
            inherited_count = presets.inherit_transferred_keys_to_presets(source, target, selected_names)

        if getattr(context.scene.skv_props, "transfer_keyframes_inheritance", False):
            inherit_transferred_keyframes(source, target, selected_names)

        clear_selection_ui(context, key_data)

        view_layer = context.view_layer
        try:
            for ob in list(view_layer.objects):
                if ob.select_get():
                    ob.select_set(False)
        except Exception:
            pass

        try:
            target.select_set(True)
        except Exception:
            pass

        view_layer.objects.active = target

        tag_redraw_view3d(context)
        return {"FINISHED"}



class SKV_OT_TransferFrom(Operator):
    bl_idname = "skv.transfer_from"
    bl_label = "Transfer from"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        target = get_active_object(context)
        if not target:
            return {"CANCELLED"}

        context.scene.skv_transfer_source_name = target.name

        source = getattr(context.scene, "skv_transfer_target", None)
        if not source or getattr(source, "type", None) != "MESH" or source.name == target.name:
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout

        target = get_active_object(context)
        target_key_data = get_shape_key_data(target) if target else None
        target_has_shape_keys = bool(
            target_key_data
            and getattr(target_key_data, "key_blocks", None)
            and any(kb.name != "Basis" for kb in target_key_data.key_blocks)
        )

        if target_has_shape_keys:
            box = layout.box()
            box.label(text="Warning: matching Shape Keys on target will be replaced.", icon="ERROR")

        layout.prop(context.scene, "skv_transfer_target", text="Source")
        layout.prop(context.scene.skv_props, "transfer_inheritance", text="Inherit presets")
        layout.prop(context.scene.skv_props, "transfer_keyframes_inheritance", text="Inherit keyframes")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        return bool(obj and getattr(obj, "type", None) == "MESH")

    def execute(self, context):
        from .transfer import MeshDataTransfer
        from . import presets

        target = get_active_object(context)
        if not target:
            return {"CANCELLED"}

        source = getattr(context.scene, "skv_transfer_target", None)
        if not source or getattr(source, "type", None) != "MESH":
            self.report({"ERROR"}, "Source mesh is not set")
            return {"CANCELLED"}

        if source.name == target.name:
            self.report({"ERROR"}, "Source must be different from target")
            return {"CANCELLED"}

        source_key_data = get_shape_key_data(source)
        if not source_key_data or not getattr(source_key_data, "key_blocks", None):
            self.report({"ERROR"}, "Source has no Shape Keys")
            return {"CANCELLED"}

        selected_names = [kb.name for kb in source_key_data.key_blocks if kb.name != "Basis"]

        if not selected_names:
            self.report({"ERROR"}, "Source has no transferable Shape Keys")
            return {"CANCELLED"}

        _remove_existing_target_shape_keys(target, selected_names)

        mdt = MeshDataTransfer(source=source, target=target, vertex_group=None)
        try:
            ok = mdt.transfer_shape_keys(shapekey_names=selected_names)
        finally:
            mdt.free()

        if not ok:
            self.report({"WARNING"}, "Nothing transferred")
            return {"CANCELLED"}

        target_key_data = get_shape_key_data(target)
        if target_key_data and getattr(target_key_data, "key_blocks", None):
            with InternalValueChangeGuard():
                for key_name in selected_names:
                    src_kb = source_key_data.key_blocks.get(key_name)
                    dst_kb = target_key_data.key_blocks.get(key_name)
                    if not src_kb or not dst_kb:
                        continue
                    try:
                        dst_kb.value = float(src_kb.value)
                    except Exception:
                        pass

        inherit_transferred_groups(source, target, selected_names)

        if getattr(context.scene.skv_props, "transfer_inheritance", False):
            presets.inherit_transferred_keys_to_presets(source, target, selected_names)

        if getattr(context.scene.skv_props, "transfer_keyframes_inheritance", False):
            inherit_transferred_keyframes(source, target, selected_names)

        clear_selection_ui(context, source_key_data)

        view_layer = context.view_layer
        try:
            for ob in list(view_layer.objects):
                if ob.select_get():
                    ob.select_set(False)
        except Exception:
            pass

        try:
            target.select_set(True)
        except Exception:
            pass

        view_layer.objects.active = target

        tag_redraw_view3d(context)
        return {"FINISHED"}

CLASSES = (
    SKV_Group,
    SKV_SelectedName,
    SKV_KeyGroupEntry,
    SKV_KeyDefaultEntry,
    SKV_ActiveKeyEntry,
    SKV_AutoKeyframeEntry,
    SKV_UL_Groups,
    SKV_UL_key_blocks,
    SKV_UL_active_keys,
    SKV_MT_MoveToGroup,
    SKV_MT_SelectActions,
    SKV_OT_ActiveKeyRemove,
    SKV_OT_ShapeKeyToggleVisibility,
    SKV_OT_AutoKeyframeToggle,
    SKV_OT_KeyToggleSelect,
    SKV_OT_SelectVisible,
    # SKV_OT_SelectByAffix,
    SKV_OT_MoveSelectedToGroup,
    SKV_OT_ResetGroupValues,
    SKV_OT_GroupAdd,
    SKV_OT_GroupRemove,
    SKV_OT_GroupRename,
    SKV_OT_CreateGroupFromSelected,
    SKV_OT_TransferTo,
    SKV_OT_TransferFrom,
)