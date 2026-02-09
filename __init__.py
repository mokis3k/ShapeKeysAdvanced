bl_info = {
    "name": "Shape Keys Viewer",
    "author": "xtafr001",
    "version": (0, 5, 5),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > ShapeKeys",
    "description": "Shape keys grouping, selection tools, and presets (multi-key control).",
    "category": "Object",
}

import re
import bpy
from bpy.types import Operator, Panel, PropertyGroup, UIList, Menu
from bpy.props import (
    BoolProperty,
    IntProperty,
    FloatProperty,
    StringProperty,
    PointerProperty,
    CollectionProperty,
    EnumProperty,
)

INIT_GROUP_NAME = "Init"

# Prevent recursion when preset slider writes to key values
_PRESET_APPLY_GUARD = False


# -----------------------------
# Utilities
# -----------------------------
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
    # Split by comma/semicolon; trim; drop empties.
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
    # Clear selected keys + disable Select mode (hide checkboxes)
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    if hasattr(context, "scene") and hasattr(context.scene, "skv_props"):
        context.scene.skv_props.show_select = False


def show_select_update(self, context):
    # Clear selection when toggling Select mode (either on or off)
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if key_data and hasattr(key_data, "skv_selected"):
        key_data.skv_selected.clear()
    tag_redraw_view3d(context)


# -----------------------------
# Legacy (old versions) group storage on KeyBlock ID props (read-only fallback)
# -----------------------------
def kb_get_group_legacy(kb) -> str:
    try:
        v = kb.get("skv_group", INIT_GROUP_NAME)
        return v if v else INIT_GROUP_NAME
    except Exception:
        return INIT_GROUP_NAME


# -----------------------------
# Group mapping on Key datablock: Key.skv_key_groups (name -> group)
# -----------------------------
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


# -----------------------------
# Multi-select storage on Key datablock (name list)
# -----------------------------
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


# -----------------------------
# Counts
# -----------------------------
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


# -----------------------------
# Init / scan sync (Operators only)
# -----------------------------
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


# -----------------------------
# Presets
# -----------------------------
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


# -----------------------------
# Data Model
# -----------------------------
class SKV_Group(PropertyGroup):
    name: StringProperty(name="Name", default="Group")


class SKV_SelectedName(PropertyGroup):
    name: StringProperty(name="Name", default="")


class SKV_KeyGroupEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    group: StringProperty(name="Group", default=INIT_GROUP_NAME)


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


class SKV_Props(PropertyGroup):
    # Keep -1 to avoid "active" highlight in the list
    keys_index: IntProperty(name="Keys Index", default=-1, min=-1)
    search: StringProperty(name="Search", default="")
    show_select: BoolProperty(name="Select", default=False, update=show_select_update)

    keys_open: BoolProperty(name="Keys", default=True)
    presets_open: BoolProperty(name="Presets", default=False)
    transfer_open: BoolProperty(name="Mesh Data Transfer", default=False)
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


# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_Groups(UIList):
    bl_idname = "SKV_UL_groups"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        group = item
        key_data = data
        row = layout.row(align=True)
        row.label(text=group.name, icon="FILE_FOLDER")
        row.label(text=str(count_keys_in_group(key_data, group.name)))


class SKV_UL_KeyBlocks(UIList):
    bl_idname = "SKV_UL_key_blocks"

    def filter_items(self, context, data, propname):
        key_data = data
        items = getattr(key_data, propname)
        flags = [0] * len(items)
        neworder = []

        selected_group = get_selected_group_name(key_data)
        search = context.scene.skv_props.search.strip().lower()

        for i, kb in enumerate(items):
            if kd_get_group(key_data, kb.name) != selected_group:
                flags[i] = 0
                continue
            if search and search not in kb.name.lower():
                flags[i] = 0
                continue
            flags[i] = self.bitflag_filter_item

        return flags, neworder

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        kb = item
        key_data = data
        props = context.scene.skv_props

        row = layout.row(align=True)

        if props.show_select:
            icon_id = "CHECKBOX_HLT" if kd_is_selected(key_data, kb.name) else "CHECKBOX_DEHLT"
            op = row.operator("skv.key_toggle_select", text="", icon=icon_id, emboss=False)
            op.key_index = index

        row.prop(kb, "value", text=kb.name, slider=True)


