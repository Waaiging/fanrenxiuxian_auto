from dataclasses import dataclass


DEFAULT_MAIN_FIELD_TRAINING_COMMAND = ".野外历练 谨慎"
DEFAULT_AVATAR_FIELD_TRAINING_COMMAND = ".野外历练"
YUANYING_OUT_COMMAND = ".元婴出窍"
YUANYING_RETREAT_COMMAND = ".元婴闭关"
RIFT_SEARCH_COMMAND = ".探寻裂缝"
DEFAULT_TREASURE_TOUCH_COMMAND = ".抚摸法宝 青竹蜂云剑"
NURTURE_SPIRIT_COMMAND = ".温养器灵 斩灵"
ASK_DAO_COMMAND = ".问道"


@dataclass(frozen=True)
class CommandStep:
    command: str
    delay_after: float = 0


@dataclass(frozen=True)
class FieldTrainingPlan:
    identity: str
    command: str
    pre_steps: tuple[CommandStep, ...] = ()
    timeout: int = 90
    max_retries: int = 0
    force_identity_check: bool = True
    suppress_no_response_alert: bool = True
    return_response_msg: bool = True

    def all_commands(self):
        return [step.command for step in self.pre_steps] + [self.command]


@dataclass(frozen=True)
class TimedCommandPlan:
    identity: str
    command: str
    last_key: str
    next_key: str
    timeout: int = 120
    max_retries: int = 0
    force_identity_check: bool = True
    return_response_msg: bool = False


def join_command(*parts):
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip())


def field_training_plan_from_features(
    identity="主魂",
    features=None,
    main_command=DEFAULT_MAIN_FIELD_TRAINING_COMMAND,
    default_avatar_command=DEFAULT_AVATAR_FIELD_TRAINING_COMMAND,
):
    identity = str(identity or "主魂").strip() or "主魂"
    features = dict(features or {})
    if identity == "主魂":
        return FieldTrainingPlan(identity=identity, command=str(main_command or DEFAULT_MAIN_FIELD_TRAINING_COMMAND).strip())

    base_command = features.get("training_cmd")
    if not base_command:
        base_command = default_avatar_command
    command = join_command(base_command, features.get("training_level", ""))

    prefix_commands = features.get("training_prefix_commands")
    if prefix_commands is None:
        meditation_prefix = str(features.get("meditation_prefix") or "").strip()
        prefix_commands = [f"{meditation_prefix} 探索"] if meditation_prefix else []
    pre_steps = tuple(
        CommandStep(str(command).strip(), delay_after=3)
        for command in prefix_commands
        if str(command or "").strip()
    )
    return FieldTrainingPlan(identity=identity, command=command, pre_steps=pre_steps)


def yuanying_command_for_identity(identity="主魂", main_command=YUANYING_OUT_COMMAND):
    identity = str(identity or "主魂").strip() or "主魂"
    if identity == "主魂":
        return str(main_command or YUANYING_OUT_COMMAND).strip() or YUANYING_OUT_COMMAND
    return YUANYING_OUT_COMMAND


def yuanying_out_plan(identity="主魂", main_command=YUANYING_OUT_COMMAND):
    identity = str(identity or "主魂").strip() or "主魂"
    return TimedCommandPlan(
        identity=identity,
        command=yuanying_command_for_identity(identity, main_command=main_command),
        last_key="last_yuanying_out_time",
        next_key="next_yuanying_out_time",
        timeout=120,
        max_retries=0,
        force_identity_check=identity != "主魂",
        return_response_msg=False,
    )


def rift_search_plan(identity="主魂"):
    identity = str(identity or "主魂").strip() or "主魂"
    return TimedCommandPlan(
        identity=identity,
        command=RIFT_SEARCH_COMMAND,
        last_key="last_rift_search_time",
        next_key="next_rift_search_time",
        timeout=120,
        max_retries=0,
        force_identity_check=identity != "主魂",
        return_response_msg=identity == "主魂",
    )


def treasure_touch_plan(command=DEFAULT_TREASURE_TOUCH_COMMAND):
    return TimedCommandPlan(
        identity="主魂",
        command=str(command or DEFAULT_TREASURE_TOUCH_COMMAND).strip() or DEFAULT_TREASURE_TOUCH_COMMAND,
        last_key="last_treasure_touch_time",
        next_key="next_treasure_touch_time",
        timeout=90,
        max_retries=0,
        force_identity_check=True,
    )


def nurture_spirit_plan(command=NURTURE_SPIRIT_COMMAND):
    return TimedCommandPlan(
        identity="主魂",
        command=str(command or NURTURE_SPIRIT_COMMAND).strip() or NURTURE_SPIRIT_COMMAND,
        last_key="last_nurture_spirit_time",
        next_key="next_nurture_spirit_time",
        timeout=90,
        max_retries=0,
        force_identity_check=False,
    )


def ask_dao_plan(command=ASK_DAO_COMMAND):
    return TimedCommandPlan(
        identity="主魂",
        command=str(command or ASK_DAO_COMMAND).strip() or ASK_DAO_COMMAND,
        last_key="last_ask_dao_time",
        next_key="next_ask_dao_time",
        timeout=90,
        max_retries=1,
        force_identity_check=True,
    )
