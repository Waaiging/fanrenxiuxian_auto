"""Private, expiring checkpoints for interrupted world-boss battles."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from world_boss_turnstile import _atomic_write


class WorldBossRecoveryStore:
    def __init__(self, directory: Path, account: str, *, clock=time.time):
        self.directory = Path(directory)
        self.account = re.sub(r"[^a-z0-9_-]", "", account.lower())
        if not self.account:
            raise ValueError("invalid_world_boss_account")
        self.clock = clock

    def _path(self, fingerprint: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("invalid_world_boss_fingerprint")
        return self.directory / f"{self.account}_{fingerprint}.json"

    def save(self, checkpoint: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self._path(checkpoint["entry"]["fingerprint"])
        _atomic_write(path, json.dumps(checkpoint, ensure_ascii=False) + "\n")

    def load(self, fingerprint: str) -> dict[str, Any] | None:
        path = self._path(fingerprint)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entry = data["entry"]
            valid = (
                data.get("version") == 1
                and data.get("account") == self.account
                and float(data.get("expires_epoch") or 0) > self.clock()
                and entry["fingerprint"] == fingerprint
                and hashlib.sha256(entry["token"].encode("utf-8")).hexdigest() == fingerprint
                and data.get("stage") in {"fighting", "finish_pending"}
            )
            if valid:
                return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        self.delete(fingerprint)
        return None

    def list_pending(self) -> list[dict[str, Any]]:
        result = []
        for path in self.directory.glob(f"{self.account}_*.json"):
            fingerprint = path.stem[len(self.account) + 1:]
            if re.fullmatch(r"[a-f0-9]{64}", fingerprint):
                checkpoint = self.load(fingerprint)
                if checkpoint:
                    result.append(checkpoint)
        return result

    def delete(self, fingerprint: str) -> None:
        self._path(fingerprint).unlink(missing_ok=True)
