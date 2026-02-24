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


# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_Groups(UIList):
    bl_idname = "SKV_UL_groups"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        g = item
        key_data = data

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
        tokens = parse_tokens(getattr(props, "key_search", ""))

        flt_flags = []
        flt_neworder = []

        bf = self.bitflag_filter_item
        for kb in getattr(key_data, propname):
            ok = True

            # Filter by group membership
            if group_name:
                if kd_get_group(key_data, kb.name) != group_name:
                    ok = False

            # Filter by search tokens
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
        props = context.scene.skv_props

        row = layout.row(align=True)

        # Selection checkbox (only in Select mode)
        if props.show_select:
            icon_id = "CHECKBOX_HLT" if kd_is_selected(key_data, kb.name) else "CHECKBOX_DEHLT"
            op = row.operator("skv.key_toggle_select", text="", icon=icon_id, emboss=False)
            op.key_index = index

        # Shape key slider / name
        row.prop(kb, "value", text=kb.name, slider=True)

        # Visibility toggle (mute)
        vis_icon = "HIDE_ON" if kb.mute else "HIDE_OFF"
        opv = row.operator("skv.shape_key_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.key_name = kb.name

        # Auto keyframe toggle
        entry = _autokf_get_entry(key_data, kb.name, create=False)
        kf_enabled = bool(entry.enabled) if entry else False
        kf_icon = "KEYFRAME_HLT" if kf_enabled else "KEYFRAME"
        opk = row.operator("skv.auto_keyframe_toggle", text="", icon=kf_icon, emboss=False)
        opk.key_name = kb.name


class SKV_UL_active_keys(UIList):
    bl_idname = "SKV_UL_active_keys"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # data is Key datablock, item is SKV_ActiveKeyEntry
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
            row.prop(kb, "value", text=kname, slider=True)
        else:
            row.label(text=kname or "Invalid", icon="ERROR")

        # Make the remove button more visible
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

        for g in key_data.skv_groups:
            op = layout.operator("skv.move_selected_to_group", text=g.name, icon="FILE_FOLDER")
            op.group = g.name


class SKV_MT_SelectActions(Menu):
    bl_label = "Actions"
    bl_idname = "SKV_MT_select_actions"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Not used (actions are displayed inline).")


# -----------------------------
# Operators
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

        kb = None
        try:
            kb = key_data.key_blocks.get(name)
        except Exception:
            kb = None

        # 1) Remove from active list
        try:
            for i, it in enumerate(list(key_data.skv_active_keys)):
                if it.name == name:
                    key_data.skv_active_keys.remove(i)
                    break
        except Exception:
            pass

        # 2) Update default value to current value so it won't re-appear
        #    unless changed again in the future.
        if kb is not None:
            cur_val = 0.0
            try:
                cur_val = float(kb.value)
            except Exception:
                cur_val = 0.0

            updated = False
            try:
                for it in key_data.skv_key_defaults:
                    if it.name == name:
                        it.value = cur_val
                        updated = True
                        break
            except Exception:
                updated = False

            if not updated:
                try:
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
        if not kb:
            self.report({"WARNING"}, "Shape Key not found")
            return {"CANCELLED"}

        kb.mute = not kb.mute
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_AutoKeyframeToggle(Operator):
    bl_idname = "skv.auto_keyframe_toggle"
    bl_label = "Toggle Auto Keyframe"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Shape Key Name", default="")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        return bool(key_data and getattr(key_data, "key_blocks", None))

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        name = (self.key_name or "").strip()
        if not name:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(name)
        if not kb:
            self.report({"WARNING"}, "Shape Key not found")
            return {"CANCELLED"}

        entry = _autokf_get_entry(key_data, name, create=True)
        entry.enabled = not bool(entry.enabled)

        frame = int(context.scene.frame_current)
        try:
            entry.last_frame = frame
        except Exception:
            pass
        try:
            entry.last_value = float(kb.value)
        except Exception:
            entry.last_value = 0.0

        # When enabling, insert an initial keyframe at current frame.
        if entry.enabled:
            try:
                key_data.keyframe_insert(data_path=f'key_blocks["{kb.name}"].value', frame=frame)
            except Exception:
                pass

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
    bl_label = "Zero Selected Values"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        selected = kd_selected_set(key_data)
        for n in selected:
            if not n or n == "Basis":
                continue
            kb = key_data.key_blocks.get(n)
            if kb:
                try:
                    kb.value = 0.0
                except Exception:
                    pass

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
        from .meshDataTransfer import MeshDataTransfer

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

        mdt = MeshDataTransfer(source=source, target=target, vertex_group=None)
        ok = mdt.transfer_shape_keys(shapekey_names=selected_names)

        if not ok:
            self.report({"WARNING"}, "Nothing transferred")
            return {"CANCELLED"}

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
    SKV_OT_SelectByAffix,
    SKV_OT_MoveSelectedToGroup,
    SKV_OT_ResetGroupValues,
    SKV_OT_GroupAdd,
    SKV_OT_GroupRemove,
    SKV_OT_GroupRename,
    SKV_OT_CreateGroupFromSelected,
    SKV_OT_TransferTo,
)