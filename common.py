import re
import bpy

INIT_GROUP_NAME = "Main"

# Prevent recursion when preset slider writes to key values
_PRESET_APPLY_GUARD = False

# Internal guard for programmatic shape key value changes (addon operators).
# Used to prevent "Active Shape Keys" being polluted by scripted value updates.
_SKV_INTERNAL_VALUE_CHANGE_DEPTH = 0


def internal_value_change_begin() -> None:
    global _SKV_INTERNAL_VALUE_CHANGE_DEPTH
    _SKV_INTERNAL_VALUE_CHANGE_DEPTH += 1


def internal_value_change_end() -> None:
    global _SKV_INTERNAL_VALUE_CHANGE_DEPTH
    _SKV_INTERNAL_VALUE_CHANGE_DEPTH = max(0, _SKV_INTERNAL_VALUE_CHANGE_DEPTH - 1)


def is_internal_value_change() -> bool:
    return _SKV_INTERNAL_VALUE_CHANGE_DEPTH > 0


class InternalValueChangeGuard:
    def __enter__(self):
        internal_value_change_begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        internal_value_change_end()
        return False


# Utilities
def get_active_object(context):
    obj = context.active_object
    if not obj:
        return None
    data = getattr(obj, "data", None)
    if data is None:
        return None
    if not hasattr(data, "shape_keys"):
        return None
    return obj


def get_shape_key_data(obj):
    if obj is None:
        return None
    data = getattr(obj, "data", None)
    if data is None:
        return None
    if not hasattr(data, "shape_keys"):
        return None
    return data.shape_keys  # bpy.types.Key or None


def has_group_storage(key_data) -> bool:
    return bool(key_data) and hasattr(key_data, "skv_groups") and hasattr(key_data, "skv_group_index")


def group_names(key_data):
    if not has_group_storage(key_data):
        return []
    return [g.name for g in key_data.skv_groups]


def get_fallback_group_name(key_data) -> str:
    if has_group_storage(key_data) and getattr(key_data, "skv_groups", None) and len(key_data.skv_groups) > 0:
        name = (key_data.skv_groups[0].name or "").strip()
        return name or INIT_GROUP_NAME
    return INIT_GROUP_NAME


def is_initialized(key_data) -> bool:
    """Return True when addon storage exists and at least one group is present."""
    if not has_group_storage(key_data):
        return False
    return bool(getattr(key_data, "skv_groups", None)) and len(key_data.skv_groups) > 0


def cleanup_legacy_init_group(key_data) -> None:
    if not has_group_storage(key_data) or not getattr(key_data, "skv_groups", None):
        return
    # Do not modify linked data.
    if getattr(key_data, "library", None) is not None:
        return

    init_indices = [i for i, g in enumerate(key_data.skv_groups) if g.name == "Init"]
    if not init_indices:
        if getattr(key_data, "key_blocks", None):
            for kb in key_data.key_blocks:
                try:
                    if "skv_group" in kb:
                        del kb["skv_group"]
                except Exception:
                    pass
        return

    main_indices = [i for i, g in enumerate(key_data.skv_groups) if g.name == INIT_GROUP_NAME]

    # Remap mapping entries first.
    if hasattr(key_data, "skv_key_groups"):
        for it in key_data.skv_key_groups:
            if getattr(it, "group", "") == "Init":
                it.group = INIT_GROUP_NAME

    if main_indices:
        for i in reversed(init_indices):
            try:
                key_data.skv_groups.remove(i)
            except Exception:
                pass
    else:
        try:
            key_data.skv_groups[init_indices[0]].name = INIT_GROUP_NAME
        except Exception:
            pass
        for i in reversed(init_indices[1:]):
            try:
                key_data.skv_groups.remove(i)
            except Exception:
                pass

    try:
        if key_data.skv_group_index < 0 or key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = 0
    except Exception:
        pass

    if getattr(key_data, "key_blocks", None):
        for kb in key_data.key_blocks:
            try:
                if "skv_group" in kb:
                    del kb["skv_group"]
            except Exception:
                pass


def get_selected_group_name(key_data):
    if not has_group_storage(key_data) or not key_data.skv_groups:
        return INIT_GROUP_NAME

    idx = int(getattr(key_data, "skv_group_index", 0))
    if 0 <= idx < len(key_data.skv_groups):
        name = (key_data.skv_groups[idx].name or "").strip()
        return name if name else get_fallback_group_name(key_data)

    return get_fallback_group_name(key_data)


def enum_groups_for_active_object(self, context):
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if not key_data or not has_group_storage(key_data) or not key_data.skv_groups:
        return [(INIT_GROUP_NAME, INIT_GROUP_NAME, "Not initialized")]
    return [(g.name, g.name, "") for g in key_data.skv_groups if g.name]


def tag_redraw_view3d(context):
    scr = getattr(context, "screen", None)
    if not scr:
        return
    for area in scr.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def parse_tokens(text: str):
    if not text:
        return []
    parts = re.split(r"[;,]+", text)
    out = []
    for p in parts:
        t = p.strip()
        if t:
            out.append(t)
    return out


