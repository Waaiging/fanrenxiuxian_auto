"""Complete identity panels with supported commands, without scheduling them.

The catalog supplies display text only. Workers never import this module;
command permissions and feature participation remain in their own policies.
"""
from dashboard_command_catalog import COMMAND_CATALOG, IDENTITY_PAGE_COMMANDS
from automation_command_controls import CONTROL_PARENTS
from log_utils import command_control_candidate_keys, command_control_key


def _applies(entry, account, identity, sect):
    if entry.lifecycle in {"disabled", "retired"}:
        return False
    if entry.category == "sect" and entry.subcategory != sect:
        return False
    main = identity == "主魂"
    # Keep retired account/identity paths out of panels. A generic catalog
    # entry is not evidence that an account has the corresponding workflow.
    if entry.key in {"node", "world-status", "world-manifest", "world-sermon", "world-soothe", "world-collect"}:
        return main and account in {"main", "waaiging"}
    if entry.key in {"treasure-refine", "bottle-status", "bottle-condense", "bottle-nurture", "falling-trial", "wanying"}:
        return main and account == "main"
    if entry.key == "treasure-touch" and account == "waaiging":
        return False
    if entry.key in {"voyage", "voyage-return"}:
        return main and account == "main"
    if entry.key == "yuanying-retreat":
        return main and account == "sub"
    if entry.key == "yuanying-out" and main and account == "sub":
        return False
    if entry.key in {"sect-war", "sect-war-join", "concubine-place", "concubine-status"} and main and account == "sub":
        return False
    if entry.key == "force-exit" and account == "sub" and sect == "阴罗宗":
        return False
    if entry.key in {"formation-start", "formation-assist"}:
        return sect == "星宫"
    if entry.key in {"beast-equip", "beast-patrol"}:
        return sect == "万灵宗"
    if entry.key == "spirit-nurture" and main and account == "main":
        return False
    if entry.key == "concubine-recall":  # disabled_by_local_policy
        return False
    return True


def append_identity_control_commands(account, panel, state):
    identity = panel.get("identity") or "主魂"
    root = state if isinstance(state, dict) else {}
    own = root if identity == "主魂" else (root.get("avatars") or {}).get(identity, {})
    sect = str((root.get("identity_sect_names") or {}).get(identity)
               or own.get("miniapp_sect_name") or own.get("sect_name") or "").strip()
    rows = panel.setdefault("commands", [])
    def display_keys(command):
        parent = CONTROL_PARENTS.get(command_control_key(command))
        return {key for key in command_control_candidate_keys(command) if key != parent}

    existing_keys = {key for row in rows if not row.get("custom") for key in display_keys(row.get("command"))}

    def add(command, label, group, detail="", channel=None, **fields):
        key = command_control_key(command)
        if key in existing_keys:
            return
        row = {"command": command, "label": label, "group": group,
               "status": "按原策略", "tone": "manual", "actionable": False,
               "remaining": "", "at": "", "detail": detail or "满足原有参与设置和触发条件后执行",
               **fields}
        if channel:
            row["execution_channel"] = channel
        rows.append(row)
        existing_keys.update(display_keys(command))

    for entry in COMMAND_CATALOG:
        if not _applies(entry, account, identity, sect):
            continue
        commands = IDENTITY_PAGE_COMMANDS.get(entry.key, entry.commands)
        if entry.key == "identity-switch":
            commands = (f".切换 {identity}",)
        for command in commands:
            label = command.lstrip(".").split(" <", 1)[0] if len(commands) > 1 else entry.label
            add(command, label, entry.subcategory, entry.condition,
                "miniapp" if entry.channel == "miniapp" else "group",
                status={"查询": "按需查询", "流程": "流程内", "事件": "监听触发",
                        "计划": "按计划", "每日": "按参与设置"}.get(entry.trigger, "按原策略"))

    return panel
