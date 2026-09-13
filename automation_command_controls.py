"""Pause keys for page-native operations, independent of display taxonomy.

These keys only veto existing automation. They never select participants,
create schedules, change membership checks, or authorize a retired command.
"""
from sect_rules import SectTaskStopped


PAGODA = "miniapp:pagoda"
HUNT = ".洞府寻宝"
TRIAL = ".天机试炼"
FATE_CARDS = ".天机命脉"
FISHING = "miniapp:fishing"
JOURNEY = "miniapp:journey-deep"
WORLD_BOSS = "miniapp:world-boss"
STAR_FARM = "miniapp:star-farm"
BEAST_SYNC = "miniapp:spirit-beast"
INVENTORY = "miniapp:inventory"
PROFILE = "miniapp:profile"

# Current UI aliases share the same persisted key; old chat-command keys do
# not control their Mini App replacements (notably .闯塔 / .安抚星辰).
CONTROL_ALIASES = {
    "miniapp:spirit-beast:xiaohao": BEAST_SYNC,
    "miniapp:spirit-beast-seek": ".寻觅灵兽",
    ".游历 深入": JOURNEY,
}
CONTROL_PARENTS = {
    **{STAR_FARM + "-" + action: STAR_FARM for action in ("soothe", "collect", "pull")},
    "miniapp:fishing-bait": FISHING,
    "miniapp:fishing-chum": FISHING,
}


class CommandControlPaused(SectTaskStopped):
    """A user pause, not a game error or a completed attempt."""

    def __init__(self, command, identity):
        self.command = command
        self.identity = identity
        super().__init__(f"{identity}: {command}")


def command_paused(actor, command, identity):
    if actor is None or not command:
        return False
    checker = getattr(actor, "dashboard_command_paused", None)
    if callable(checker):
        return bool(checker(command, identity))
    from log_utils import dashboard_command_disabled
    return dashboard_command_disabled(actor, command, identity)[0]


def require_enabled(actor, identity, *commands):
    for command in commands:
        if command_paused(actor, command, identity):
            raise CommandControlPaused(command, identity)


def miniapp_request_commands(path, payload=None):
    """Map a real API action to the switches checked just before transport.

    Authentication is deliberately absent. Fishing receipts/finishes are
    allowed to settle an already-issued cast after new casts are paused.
    """
    payload = payload or {}
    action = payload.get("action", "")
    dwelling = "/api/miniapp/xianxia-dwelling/"
    if path.startswith(dwelling):
        route = path[len(dwelling):]
        if route == "command-center":
            return (str(payload.get("command") or ""),)
        if route == "cultivation":
            return (".闭关修炼",)
        if route == "deep-seclusion":
            return ({"start": ".深度闭关", "status": ".查看闭关",
                     "force": ".强行出关", "settle": "miniapp:meditation-settle"}.get(action, ""),)
        if route == "journey":
            return (JOURNEY,)
        if route == "hunt" or route.startswith("hunt/"):
            return (HUNT,)
        if route == "small-world":
            return ({"manifest": ".显灵", "soothe": ".安抚信徒",
                     "miracle_sermon": ".神迹 布道", "miracle_relief": ".神迹 赈灾",
                     "collect": "miniapp:small-world-collect"}.get(action, ""),)
        if route == "star-palace":
            return ({"divine": ".观星", "shift_destiny": ".改换星移 @" + str(payload.get("targetUsername") or "").lstrip("@"),
                     "refresh": "miniapp:star-palace"}.get(action, ""),)
        if route == "overview":
            return (PROFILE,)
        if route == "inventory":
            return (INVENTORY,)
        if route == "forge/craft":
            return ("miniapp:forge",)
    if "/xianxia-pagoda/" in path:
        return (PAGODA,)
    if "/xianxia-trial/" in path:
        return (TRIAL,)
    if "/xianxia-fate-cards/" in path:
        return (FATE_CARDS,)
    if "/xianxia-sect-farm/" in path:
        return (STAR_FARM, f"{STAR_FARM}-{action}") if action else (STAR_FARM,)
    if "/xianxia-spirit-beast/" in path:
        command = {"seek": ".寻觅灵兽", "release": "miniapp:spirit-beast-release",
                   "rest": "miniapp:spirit-beast-rest", "interact": "miniapp:spirit-beast-contract",
                   "active": ".灵兽出战 *"}.get(action)
        return (command or ("miniapp:spirit-beast-abyss" if path.endswith("/abyss/enter") else BEAST_SYNC),)
    if "/xianxia-fishing/" in path:
        operation = path.rsplit("/", 1)[-1]
        if operation in {"finish", "result"}:
            return ()
        extra = {"buy-bait": "miniapp:fishing-bait", "chum": "miniapp:fishing-chum"}.get(operation)
        return (FISHING, extra) if extra else (FISHING,)
    return ()