def clear_selection_ui(context, key_data):
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    if hasattr(context, "scene") and hasattr(context.scene, "skv_props"):
        context.scene.skv_props.show_select = False


def show_select_update(self, context):
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    tag_redraw_view3d(context)


def kd_get_group(key_data, kb_name: str) -> str:
    fallback = get_fallback_group_name(key_data)

    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return fallback
    if not kb_name:
        return fallback

    for it in key_data.skv_key_groups:
        if it.name == kb_name:
            group_name = (it.group or "").strip()
            return group_name if group_name else fallback

    return fallback


def kd_set_group(key_data, kb_name: str, group_name: str) -> None:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return
    if not kb_name:
        return

    group_name = (group_name or "").strip() or get_fallback_group_name(key_data)

    for it in key_data.skv_key_groups:
        if it.name == kb_name:
            it.group = group_name
            return

    it = key_data.skv_key_groups.add()
    it.name = kb_name
    it.group = group_name


def kd_prune_group_map(key_data, valid_names) -> None:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return
    i = 0
    while i < len(key_data.skv_key_groups):
        if key_data.skv_key_groups[i].name not in valid_names:
            key_data.skv_key_groups.remove(i)
        else:
            i += 1


def kd_selected_set(key_data):
    if not key_data or not hasattr(key_data, "skv_selected"):
        return set()
    return {it.name for it in key_data.skv_selected if it.name}


def kd_is_selected(key_data, kb_name: str) -> bool:
    if not kb_name or not key_data or not hasattr(key_data, "skv_selected"):
        return False
    for it in key_data.skv_selected:
        if it.name == kb_name:
            return True
    return False


def kd_set_selected(key_data, kb_name: str, state: bool) -> None:
    if not kb_name or not key_data or not hasattr(key_data, "skv_selected"):
        return

    if state:
        if kd_is_selected(key_data, kb_name):
            return
        it = key_data.skv_selected.add()
        it.name = kb_name
    else:
        for i, it in enumerate(key_data.skv_selected):
            if it.name == kb_name:
                key_data.skv_selected.remove(i)
                return


def kd_clear_selected(key_data) -> None:
    if not key_data or not hasattr(key_data, "skv_selected"):
        return
    key_data.skv_selected.clear()


def count_keys_in_group(key_data, group_name: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None):
        return 0

    return sum(
        1
        for kb in key_data.key_blocks
        if kb.name != "Basis" and kd_get_group(key_data, kb.name) == group_name
    )


def count_selected_in_group(key_data, group_name: str, search: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None):
        return 0

    s = (search or "").strip().lower()
    selected = kd_selected_set(key_data)

    c = 0
    for kb in key_data.key_blocks:
        if kb.name == "Basis":
            continue
        if kd_get_group(key_data, kb.name) != group_name:
            continue
        if s and s not in kb.name.lower():
            continue
        if kb.name in selected:
            c += 1
    return c


def ensure_init_setup_write(obj):
    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if not has_group_storage(key_data):
        return
    if getattr(key_data, "library", None) is not None:
        return

    cleanup_legacy_init_group(key_data)

    if len(key_data.skv_groups) == 0:
        g = key_data.skv_groups.add()
        g.name = INIT_GROUP_NAME
        try:
            g.last_name = INIT_GROUP_NAME
        except Exception:
            pass

    try:
        if key_data.skv_group_index < 0 or key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = 0
    except Exception:
        pass

    names = group_names(key_data)
    fallback_group = get_fallback_group_name(key_data)

    valid_kb_names = {kb.name for kb in key_data.key_blocks}
    kd_prune_group_map(key_data, valid_kb_names)

    for kb in key_data.key_blocks:
        cur = kd_get_group(key_data, kb.name)
        if cur not in names:
            kd_set_group(key_data, kb.name, fallback_group)

    for kb in key_data.key_blocks:
        try:
            if "skv_group" in kb:
                del kb["skv_group"]
        except Exception:
            pass


def get_active_preset(key_data):
    if not key_data or not hasattr(key_data, "skv_presets") or not hasattr(key_data, "skv_preset_index"):
        return None
    idx = int(key_data.skv_preset_index)
    if 0 <= idx < len(key_data.skv_presets):
        return key_data.skv_presets[idx]
    return None


def _is_basis_name(key_data, name: str) -> bool:
    try:
        if not key_data or not key_data.key_blocks:
            return False
        return key_data.key_blocks[0].name == name
    except Exception:
        return name == "Basis"


