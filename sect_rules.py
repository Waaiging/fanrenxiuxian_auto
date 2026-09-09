"""Shared membership and pause checks for sect-specific automation."""

SECT_COMMAND_ROOTS = {
    "太一门": (".引道",),
    "元婴宗": (".问道",),
    "天星宗": (".观命", ".定命", ".推命", ".改命"),
    "凌霄宫": (".天阶状态", ".登天阶", ".引九天罡风", ".问心台"),
    "阴罗宗": (".我的阴罗幡", ".每日献祭", ".血洗山林", ".召唤魔影", ".召回魔影",
               ".一键安抚幡灵", ".安抚幡灵", ".收取精华", ".囚禁魂魄", ".化功为煞",
               ".接取解咒委托", ".辨认咒纹", ".借幡镇魂", ".剥离咒源"),
    "星宫": (".观星", ".改换星移", ".一键收取精华"),
    "万灵宗": (".寻觅灵兽", "miniapp:spirit-beast-contract",
               "miniapp:spirit-beast-abyss", "miniapp:spirit-beast-release", "miniapp:spirit-beast-rest"),
}


class SectTaskStopped(Exception):
    """The identity changed sect or was paused while an operation was waiting."""


def command_sect(command):
    command = str(command or "").strip()
    if command == ".双修 温养":
        return "合欢宗"
    root = command.split()[0] if command else ""
    return next((sect for sect, roots in SECT_COMMAND_ROOTS.items() if root in roots), "")


def resolve_identity(actor, identity):
    identity = str(identity or "主魂").strip() or "主魂"
    resolver = getattr(actor, "resolve_avatar_identity", None)
    return resolver(identity) if callable(resolver) else identity


def identity_names(actor):
    avatars = getattr(actor, "avatars", None)
    if avatars is None:
        avatars = (getattr(actor, "state", {}) or {}).get("avatars", {})
    return list(dict.fromkeys(resolve_identity(actor, n) for n in ["主魂", *(avatars or [])]))


def identity_sect(actor, identity):
    identity = resolve_identity(actor, identity)
    resolver = getattr(actor, "identity_sect_name", None)
    if callable(resolver):
        return str(resolver(identity) or "").strip()
    root = getattr(actor, "state", {}) or {}
    mapping = getattr(actor, "identity_sect_names", None)
    if not isinstance(mapping, dict):
        mapping = root.get("identity_sect_names") or {}
    state = root if identity == "主魂" else (root.get("avatars") or {}).get(identity, {})
    return str(mapping.get(identity) or state.get("sect_name") or state.get("miniapp_sect_name")
               or (getattr(actor, "sect_name", "") if identity == "主魂" else "") or "").strip()


def task_paused(actor, identity, command=""):
    identity = resolve_identity(actor, identity)
    if (getattr(actor, "state", {}) or {}).get("is_paused"):
        return True
    pause = getattr(actor, "pause_event", None)
    if pause is not None and not pause.is_set():
        return True
    checker = getattr(actor, "identity_pause_seconds", None)
    if callable(checker) and checker(identity) > 0:
        return True
    checker = getattr(actor, "dashboard_command_paused", None)
    return bool(command and callable(checker) and checker(command, identity))


def command_allowed(actor, identity, command):
    sect = command_sect(command)
    if not sect:
        return True
    identity = resolve_identity(actor, identity)
    return (identity in identity_names(actor) and identity_sect(actor, identity) == sect
            and not task_paused(actor, identity, command))


def require_command(actor, identity, command):
    if not command_allowed(actor, identity, command):
        raise SectTaskStopped(command)
