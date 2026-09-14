"""Short-lived browser-to-worker handoff for the Qing Yuanzi Turnstile token.

The world-boss API now requires a real Cloudflare Turnstile token for ``/begin``.
The automatic browser worker completes the real widget and submits its callback
token here. The authenticated Dashboard also accepts a manual browser fallback.
The game worker consumes the token once and removes it immediately; acceptance
is recorded separately after the upstream ``/begin`` response.

Only request metadata is exposed by the read API.  Token contents never enter
the normal state files or logs and are kept in a mode-600, short-lived file on
the same host as the workers.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Iterator


_MODULE_DIR = Path(__file__).resolve().parent
_DEFAULT_QUEUE_DIR = Path(
    os.environ.get(
        "WORLD_BOSS_TURNSTILE_QUEUE_DIR",
        str(_MODULE_DIR / ".world_boss_turnstile"),
    )
).resolve()

REQUEST_PREFIX = "request_"
TOKEN_PREFIX = "token_"
REQUEST_SUFFIX = ".json"
TOKEN_SUFFIX = ".txt"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
MAX_TOKEN_LENGTH = 4096
DEFAULT_REQUEST_TTL_SECONDS = 180
STALE_RETENTION_SECONDS = 300
BROWSER_WARMUP_SECONDS = 90
BROWSER_ORIGIN = "https://asc.aiopenai.app"
BROWSER_EVENTS = frozenset({
    "browser_starting", "browser_ready", "page_ready",
    "helper_ready", "widget_ready", "interaction_required", "token_generated",
    "widget_error", "widget_timeout", "unsupported", "expired", "script_error",
    "config_error",
})
BROWSER_FAILURE_CODES = frozenset({
    "browser_executable_missing", "websocket_client_missing", "virtual_display_missing",
    "browser_launch_failed", "browser_connection_timeout", "browser_protocol_error",
    "browser_disconnected", "browser_protocol_timeout", "browser_script_error",
    "browser_origin_not_allowed", "turnstile_script_unavailable", "verification_request_finished",
    "verification_generation_changed", "browser_token_invalid", "turnstile_browser_error",
    "turnstile_browser_unsupported", "turnstile_browser_expired", "turnstile_browser_timeout",
    "turnstile_browser_config_error", "turnstile_request_invalid", "turnstile_request_not_found",
    "turnstile_request_expired", "turnstile_request_already_submitted", "turnstile_token_invalid",
})
FINISHED_STATUSES = frozenset({"consumed", "accepted", "rejected", "expired", "cancelled"})


class TurnstileRequestError(ValueError):
    """A safe, user-facing error for an invalid handoff request."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "turnstile_request_invalid")
        super().__init__(self.code)


def _now_epoch() -> float:
    return time.time()


