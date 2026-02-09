import re
import bpy

INIT_GROUP_NAME = "Init"

# Prevent recursion when preset slider writes to key values
_PRESET_APPLY_GUARD = False


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


def is_initialized(key_data) -> bool:
    if not has_group_storage(key_data):
        return False
    if not key_data.skv_groups:
        return False
    return any(g.name == INIT_GROUP_NAME for g in key_data.skv_groups)


def get_selected_group_name(key_data):
    if not has_group_storage(key_data) or not key_data.skv_groups:
        return INIT_GROUP_NAME
    idx = int(getattr(key_data, "skv_group_index", 0))
    if 0 <= idx < len(key_data.skv_groups):
        return key_data.skv_groups[idx].name
    return INIT_GROUP_NAME


def enum_groups_for_active_object(self, context):
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if not key_data or not has_group_storage(key_data) or not key_data.skv_groups:
        return [(INIT_GROUP_NAME, INIT_GROUP_NAME, "Not initialized")]
    items = [(g.name, g.name, "") for g in key_data.skv_groups]
    if not any(i[0] == INIT_GROUP_NAME for i in items):
        items.insert(0, (INIT_GROUP_NAME, INIT_GROUP_NAME, ""))
    return items


def tag_redraw_view3d(context):
    scr = getattr(context, "screen", None)
    if not scr:
        return
    for area in scr.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def parse_tokens(text: str) -> list[str]:
    # Split by comma/semicolon, trim, drop empties
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
    # Clear selected keys, disable Select mode
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    if hasattr(context, "scene") and hasattr(context.scene, "skv_props"):
        context.scene.skv_props.show_select = False


def show_select_update(self, context):
    # Clear selection when toggling Select mode
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    tag_redraw_view3d(context)


# Legacy (old versions) group storage on KeyBlock ID props
def kb_get_group_legacy(kb) -> str:
    try:
        v = kb.get("skv_group", INIT_GROUP_NAME)
        return v if v else INIT_GROUP_NAME
    except Exception:
        return INIT_GROUP_NAME


# Group mapping on Key datablock: Key.skv_key_groups
def kd_get_group(key_data, kb_name: str) -> str:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return INIT_GROUP_NAME
    if not kb_name:
        return INIT_GROUP_NAME
    for it in key_data.skv_key_groups:
        if it.name == kb_name:
            return it.group if it.group else INIT_GROUP_NAME
    return INIT_GROUP_NAME


def kd_set_group(key_data, kb_name: str, group_name: str) -> None:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return
    if not kb_name:
        return
    group_name = group_name or INIT_GROUP_NAME

    for it in key_data.skv_key_groups:
        if it.name == kb_name:
            it.group = group_name
            return

    it = key_data.skv_key_groups.add()
    it.name = kb_name
    it.group = group_name


def kd_prune_group_map(key_data, valid_names: set[str]) -> None:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return
    i = 0
    while i < len(key_data.skv_key_groups):
        if key_data.skv_key_groups[i].name not in valid_names:
            key_data.skv_key_groups.remove(i)
        else:
            i += 1


# Multi-select storage on Key datablock (name list)
def kd_selected_set(key_data) -> set[str]:
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


# Counts
def count_keys_in_group(key_data, group_name: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None):
        return 0
    return sum(1 for kb in key_data.key_blocks if kd_get_group(key_data, kb.name) == group_name)


def count_selected_in_group(key_data, group_name: str, search: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None):
        return 0

    s = (search or "").strip().lower()
    selected = kd_selected_set(key_data)

    c = 0
    for kb in key_data.key_blocks:
        if kd_get_group(key_data, kb.name) != group_name:
            continue
        if s and s not in kb.name.lower():
            continue
        if kb.name in selected:
            c += 1
    return c


# Init / scan sync (Operators only)
def ensure_init_setup_write(obj):
    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if not has_group_storage(key_data):
        return
    if getattr(key_data, "library", None) is not None:
        return

    names = group_names(key_data)
    if INIT_GROUP_NAME not in names:
        g = key_data.skv_groups.add()
        g.name = INIT_GROUP_NAME
        names = group_names(key_data)

    if key_data.skv_group_index < 0 or key_data.skv_group_index >= len(key_data.skv_groups):
        key_data.skv_group_index = 0

    valid_kb_names = {kb.name for kb in key_data.key_blocks}
    kd_prune_group_map(key_data, valid_kb_names)

    for kb in key_data.key_blocks:
        cur = kd_get_group(key_data, kb.name)
        if cur == INIT_GROUP_NAME:
            legacy = kb_get_group_legacy(kb)
            if legacy in names and legacy != INIT_GROUP_NAME:
                kd_set_group(key_data, kb.name, legacy)
                continue
        if cur not in names:
            kd_set_group(key_data, kb.name, INIT_GROUP_NAME)


# Presets (helpers)
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
        for it in preset.items:
            if not it.name:
                continue
            kb = kb_map.get(it.name)
            if not kb:
                continue
            try:
                kb.value = factor * float(it.max_value)
            except Exception:
                pass
    finally:
        _PRESET_APPLY_GUARD = False

    tag_redraw_view3d(context)


def preset_value_update(self, context):
    preset_apply(self, context)


def get_active_preset(key_data):
    if not key_data or not hasattr(key_data, "skv_presets") or not hasattr(key_data, "skv_preset_index"):
        return None
    idx = int(key_data.skv_preset_index)
    if 0 <= idx < len(key_data.skv_presets):
        return key_data.skv_presets[idx]
    return None
