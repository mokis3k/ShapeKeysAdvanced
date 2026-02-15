import bpy
from bpy.types import Operator, PropertyGroup, UIList, Menu
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    CollectionProperty,
)

from .common import (
    INIT_GROUP_NAME,
    get_active_object,
    get_shape_key_data,
    has_group_storage,
    group_names,
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
)


# -----------------------------
# Data Model (groups + selection + group mapping)
# -----------------------------
class SKV_Group(PropertyGroup):
    name: StringProperty(name="Name", default="Group")


class SKV_SelectedName(PropertyGroup):
    name: StringProperty(name="Name", default="")


class SKV_KeyGroupEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    group: StringProperty(name="Group", default=INIT_GROUP_NAME)


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






        vis_icon = "HIDE_ON" if getattr(kb, "mute", False) else "HIDE_OFF"
        opv = row.operator("skv.shape_key_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.key_name = kb.name

        opd = row.operator("skv.shape_key_delete", text="", icon="TRASH", emboss=False)
        opd.key_name = kb.name

class SKV_OT_ShapeKeyToggleVisibility(Operator):
    bl_idname = "skv.shape_key_toggle_visibility"
    bl_label = "Toggle Shape Key Visibility"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Shape Key Name", default="")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        return bool(obj and get_shape_key_data(obj))

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(self.key_name)
        if kb is None:
            self.report({"WARNING"}, "Shape key not found.")
            return {"CANCELLED"}

        # KeyBlock.mute is used as "disabled/hidden" state.
        kb.mute = not bool(getattr(kb, "mute", False))
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ShapeKeyDelete(Operator):
    bl_idname = "skv.shape_key_delete"
    bl_label = "Delete Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Shape Key Name", default="")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        return bool(obj and get_shape_key_data(obj))

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(self.key_name)
        if kb is None:
            self.report({"WARNING"}, "Shape key not found.")
            return {"CANCELLED"}

        if kb.name == "Basis":
            self.report({"WARNING"}, "Cannot delete Basis shape key.")
            return {"CANCELLED"}

        try:
            obj.shape_key_remove(kb)
        except Exception as ex:
            self.report({"ERROR"}, f"Failed to delete shape key: {ex}")
            return {"CANCELLED"}

        # Reconcile addon storage with current key_blocks.
        ensure_init_setup_write(obj)

        # Keep UI index valid.
        try:
            props = context.scene.skv_props
            if props.keys_index < 0:
                props.keys_index = 0
        except Exception:
            pass

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
# Operators
# -----------------------------
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

    mode: bpy.props.EnumProperty(
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

        raw_affix = props.affix_value
        tokens = parse_tokens(raw_affix)
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

        # Track the last applied affix and mark it as pending for name prefills.
        props.last_affix_name = raw_affix.strip()
        props.last_affix_pending = True

        # Clear input for next usage.
        props.affix_value = ""

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_MoveSelectedToGroup(Operator):
    bl_idname = "skv.move_selected_to_group"
    bl_label = "Move Selected To Group"
    bl_options = {"REGISTER", "UNDO"}

    group: bpy.props.EnumProperty(name="Group", items=enum_groups_for_active_object)

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

        # Keep current group
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

        name = key_data.skv_groups[idx].name
        if name == INIT_GROUP_NAME:
            self.report({"WARNING"}, "Cannot remove 'Main' group.")
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
            self.report({"WARNING"}, "Cannot rename 'Main' group.")
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


class SKV_OT_CreateGroupFromSelected(Operator):
    bl_idname = "skv.create_group_from_selected"
    bl_label = "Create Group From Selected"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
        # Prefill from last applied affix, only if it is pending.
        props = getattr(context.scene, "skv_props", None)
        if props and props.last_affix_pending and props.last_affix_name.strip():
            self.name = props.last_affix_name.strip()
            # One-shot: clear pending so other creates revert to defaults unless Apply is used again.
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


CLASSES = (
    SKV_Group,
    SKV_SelectedName,
    SKV_KeyGroupEntry,
    SKV_UL_Groups,
    SKV_UL_KeyBlocks,
    SKV_MT_MoveToGroup,
    SKV_MT_SelectActions,
    SKV_OT_KeyToggleSelect,
    SKV_OT_SelectVisible,
    SKV_OT_SelectByAffix,
    SKV_OT_MoveSelectedToGroup,
    SKV_OT_ResetGroupValues,
    SKV_OT_GroupAdd,
    SKV_OT_GroupRemove,
    SKV_OT_GroupRename,
    SKV_OT_CreateGroupFromSelected,
    SKV_OT_ShapeKeyToggleVisibility,
    SKV_OT_ShapeKeyDelete,
)