def _time_text(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _safe_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if not REQUEST_ID_RE.fullmatch(candidate):
        raise TurnstileRequestError("turnstile_request_invalid")
    return candidate


def _safe_text(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text[: max(1, int(limit))]


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        # Create the temporary file with restrictive permissions from the
        # outset.  chmod-after-open alone leaves a brief mode-umask window in
        # which another local user could read a freshly submitted token.
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _queue_lock(path: Path) -> Iterator[None]:
    """Serialize metadata updates across the VPS worker and Dashboard process."""

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            # Tests and local development normally use one process.  The
            # process-local lock below still protects those calls; importing
            # msvcrt here avoids making the Linux deployment depend on it.
            locked = True
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked and os.name != "nt":
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class WorldBossTurnstileBroker:
    """Manage one-shot browser token requests using an ephemeral file queue."""

    def __init__(
        self,
        queue_dir: str | os.PathLike[str] | None = None,
        *,
        clock: Any = _now_epoch,
        request_ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
    ) -> None:
        self.queue_dir = Path(queue_dir or _DEFAULT_QUEUE_DIR).resolve()
        self.clock = clock
        self.request_ttl_seconds = max(30, min(600, int(request_ttl_seconds or 30)))
        self._thread_lock = threading.RLock()

    @property
    def lock_path(self) -> Path:
        return self.queue_dir / ".lock"

    def _ensure_dir(self) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.queue_dir, 0o700)
        except OSError:
            pass

    @staticmethod
    def _request_path(queue_dir: Path, request_id: str) -> Path:
        return queue_dir / f"{REQUEST_PREFIX}{request_id}{REQUEST_SUFFIX}"

    @staticmethod
    def _token_path(queue_dir: Path, request_id: str) -> Path:
        return queue_dir / f"{TOKEN_PREFIX}{request_id}{TOKEN_SUFFIX}"

    def _read_request_unlocked(self, request_id: str) -> dict[str, Any] | None:
        path = self._request_path(self.queue_dir, request_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _write_request_unlocked(self, request: dict[str, Any]) -> None:
        request_id = _safe_id(request.get("request_id"))
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
        _atomic_write(self._request_path(self.queue_dir, request_id), payload)

    def _read_warmup_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads((self.queue_dir / "browser_warmup.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def request_warmup(self, *, event_fingerprint: str, origin: str, notice_epoch: float) -> bool:
        """Load the official page during registration, without creating a token.

        The deadline belongs to the announcement, not to each caller. Four
        accounts, repeated notices and restarts cannot extend it or reopen a
        warmup already used by a real verification request.
        """
        now = float(self.clock())
        try:
            notice = float(notice_epoch)
        except (TypeError, ValueError):
            return False
        if (not re.fullmatch(r"[a-f0-9]{64}", str(event_fingerprint))
                or str(origin).rstrip("/") != BROWSER_ORIGIN
                or not math.isfinite(notice) or not now - BROWSER_WARMUP_SECONDS < notice <= now + 5):
            return False
        with self._thread_lock, _queue_lock(self.lock_path):
            previous = self._read_warmup_unlocked()
            if previous.get("event_fingerprint") == event_fingerprint:
                return previous.get("status") == "pending"
            try:
                if float(previous.get("notice_epoch") or 0) > notice:
                    return False
            except (TypeError, ValueError):
                pass
            _atomic_write(self.queue_dir / "browser_warmup.json", json.dumps({
                "event_fingerprint": event_fingerprint, "origin": BROWSER_ORIGIN,
                "notice_epoch": notice, "expires_epoch": notice + BROWSER_WARMUP_SECONDS,
                "status": "pending",
            }, separators=(",", ":")))
        return True

    def get_warmup(self) -> dict[str, Any] | None:
        with self._thread_lock, _queue_lock(self.lock_path):
            value = self._read_warmup_unlocked()
        try:
            remaining = float(value.get("expires_epoch") or 0) - float(self.clock())
        except (TypeError, ValueError):
            return None
        if (value.get("status") != "pending" or not 0 < remaining <= BROWSER_WARMUP_SECONDS + 5
                or value.get("origin") != BROWSER_ORIGIN
                or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("event_fingerprint")))):
            return None
        return value

    def finish_warmup(self, event_fingerprint: str) -> None:
        with self._thread_lock, _queue_lock(self.lock_path):
            value = self._read_warmup_unlocked()
            if value.get("event_fingerprint") == event_fingerprint and value.get("status") == "pending":
                value["status"] = "used"
                _atomic_write(self.queue_dir / "browser_warmup.json", json.dumps(value, separators=(",", ":")))

    def claim_warmup(self, event_fingerprint: str) -> dict[str, Any] | None:
        """Keep the two-prewarm limit across verifier restarts as well."""
        with self._thread_lock, _queue_lock(self.lock_path):
            value = self._read_warmup_unlocked()
            try:
                attempts = int(value.get("attempts") or 0)
                remaining = float(value.get("expires_epoch") or 0) - float(self.clock())
            except (TypeError, ValueError):
                return None
            if (value.get("event_fingerprint") != event_fingerprint or value.get("status") != "pending"
                    or not 0 < remaining <= BROWSER_WARMUP_SECONDS + 5 or not 0 <= attempts < 2):
                return None
            value["attempts"] = attempts + 1
            _atomic_write(self.queue_dir / "browser_warmup.json", json.dumps(value, separators=(",", ":")))
            return value

    def _delete_files_unlocked(self, request_id: str) -> None:
        for path in (
            self._request_path(self.queue_dir, request_id),
            self._token_path(self.queue_dir, request_id),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_unlocked(self, now: float | None = None) -> None:
        current = float(self.clock() if now is None else now)
        self._ensure_dir()
        for path in self.queue_dir.glob(f"{REQUEST_PREFIX}*{REQUEST_SUFFIX}"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    request = json.load(handle)
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                try:
                    if current - path.stat().st_mtime > STALE_RETENTION_SECONDS:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if not isinstance(request, dict):
                continue
            request_id = str(request.get("request_id") or "")
            if not REQUEST_ID_RE.fullmatch(request_id):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            expires = float(request.get("expires_epoch") or 0)
            updated = float(request.get("updated_epoch") or request.get("created_epoch") or 0)
            status = str(request.get("status") or "pending")
            if (
                status in {"pending", "submitted"}
                and expires > 0
                and current >= expires
            ):
                request["status"] = "expired"
                request["token_available"] = False
                request["updated_epoch"] = current
                request["updated_at"] = _time_text(current)
                self._write_request_unlocked(request)
                try:
                    self._token_path(self.queue_dir, request_id).unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if status in FINISHED_STATUSES and (
                current - max(updated, 0) > STALE_RETENTION_SECONDS
            ):
                self._delete_files_unlocked(request_id)
                continue
            # A token file without a live request must never accumulate.
        for path in self.queue_dir.glob(f"{TOKEN_PREFIX}*{TOKEN_SUFFIX}"):
            request_id = path.name[len(TOKEN_PREFIX) : -len(TOKEN_SUFFIX)]
            if not REQUEST_ID_RE.fullmatch(request_id):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if self._read_request_unlocked(request_id) is None:
                try:
                    if current - path.stat().st_mtime > STALE_RETENTION_SECONDS:
                        path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _public_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Return metadata only; never include the token or internal paths."""

        allowed = (
            "request_id",
            "event_fingerprint",
            "message_id",
            "account",
            "identity",
            "challenge_id",
            "origin",
            "created_at",
            "created_epoch",
            "expires_at",
            "expires_epoch",
            "status",
            "token_available",
            "submitted_at",
            "consumed_at",
            "browser_event",
            "browser_error_code",
            "browser_updated_at",
            "browser_source",
            "browser_attempts",
            "result_error",
            "result_http_status",
            "result_at",
            "cancel_reason",
        )
        return {key: request[key] for key in allowed if key in request}

    def record_browser_attempt(self, request_id: str, *, attempt: int, duration_ms: int,
                               budget_ms: int, error: str = "", cf_code: str = "", stage: str = "") -> None:
        """Retain bounded, credential-free attempt timing even after cancellation."""
        request_id = _safe_id(request_id)
        safe_error = str(error or "")
        if safe_error and safe_error not in BROWSER_FAILURE_CODES:
            safe_error = "browser_verification_failed"
        code = str(cf_code or "")
        if code and not re.fullmatch(r"[0-9]{3,6}", code):
            code = ""
        row = {"attempt": max(1, min(3, int(attempt))),
               "duration_ms": max(0, min(600000, int(duration_ms))),
               "budget_ms": max(0, min(90000, int(budget_ms))),
               "error": safe_error, "cf_code": code,
               "stage": stage if stage in BROWSER_EVENTS else ""}
        with self._thread_lock, _queue_lock(self.lock_path):
            request = self._read_request_unlocked(request_id)
            if request:
                request["browser_attempts"] = [*(request.get("browser_attempts") or [])[-2:], row]
                self._write_request_unlocked(request)

    def get_request(self, request_id: Any) -> dict[str, Any] | None:
        request_id = _safe_id(request_id)
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked()
            request = self._read_request_unlocked(request_id)
            return self._public_request(request) if request else None

    def record_browser_event(
        self, request_id: Any, event: Any, error_code: Any = "", *, source: str = "manual",
    ) -> dict[str, Any]:
        """Keep only enumerated stages and numeric CF codes, never arbitrary JS text."""
        request_id = _safe_id(request_id)
        event = str(event or "")
        code = str(error_code or "")
        if event not in BROWSER_EVENTS or source not in {"manual", "automatic"} or (code and not re.fullmatch(r"[0-9]{3,6}", code)):
            raise TurnstileRequestError("turnstile_browser_event_invalid")
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            request = self._read_request_unlocked(request_id)
            if not request:
                raise TurnstileRequestError("turnstile_request_not_found")
            # Late widget callbacks must not overwrite a worker's result.
            if request.get("status") == "pending":
                request.update({
                    "browser_event": event,
                    "browser_error_code": code,
                    "browser_updated_at": _time_text(now),
                    "browser_source": source,
                    "updated_epoch": now,
                })
                self._write_request_unlocked(request)
            return self._public_request(request)

    def record_result(
        self, request_id: Any, *, accepted: bool, error: str = "", http_status: int = 0,
    ) -> dict[str, Any] | None:
        """A consumed token is only accepted after the upstream /begin succeeds."""
        request_id = _safe_id(request_id)
        code = str(error or "")
        if code and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
            code = "request_failed"
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            request = self._read_request_unlocked(request_id)
            if not request:
                return None
            if request.get("status") == "consumed":
                request.update({
                    "status": "accepted" if accepted else "rejected",
                    "result_error": "" if accepted else code,
                    "result_http_status": max(0, min(599, int(http_status or 0))),
                    "result_at": _time_text(now),
                    "updated_epoch": now,
                    "token_available": False,
                })
                self._write_request_unlocked(request)
            return self._public_request(request)

    def create_request(
        self,
        *,
        event_fingerprint: str,
        message_id: int | None,
        account: str,
        identity: str,
        challenge_id: str,
        origin: str,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        now = float(self.clock())
        ttl = self.request_ttl_seconds if ttl_seconds is None else max(30, min(600, int(ttl_seconds)))
        request_id = secrets.token_urlsafe(24)
        # URL-safe IDs are normally 32 characters; keep the defensive check in
        # one place in case the generator is replaced in a test.
        _safe_id(request_id)
        request = {
            "version": 1,
            "request_id": request_id,
            "event_fingerprint": _safe_text(event_fingerprint, 64),
            "message_id": int(message_id or 0),
            "account": _safe_text(account, 32),
            "identity": _safe_text(identity, 80),
            "challenge_id": _safe_text(challenge_id, 160),
            "origin": _safe_text(origin, 200),
            "created_epoch": now,
            "created_at": _time_text(now),
            "expires_epoch": now + ttl,
            "expires_at": _time_text(now + ttl),
            "updated_epoch": now,
            "status": "pending",
            "token_available": False,
        }
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            self._ensure_dir()
            self._write_request_unlocked(request)
        return self._public_request(request)

    def list_requests(self, *, include_finished: bool = False) -> list[dict[str, Any]]:
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            self._ensure_dir()
            rows: list[dict[str, Any]] = []
            for path in self.queue_dir.glob(f"{REQUEST_PREFIX}*{REQUEST_SUFFIX}"):
                try:
                    request_id = path.name[len(REQUEST_PREFIX) : -len(REQUEST_SUFFIX)]
                    if not REQUEST_ID_RE.fullmatch(request_id):
                        continue
                    request = self._read_request_unlocked(request_id)
                except (OSError, TypeError, ValueError):
                    continue
                if not request:
                    continue
                status = str(request.get("status") or "pending")
                if not include_finished and status not in {"pending", "submitted"}:
                    continue
                rows.append(self._public_request(request))
            rows.sort(key=lambda row: str(row.get("created_at") or ""))
            return rows

    def submit_token(self, request_id: Any, token: Any) -> dict[str, Any]:
        request_id = _safe_id(request_id)
        value = str(token or "").strip()
        if not value or len(value) > MAX_TOKEN_LENGTH or any(ord(ch) < 32 for ch in value):
            raise TurnstileRequestError("turnstile_token_invalid")
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            request = self._read_request_unlocked(request_id)
            if not request:
                raise TurnstileRequestError("turnstile_request_not_found")
            expires = float(request.get("expires_epoch") or 0)
            if expires > 0 and now >= expires:
                raise TurnstileRequestError("turnstile_request_expired")
            if str(request.get("status") or "pending") not in {"pending"}:
                raise TurnstileRequestError("turnstile_request_already_submitted")
            token_path = self._token_path(self.queue_dir, request_id)
            if token_path.exists():
                raise TurnstileRequestError("turnstile_request_already_submitted")
            _atomic_write(token_path, value + "\n")
            request["status"] = "submitted"
            request["token_available"] = True
            request["submitted_epoch"] = now
            request["submitted_at"] = _time_text(now)
            request["updated_epoch"] = now
            self._write_request_unlocked(request)
            return self._public_request(request)

    def take_token(self, request_id: Any) -> str | None:
        request_id = _safe_id(request_id)
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            request = self._read_request_unlocked(request_id)
            if not request:
                return None
            expires = float(request.get("expires_epoch") or 0)
            if expires > 0 and now >= expires:
                return None
            if str(request.get("status") or "") != "submitted":
                return None
            token_path = self._token_path(self.queue_dir, request_id)
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError):
                return None
            try:
                token_path.unlink(missing_ok=True)
            except OSError:
                pass
            request["status"] = "consumed"
            request["token_available"] = False
            request["consumed_epoch"] = now
            request["consumed_at"] = _time_text(now)
            request["updated_epoch"] = now
            self._write_request_unlocked(request)
            return token or None

    def cancel(self, request_id: Any, *, reason: str = "cancelled") -> None:
        request_id = _safe_id(request_id)
        now = float(self.clock())
        with self._thread_lock, _queue_lock(self.lock_path):
            self._cleanup_unlocked(now)
            request = self._read_request_unlocked(request_id)
            if not request:
                return
            if request.get("status") in {"accepted", "rejected"}:
                return
            try:
                self._token_path(self.queue_dir, request_id).unlink(missing_ok=True)
            except OSError:
                pass
            request["status"] = "cancelled"
            request["token_available"] = False
            request["cancel_reason"] = _safe_text(reason, 80)
            request["updated_epoch"] = now
            request["updated_at"] = _time_text(now)
            self._write_request_unlocked(request)


_DEFAULT_BROKER: WorldBossTurnstileBroker | None = None
_DEFAULT_BROKER_LOCK = threading.Lock()


def default_world_boss_turnstile_broker() -> WorldBossTurnstileBroker:
    global _DEFAULT_BROKER
    if _DEFAULT_BROKER is None:
        with _DEFAULT_BROKER_LOCK:
            if _DEFAULT_BROKER is None:
                _DEFAULT_BROKER = WorldBossTurnstileBroker()
    return _DEFAULT_BROKER


def list_world_boss_turnstile_requests(*, include_finished: bool = False) -> list[dict[str, Any]]:
    return default_world_boss_turnstile_broker().list_requests(
        include_finished=include_finished
    )


def submit_world_boss_turnstile_token(request_id: Any, token: Any) -> dict[str, Any]:
    return default_world_boss_turnstile_broker().submit_token(request_id, token)


def record_world_boss_turnstile_browser_event(
    request_id: Any, event: Any, error_code: Any = "",
) -> dict[str, Any]:
    return default_world_boss_turnstile_broker().record_browser_event(request_id, event, error_code)