class SKV_UL_Presets(UIList):
    bl_idname = "SKV_UL_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)
        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

        # Capture Max button per preset row (icon changed from previous version)
        op = row.operator("skv.preset_capture_max_index", text="", icon="COPYDOWN", emboss=True)
        op.preset_index = index


class SKV_UL_PresetItems(UIList):
    bl_idname = "SKV_UL_preset_items"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Left align in UIList: split the row and force alignment on the left cell.
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

        # Slider controls actual Shape Key value (same as in group list)
        layout.prop(kb, "value", text=item.name, slider=True)


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


class SKV_OT_InitRescan(Operator):
    bl_idname = "skv.init_rescan"
    bl_label = "Scan/Rescan"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "Active object has no shape keys support.")
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


class SKV_OT_KeyToggleSelect(Operator):
    bl_idname = "skv.key_toggle_select"
    bl_label = "Toggle Shape Key Selection"
    bl_options = {"REGISTER", "UNDO"}

    key_index: IntProperty()

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        if 0 <= self.key_index < len(key_data.key_blocks):
            kb = key_data.key_blocks[self.key_index]
            kd_set_selected(key_data, kb.name, not kd_is_selected(key_data, kb.name))

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_SelectVisible(Operator):
    bl_idname = "skv.select_visible"
    bl_label = "Select Visible"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=[
            ("ALL", "All", ""),
            ("NONE", "Clear", ""),
            ("INVERT", "Invert", ""),
        ],
        default="ALL",
    )

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        props = context.scene.skv_props
        selected_group = get_selected_group_name(key_data)
        search = props.search.strip().lower()

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) != selected_group:
                continue
            if search and search not in kb.name.lower():
                continue

            if self.mode == "ALL":
                kd_set_selected(key_data, kb.name, True)
            elif self.mode == "NONE":
                kd_set_selected(key_data, kb.name, False)
            else:
                kd_set_selected(key_data, kb.name, not kd_is_selected(key_data, kb.name))

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_SelectByAffix(Operator):
    bl_idname = "skv.select_by_affix"
    bl_label = "Select By Prefix/Suffix"
    bl_options = {"REGISTER", "UNDO"}

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

        props = context.scene.skv_props
        selected_group = get_selected_group_name(key_data)

        tokens = parse_tokens(props.affix_value)
        if not tokens:
            self.report({"INFO"}, "No prefix/suffix provided.")
            return {"CANCELLED"}

        kd_clear_selected(key_data)

        if props.affix_type == "PREFIX":

            def match(name: str) -> bool:
                return any(name.startswith(t) for t in tokens)

        else:

            def match(name: str) -> bool:
                return any(name.endswith(t) for t in tokens)

        selected_any = 0
        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) != selected_group:
                continue
            if match(kb.name):
                kd_set_selected(key_data, kb.name, True)
                selected_any += 1

        if selected_any == 0:
            self.report({"INFO"}, "No shape keys matched.")
            return {"CANCELLED"}

        context.scene.skv_props.affix_value = ""

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_MoveSelectedToGroup(Operator):
    bl_idname = "skv.move_selected_to_group"
    bl_label = "Move Selected To Group"
    bl_options = {"REGISTER", "UNDO"}

    group: EnumProperty(name="Group", items=enum_groups_for_active_object)

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

        ensure_init_setup_write(obj)

        names = group_names(key_data)
        if self.group not in names:
            self.group = INIT_GROUP_NAME

        selected = kd_selected_set(key_data)
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        moved = 0
        for kb in key_data.key_blocks:
            if kb.name in selected:
                kd_set_group(key_data, kb.name, self.group)
                moved += 1

        if moved == 0:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        clear_selection_ui(context, key_data)

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

        changed = 0
        for kb in key_data.key_blocks:
            if kb.name not in selected:
                continue
            if kd_get_group(key_data, kb.name) != group_name:
                continue
            try:
                kb.value = 0.0
                changed += 1
            except Exception:
                pass

        if changed == 0:
            self.report({"INFO"}, "No keys were reset.")
            return {"CANCELLED"}

        kd_clear_selected(key_data)

        tag_redraw_view3d(context)
        return {"FINISHED"}


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