def preset_apply(preset, context) -> None:
    global _PRESET_APPLY_GUARD
    if _PRESET_APPLY_GUARD:
        return

    key_data = getattr(preset, "id_data", None)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if getattr(key_data, "library", None) is not None:
        return

    obj = get_active_object(context)
    if not obj or get_shape_key_data(obj) is not key_data:
        return

    _PRESET_APPLY_GUARD = True
    try:
        kb_map = key_data.key_blocks
        factor = float(preset.value)

        # Prevent "Active Shape Keys" pollution by preset-driven value changes.
        with InternalValueChangeGuard():
            for it in preset.items:
                if not it.name:
                    continue
                kb = kb_map.get(it.name)
                if not kb:
                    continue

                try:
                    new_val = factor * float(it.max_value)
                except Exception:
                    continue

                try:
                    kb.value = new_val
                except Exception:
                    continue

                # Sync defaults snapshot to new value so diff-based detector won't add it later.
                try:
                    updated = False
                    if hasattr(key_data, "skv_key_defaults"):
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
        _PRESET_APPLY_GUARD = False

    tag_redraw_view3d(context)


def preset_value_update(self, context):
    preset_apply(self, context)


_SKV_SHAPE_KEY_LIST_SYNC_GUARD = False


def skv_shape_key_list_sync_active() -> bool:
    return _SKV_SHAPE_KEY_LIST_SYNC_GUARD


def skv_is_quick_shape_key_name(name: str) -> bool:
    return bool(re.fullmatch(r"Quick Key(?: \d+)?", name or ""))


def skv_find_shape_key_index(key_data, key_name: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None) or not key_name:
        return -1

    for i, kb in enumerate(key_data.key_blocks):
        if kb.name == key_name:
            return i

    return -1


def skv_get_active_shape_key_name(obj) -> str:
    if not obj or getattr(obj, "type", None) != "MESH":
        return ""

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return ""

    idx = int(getattr(obj, "active_shape_key_index", -1))
    if 0 <= idx < len(key_data.key_blocks):
        return key_data.key_blocks[idx].name

    return ""


def skv_set_active_shape_key_by_name(obj, key_name: str) -> bool:
    if not obj or getattr(obj, "type", None) != "MESH" or not key_name:
        return False

    key_data = get_shape_key_data(obj)
    idx = skv_find_shape_key_index(key_data, key_name)

    if idx < 0:
        return False

    try:
        obj.active_shape_key_index = idx
        return True
    except Exception:
        return False


def skv_sync_shape_key_list_indices(context, obj, key_name: str = "", set_blender_active: bool = True) -> None:
    """
    Synchronize active rows of addon UILists from one active shape key name.

    This synchronizes:
    - Shape Keys in group
    - Quick Shape Keys
    - Active Shape Keys
    - Shape Keys in active preset
    """
    global _SKV_SHAPE_KEY_LIST_SYNC_GUARD

    if _SKV_SHAPE_KEY_LIST_SYNC_GUARD:
        return

    if not context or not obj or getattr(obj, "type", None) != "MESH":
        return

    scene = getattr(context, "scene", None)
    props = getattr(scene, "skv_props", None) if scene else None
    if not props:
        return

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return

    if not key_name:
        key_name = skv_get_active_shape_key_name(obj)

    if not key_name or skv_find_shape_key_index(key_data, key_name) < 0:
        if getattr(key_data, "key_blocks", None):
            key_name = key_data.key_blocks[0].name
        else:
            return

    key_index = skv_find_shape_key_index(key_data, key_name)
    if key_index < 0:
        return

    _SKV_SHAPE_KEY_LIST_SYNC_GUARD = True
    try:
        if set_blender_active or int(getattr(obj, "active_shape_key_index", -1)) < 0:
            skv_set_active_shape_key_by_name(obj, key_name)

        try:
            key_group = kd_get_group(key_data, key_name)
            if hasattr(key_data, "skv_groups") and key_group:
                for group_index, group in enumerate(key_data.skv_groups):
                    if group.name == key_group:
                        key_data.skv_group_index = group_index
                        break
        except Exception:
            pass

        try:
            props.keys_index = key_index
        except Exception:
            pass

        try:
            props.quick_keys_index = key_index if skv_is_quick_shape_key_name(key_name) else -1
        except Exception:
            pass

        try:
            active_index = -1
            if hasattr(key_data, "skv_active_keys"):
                for i, it in enumerate(key_data.skv_active_keys):
                    if it.name == key_name:
                        active_index = i
                        break
            if hasattr(key_data, "skv_active_keys_index"):
                key_data.skv_active_keys_index = active_index
        except Exception:
            pass

        try:
            if hasattr(scene, "skv_global_presets") and hasattr(scene, "skv_global_preset_index"):
                preset_index = int(scene.skv_global_preset_index)
                if 0 <= preset_index < len(scene.skv_global_presets):
                    gpreset = scene.skv_global_presets[preset_index]
                    item_index = -1
                    for i, it in enumerate(gpreset.items):
                        if it.object_name == obj.name and it.key_name == key_name:
                            item_index = i
                            break
                    gpreset.items_index = item_index
        except Exception:
            pass

        tag_redraw_view3d(context)
    finally:
        _SKV_SHAPE_KEY_LIST_SYNC_GUARD = False