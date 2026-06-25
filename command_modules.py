from dataclasses import dataclass


DEFAULT_MAIN_FIELD_TRAINING_COMMAND = ".野外历练 谨慎"
DEFAULT_AVATAR_FIELD_TRAINING_COMMAND = ".野外历练"


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