# --- Preset operators ---
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


# --- Group operators ---
class SKV_OT_GroupAdd(Operator):
    bl_idname = "skv.group_add"
    bl_label = "Add Group"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            self.report({"ERROR"}, "Group storage not available.")
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        new_name = self.name.strip()
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        names = group_names(key_data)
        if new_name in names:
            self.report({"WARNING"}, "Group with this name already exists.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name

        # Keep current group (fix #1)
        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

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
        # Reuse existing logic by calling the original operator class implementation.
        return bpy.ops.skv.preset_capture_max()


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

        name = key_data.skv_groups[idx].name
        if name == INIT_GROUP_NAME:
            self.report({"WARNING"}, "Cannot remove 'Init' group.")
            return {"CANCELLED"}

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == name:
                kd_set_group(key_data, kb.name, INIT_GROUP_NAME)

        key_data.skv_groups.remove(idx)
        if key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = max(0, len(key_data.skv_groups) - 1)

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
        if old == INIT_GROUP_NAME:
            self.report({"WARNING"}, "Cannot rename 'Init' group.")
            return {"CANCELLED"}

        new = self.new_name.strip()
        if not new:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        names = group_names(key_data)
        if new in names and new != old:
            self.report({"WARNING"}, "Group with this name already exists.")
            return {"CANCELLED"}

        key_data.skv_groups[idx].name = new

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == old:
                kd_set_group(key_data, kb.name, new)

        tag_redraw_view3d(context)
        return {"FINISHED"}



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

        for g in key_data.skv_groups:
            op = layout.operator("skv.move_selected_to_group", text=g.name, icon="FILE_FOLDER")
            op.group = g.name


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



class SKV_MT_SelectActions(Menu):
    bl_label = "Actions"
    bl_idname = "SKV_MT_select_actions"

    def draw(self, context):
        layout = self.layout
        layout.menu("SKV_MT_move_to_group", text="Move to group", icon="FILE_FOLDER")
        layout.operator("skv.create_group_from_selected", text="Create new group", icon="NEWFOLDER")
        layout.menu("SKV_MT_add_to_preset", text="Add to preset", icon="PRESET")
        layout.operator("skv.preset_add_from_selected", text="Create new preset", icon="PRESET")
        layout.operator("skv.reset_group_values", text="Zero selected values", icon="RECOVER_LAST")


# -----------------------------
# Operators (UI helpers)
# -----------------------------

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

class SKV_OT_CreateGroupFromSelected(Operator):
    bl_idname = "skv.create_group_from_selected"
    bl_label = "Create Group From Selected"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
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

        new_name = (self.name or "").strip()
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        names = group_names(key_data)
        if new_name in names:
            self.report({"WARNING"}, "Group with this name already exists.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name

        # Keep current group selection
        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

        moved = 0
        for kb in key_data.key_blocks:
            if kb.name in selected:
                kd_set_group(key_data, kb.name, new_name)
                moved += 1

        if moved == 0:
            # Rollback group if nothing moved (should not happen)
            for i, gg in enumerate(key_data.skv_groups):
                if gg.name == new_name:
                    key_data.skv_groups.remove(i)
                    break
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}

# -----------------------------
# Panel
# -----------------------------
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

        obj = get_active_object(context)

        # CONTEXT (always visible)
        box_ctx = layout.box()
        row = box_ctx.row(align=True)
        row.label(text="OBJECT", icon="OBJECT_DATA")

        if not obj:
            box_ctx.label(text="No supported active mesh")
            return

        row2 = box_ctx.row(align=True)
        row2.label(text=obj.name, icon="MESH_DATA")

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            layout.label(text="No Shape Keys for selected object")
            return

        if not has_group_storage(key_data):
            layout.label(text="Group storage not available.")
            return

        initialized = is_initialized(key_data)

        # Small Rescan button only after Scan
        if initialized:
            row2.operator("skv.init_rescan", text="", icon="FILE_REFRESH")

        if not initialized:
            layout.separator()
            layout.operator("skv.init_rescan", text="Scan", icon="FILE_REFRESH")
            return

        current_group = get_selected_group_name(key_data) if initialized else INIT_GROUP_NAME

        # GROUP WORKSPACE (always visible)
        box_ws = layout.box()
        box_ws.label(text="GROUPS", icon="GROUP")

        # a) Groups
        box_groups = box_ws.box()
        box_groups.label(text="Groups")

        if initialized:
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
        else:
            box_groups.label(text="Not initialized. Use Scan below.")

        # b) Keys in "Group Name" (collapsible)
        box_keys = box_ws.box()
        head = box_keys.row(align=True)
        icon = "TRIA_DOWN" if props.keys_open else "TRIA_RIGHT"
        head.prop(props, "keys_open", text="", emboss=False, icon=icon)
        head.label(text=f"Keys in '{current_group}'")

        if initialized and props.keys_open:
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

        # PRESETS (collapsible)
        boxp = layout.box()
        headp = boxp.row(align=True)
        iconp = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
        headp.prop(props, "presets_open", text="", emboss=False, icon=iconp)
        headp.label(text="PRESETS")

        if initialized and props.presets_open:
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

        # MESH DATA TRANSFER (collapsible placeholder)
        boxt = layout.box()
        headt = boxt.row(align=True)
        icont = "TRIA_DOWN" if props.transfer_open else "TRIA_RIGHT"
        headt.prop(props, "transfer_open", text="", emboss=False, icon=icont)
        headt.label(text="MESH DATA TRANSFER")

        if props.transfer_open:
            boxt.label(text="None", icon="MOD_DATA_TRANSFER")




# -----------------------------
# Registration
# -----------------------------
classes = (
    SKV_Group,
    SKV_SelectedName,
    SKV_KeyGroupEntry,
    SKV_PresetItem,
    SKV_Preset,
    SKV_Props,
    SKV_UL_Groups,
    SKV_UL_KeyBlocks,
    SKV_UL_Presets,
    SKV_UL_PresetItems,
    SKV_UL_PresetKeySliders,
    SKV_OT_SearchClear,
    SKV_OT_InitRescan,
    SKV_OT_KeyToggleSelect,
    SKV_OT_SelectVisible,
    SKV_OT_SelectByAffix,
    SKV_OT_MoveSelectedToGroup,
    SKV_OT_ResetGroupValues,
    SKV_OT_PresetFocusKey,
    SKV_OT_PresetAddFromSelected,
    SKV_OT_PresetAddEmpty,
    SKV_OT_PresetRemove,
    SKV_OT_PresetRename,
    SKV_OT_PresetCaptureMax,
    SKV_OT_PresetCaptureMaxIndex,
    SKV_OT_GroupAdd,
    SKV_OT_GroupRemove,
    SKV_OT_GroupRename,
    SKV_MT_MoveToGroup,
    SKV_MT_AddToPreset,
    SKV_MT_SelectActions,
    SKV_OT_AddSelectedToPreset,
    SKV_OT_CreateGroupFromSelected,
    SKV_PT_ShapeKeysPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.skv_props = PointerProperty(type=SKV_Props)

    bpy.types.Key.skv_groups = CollectionProperty(type=SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(name="Group Index", default=0, min=0)

    bpy.types.Key.skv_selected = CollectionProperty(type=SKV_SelectedName)
    bpy.types.Key.skv_key_groups = CollectionProperty(type=SKV_KeyGroupEntry)

    bpy.types.Key.skv_presets = CollectionProperty(type=SKV_Preset)
    bpy.types.Key.skv_preset_index = IntProperty(name="Preset Index", default=0, min=0)


def unregister():
    del bpy.types.Key.skv_preset_index
    del bpy.types.Key.skv_presets
    del bpy.types.Key.skv_key_groups
    del bpy.types.Key.skv_selected
    del bpy.types.Key.skv_groups
    del bpy.types.Key.skv_group_index
    del bpy.types.Scene.skv_props

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
