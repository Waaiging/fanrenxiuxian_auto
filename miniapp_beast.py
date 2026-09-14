#!/usr/bin/env python3
"""Read Wanling spirit-beast state from the Telegram Mini App."""

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Callable

DEFAULT_BOT_USERNAME = "fanrenxiuxian_bot"
DEFAULT_REFRESH_SECONDS = 30 * 60
DEFAULT_RETRY_SECONDS = 5 * 60
REFRESH_REQUEST_FILE = "miniapp_beast_refresh_request.json"
SESSION_CACHE_FILE = "miniapp_beast_session.json"
TRANSPORT_HEALTH_FILE = "miniapp_transport_health.json"
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 3
DEFAULT_CIRCUIT_BACKOFF_SECONDS = (
    15 * 60,
    30 * 60,
    60 * 60,
    3 * 60 * 60,
    6 * 60 * 60,
)
DEFAULT_CIRCUIT_PROBE_LEASE_SECONDS = 90


_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_CIRCUIT_LOG = logging.getLogger("miniapp_transport")
_CIRCUIT_THREAD_LOCK = threading.RLock()


class MiniAppBeastError(RuntimeError):
    """A sanitized Mini App transport or API failure."""

    def __init__(self, code, status=0):
        self.code = str(code or "miniapp_request_failed")
        self.status = int(status or 0)
        super().__init__(self.code)


class MiniAppReadDeadlineError(MiniAppBeastError):
    """A read-only caller exhausted a budget shorter than the normal timeout."""


class MiniAppCircuitOpenError(MiniAppBeastError):
    """The shared upstream circuit is open, so no HTTP request was attempted."""

    def __init__(self, retry_after: float = 0, retry_at: str = ""):
        super().__init__("miniapp_circuit_open")
        self.retry_after = max(0, int(round(float(retry_after or 0))))
        self.retry_at = str(retry_at or "")


@dataclass(frozen=True)
class MiniAppCircuitPermit:
    probe_token: str = ""

    @property
    def is_probe(self) -> bool:
        return bool(self.probe_token)


@dataclass(frozen=True)
class MiniAppCircuitDecision:
    permit: MiniAppCircuitPermit | None = None
    wait_seconds: float = 0
    retry_at: str = ""
    half_open: bool = False


@contextmanager
def _exclusive_transport_health_lock(path: str):
    """Serialize shared circuit state across the four VPS processes."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _CIRCUIT_THREAD_LOCK:
        handle = open(path, "a+b")
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class MiniAppTransportCircuitBreaker:
    """Cross-process circuit breaker for the shared Mini App HTTP upstream."""

    def __init__(
        self,
        state_path: str | None = None,
        *,
        failure_threshold: int = DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        backoff_seconds: tuple[int, ...] = DEFAULT_CIRCUIT_BACKOFF_SECONDS,
        probe_lease_seconds: int = DEFAULT_CIRCUIT_PROBE_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
        logger: Any = None,
    ) -> None:
        self.state_path = os.path.abspath(
            state_path or os.path.join(_MODULE_DIR, TRANSPORT_HEALTH_FILE)
        )
        self.lock_path = f"{self.state_path}.lock"
        self.failure_threshold = max(1, int(failure_threshold or 1))
        durations = tuple(max(1, int(value)) for value in backoff_seconds)
        self.backoff_seconds = durations or DEFAULT_CIRCUIT_BACKOFF_SECONDS
        self.probe_lease_seconds = max(5, int(probe_lease_seconds or 5))
        self.clock = clock
        self.log = logger or _CIRCUIT_LOG

    @staticmethod
    def _origin_key(origin: str) -> str:
        parsed = urllib.parse.urlsplit(str(origin or "").strip())
        if parsed.scheme and parsed.netloc:
            return urllib.parse.urlunsplit(
                (parsed.scheme.lower(), parsed.netloc.lower(), "", "", "")
            ).rstrip("/")
        return str(origin or "").strip().rstrip("/") or "unknown"

    @staticmethod
    def _time_text(epoch: float) -> str:
        if epoch <= 0:
            return ""
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _empty_document() -> dict[str, Any]:
        return {"version": 1, "origins": {}}

    @staticmethod
    def _empty_entry() -> dict[str, Any]:
        return {
            "status": "closed",
            "consecutive_failures": 0,
            "backoff_level": 0,
            "generation": 0,
            "outage_started_epoch": 0,
            "opened_at_epoch": 0,
            "next_probe_epoch": 0,
            "probe_owner": "",
            "probe_until_epoch": 0,
            "last_error": "",
            "last_http_status": 0,
            "updated_at": "",
        }

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError, TypeError):
            return self._empty_document()
        if not isinstance(document, dict):
            return self._empty_document()
        origins = document.get("origins")
        if not isinstance(origins, dict):
            origins = {}
        document["version"] = 1
        document["origins"] = origins
        return document

    def _write_unlocked(self, document: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        temp_path = f"{self.state_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    def _load_entry(self, document: dict[str, Any], origin: str) -> tuple[str, dict[str, Any]]:
        key = self._origin_key(origin)
        stored = document["origins"].get(key)
        entry = self._empty_entry()
        if isinstance(stored, dict):
            entry.update(stored)
        return key, entry

    def acquire(self, origin: str) -> MiniAppCircuitDecision:
        now = float(self.clock())
        with _exclusive_transport_health_lock(self.lock_path):
            document = self._read_unlocked()
            key, entry = self._load_entry(document, origin)
            status = str(entry.get("status") or "closed")
            if status == "closed":
                return MiniAppCircuitDecision(permit=MiniAppCircuitPermit())

            next_probe = max(0.0, float(entry.get("next_probe_epoch") or 0))
            if now < next_probe:
                return MiniAppCircuitDecision(
                    wait_seconds=max(0.1, next_probe - now),
                    retry_at=self._time_text(next_probe),
                    half_open=False,
                )

            probe_until = max(0.0, float(entry.get("probe_until_epoch") or 0))
            if status == "half_open" and probe_until > now:
                return MiniAppCircuitDecision(
                    wait_seconds=max(0.1, probe_until - now),
                    retry_at=self._time_text(probe_until),
                    half_open=True,
                )

            token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
            entry.update({
                "status": "half_open",
                "probe_owner": token,
                "probe_until_epoch": now + self.probe_lease_seconds,
                "updated_at": self._time_text(now),
            })
            document["origins"][key] = entry
            self._write_unlocked(document)
            return MiniAppCircuitDecision(
                permit=MiniAppCircuitPermit(probe_token=token),
            )

    def record_success(self, origin: str, permit: MiniAppCircuitPermit) -> None:
        now = float(self.clock())
        recovery: tuple[int, float] | None = None
        with _exclusive_transport_health_lock(self.lock_path):
            document = self._read_unlocked()
            key, entry = self._load_entry(document, origin)
            status = str(entry.get("status") or "closed")
            if status == "closed":
                if int(entry.get("consecutive_failures") or 0) <= 0 and not entry.get("last_error"):
                    return
                entry.update({
                    "consecutive_failures": 0,
                    "last_error": "",
                    "last_http_status": 0,
                    "updated_at": self._time_text(now),
                })
            elif permit.is_probe and str(entry.get("probe_owner") or "") == permit.probe_token:
                level = max(1, int(entry.get("backoff_level") or 1))
                outage_started = max(0.0, float(entry.get("outage_started_epoch") or now))
                recovery = (level, max(0.0, now - outage_started))
                entry = self._empty_entry()
                entry["generation"] = int(
                    document["origins"].get(key, {}).get("generation") or 0
                )
                entry["updated_at"] = self._time_text(now)
            else:
                return
            document["origins"][key] = entry
            self._write_unlocked(document)
        if recovery is not None:
            level, outage_seconds = recovery
            self.log.warning(
                "Mini App upstream circuit recovered after a successful probe "
                "(level=%s, outage=%ss); HTTP requests resumed",
                level,
                int(round(outage_seconds)),
            )

    def force_recover(self, origin: str) -> None:
        """Recover after authoritative evidence outside an elected probe."""
        now = float(self.clock())
        with _exclusive_transport_health_lock(self.lock_path):
            document = self._read_unlocked()
            key, entry = self._load_entry(document, origin)
            # A normal successful request (or another worker's recovery) may
            # already have closed the circuit.  Avoid an unnecessary fsync and
            # duplicate warning for every retry of the same /start call.
            if (
                str(entry.get("status") or "closed") == "closed"
                and int(entry.get("consecutive_failures") or 0) <= 0
                and not entry.get("last_error")
            ):
                return
            generation = int(entry.get("generation") or 0)
            entry = self._empty_entry()
            entry["generation"] = generation
            entry["updated_at"] = self._time_text(now)
            document["origins"][key] = entry
            self._write_unlocked(document)
        _CIRCUIT_LOG.warning(
            "Mini App upstream circuit recovered by a time-critical request; "
            "HTTP requests resumed"
        )

    def record_failure(
        self,
        origin: str,
        permit: MiniAppCircuitPermit,
        error_code: str,
        http_status: int = 0,
    ) -> None:
        now = float(self.clock())
        transition: tuple[str, int, int, str, str] | None = None
        with _exclusive_transport_health_lock(self.lock_path):
            document = self._read_unlocked()
            key, entry = self._load_entry(document, origin)
            status = str(entry.get("status") or "closed")
            code = str(error_code or "miniapp_request_failed")
            http_status = int(http_status or 0)

            if permit.is_probe:
                if status != "half_open" or str(entry.get("probe_owner") or "") != permit.probe_token:
                    return
                level = min(
                    len(self.backoff_seconds),
                    max(1, int(entry.get("backoff_level") or 1)) + 1,
                )
                duration = self.backoff_seconds[level - 1]
                next_probe = now + duration
                entry.update({
                    "status": "open",
                    "consecutive_failures": self.failure_threshold,
                    "backoff_level": level,
                    "generation": int(entry.get("generation") or 0) + 1,
                    "opened_at_epoch": now,
                    "next_probe_epoch": next_probe,
                    "probe_owner": "",
                    "probe_until_epoch": 0,
                    "last_error": code,
                    "last_http_status": http_status,
                    "updated_at": self._time_text(now),
                })
                transition = (
                    "extended",
                    level,
                    duration,
                    code,
                    self._time_text(next_probe),
                )
            elif status == "closed":
                failures = int(entry.get("consecutive_failures") or 0) + 1
                entry.update({
                    "consecutive_failures": failures,
                    "last_error": code,
                    "last_http_status": http_status,
                    "updated_at": self._time_text(now),
                })
                if failures >= self.failure_threshold:
                    level = 1
                    duration = self.backoff_seconds[0]
                    next_probe = now + duration
                    entry.update({
                        "status": "open",
                        "backoff_level": level,
                        "generation": int(entry.get("generation") or 0) + 1,
                        "outage_started_epoch": now,
                        "opened_at_epoch": now,
                        "next_probe_epoch": next_probe,
                        "probe_owner": "",
                        "probe_until_epoch": 0,
                    })
                    transition = (
                        "opened",
                        level,
                        duration,
                        code,
                        self._time_text(next_probe),
                    )
            else:
                # Ignore requests that were already in flight when another process
                # opened the circuit. Only the elected half-open probe may extend it.
                return

            document["origins"][key] = entry
            self._write_unlocked(document)

        if transition is None:
            return
        action, level, duration, code, retry_at = transition
        if action == "opened":
            self.log.error(
                "Mini App upstream circuit opened after %s consecutive failures (%s); "
                "HTTP requests paused for %s minutes until %s",
                self.failure_threshold,
                code,
                max(1, duration // 60),
                retry_at,
            )
        else:
            self.log.error(
                "Mini App upstream recovery probe failed (%s); circuit extended "
                "to level %s for %s minutes until %s",
                code,
                level,
                max(1, duration // 60),
                retry_at,
            )

    def snapshot(self, origin: str) -> dict[str, Any]:
        with _exclusive_transport_health_lock(self.lock_path):
            document = self._read_unlocked()
            _, entry = self._load_entry(document, origin)
            return dict(entry)


def miniapp_upstream_failure(exc: BaseException) -> bool:
    """Return whether an error indicates the shared HTTP service is unhealthy."""
    code = (
        exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__
    )
    code = str(code or "").strip().lower()
    status = int(getattr(exc, "status", 0) or 0)
    if status >= 500:
        return True
    if code in {
        "invalid_json",
        "invalid_response",
        "timeouterror",
        "timeout",
        "urlerror",
        "request_failed",
        "api_timeout",
        "api_unreachable",
        "bad_response",
        "server_busy",
        "server_error",
        "remotedisconnected",
        "incompleteread",
        "connectionerror",
        "connectionrefusederror",
        "connectionreseterror",
        "brokenpipeerror",
        "sslerror",
        "gaierror",
    }:
        return True
    if code.startswith("http_"):
        try:
            return int(code.partition("_")[2]) >= 500
        except ValueError:
            return False
    return any(
        marker in code
        for marker in ("timeout", "connection", "unreachable", "disconnected")
    )


def is_miniapp_circuit_open(exc: BaseException) -> bool:
    return isinstance(exc, MiniAppCircuitOpenError)


def miniapp_circuit_wait_seconds(exc: BaseException, minimum: int = 60) -> int:
    if not is_miniapp_circuit_open(exc):
        return max(0, int(minimum or 0))
    return max(int(minimum or 0), int(getattr(exc, "retry_after", 0) or 0))


_MINIAPP_CIRCUIT = MiniAppTransportCircuitBreaker()


def miniapp_circuit_preflight(origin: Any) -> MiniAppCircuitOpenError | None:
    """Return a fast, non-network failure while the shared circuit is cooling."""
    origin = str(origin or "").strip()
    if not origin:
        # Optional and injected transports may not expose a production origin.
        # They must not inherit the shared production circuit state.
        return None
    state = _MINIAPP_CIRCUIT.snapshot(origin)
    status = str(state.get("status") or "closed")
    now = float(_MINIAPP_CIRCUIT.clock())
    if status == "open":
        next_probe = max(0.0, float(state.get("next_probe_epoch") or 0))
        if next_probe > now:
            return MiniAppCircuitOpenError(next_probe - now, MiniAppTransportCircuitBreaker._time_text(next_probe))
    elif status == "half_open":
        probe_until = max(0.0, float(state.get("probe_until_epoch") or 0))
        if probe_until > now:
            return MiniAppCircuitOpenError(probe_until - now, MiniAppTransportCircuitBreaker._time_text(probe_until))
    return None


def miniapp_entry_start_param(entry_url):
    """Extract the fixed ``startapp`` token without logging the token itself."""
    parsed = urllib.parse.urlsplit(str(entry_url or "").strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not parsed.scheme or not parsed.netloc or not token:
        raise MiniAppBeastError("invalid_entry_url")
    return token


def miniapp_origin(entry_url):
    parsed = urllib.parse.urlsplit(str(entry_url or "").strip())
    if parsed.netloc.lower() == "t.me":
        return "https://asc.aiopenai.app"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MiniAppBeastError("invalid_entry_url")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def extract_webview_init_data(webview_url):
    fragment = urllib.parse.parse_qs(urllib.parse.urlsplit(str(webview_url or "")).fragment)
    init_data = str((fragment.get("tgWebAppData") or [""])[0]).strip()
    if not init_data:
        raise MiniAppBeastError("init_data_missing")
    return init_data


def extract_spirit_token(value):
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    query = urllib.parse.parse_qs(parsed.query)
    token = str((query.get("startapp") or query.get("start_param") or [""])[0]).strip()
    if not token.lower().startswith("spiritbeast_"):
        raise MiniAppBeastError("spirit_beast_token_missing")
    return token


def normalize_spirit_beast_roster(payload):
    """Map Mini App ``beasts`` records to the existing automation cache schema."""
    rows = payload.get("beasts") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise MiniAppBeastError("beast_roster_missing")
    beasts = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        beast_type = str(item.get("beastType") or "").strip()
        try:
            tier = max(0, int(item.get("tier") or 0))
            stamina = max(0, int(item.get("stamina") or 0))
            power = max(0, int(item.get("combatPower") or 0))
            experience = max(0, int(item.get("experience") or 0))
            beast_id = int(item.get("id") or 0)
            level = max(0, int(item.get("level") or 0))
        except (TypeError, ValueError):
            continue
        if not name or not beast_type or tier <= 0:
            continue
        beasts.append({
            "id": beast_id,
            "full_name": name,
            "status": str(item.get("status") or "未知").strip() or "未知",
            "species": f"{tier}阶{beast_type}",
            "beast_type": beast_type,
            "tier": tier,
            "level": level,
            "power": power,
            "stamina": stamina,
            "exp": experience,
            "is_active": bool(item.get("isActive")),
            "can_expedition": bool(item.get("canExpedition")),
            "can_explore_abyss": bool(item.get("canExploreAbyss")),
        })
    if not beasts:
        raise MiniAppBeastError("beast_roster_empty")
    beasts.sort(
        key=lambda item: (item["power"], item["stamina"], item["full_name"]),
        reverse=True,
    )
    return beasts


def miniapp_error_details(value, depth=0):
    """Keep bounded API diagnostics without retaining credentials."""
    if depth >= 5:
        return None
    if isinstance(value, dict):
        return {
            str(key)[:80]: miniapp_error_details(item, depth + 1)
            for key, item in list(value.items())[:40]
            if not any(part in re.sub(r"[^a-z0-9]", "", str(key).lower())
                       for part in ("token", "ticket", "initdata", "authorization", "cookie", "secret", "signature"))
        }
    if isinstance(value, (list, tuple)):
        return [miniapp_error_details(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:240]
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _post_json_sync(origin, path, payload, timeout):
    request = urllib.request.Request(
        urllib.parse.urljoin(origin.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": origin.rstrip("/") + "/miniapp/xianxia-dwelling",
            "User-Agent": "Mozilla/5.0 Telegram-Android/11.0",
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(5, int(timeout or 20))) as response:
            status = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
        try:
            body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
    except Exception as exc:
        raise MiniAppBeastError(type(exc).__name__.lower()) from exc
    try:
        data = json.loads(body)
    except Exception as exc:
        raise MiniAppBeastError("invalid_json", status) from exc
    if not isinstance(data, dict):
        raise MiniAppBeastError("invalid_response", status)
    if status >= 400 or data.get("ok") is False:
        error = MiniAppBeastError(data.get("error") or f"http_{status}", status)
        error.details = miniapp_error_details(data)
        raise error
    return data


async def _run_blocking(func, *args, executor=None):
    """Run one blocking transport operation without monopolizing the
    event loop's shared default executor.

    Callers normally use ``asyncio.to_thread``.  Deadline-sensitive monitors may
    supply their own executor so unrelated background jobs cannot delay a
    request at a battle window boundary.
    """
    if executor is None:
        return await asyncio.to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func, *args)


async def _post_json(
    origin,
    path,
    payload,
    timeout,
    post_json=None,
    *,
    time_critical: bool = False,
    executor=None,
    request_sync=None,
    network_trace=None,
):
    if post_json is not None:
        # Injected transports are used by focused tests and do not represent the
        # shared production HTTP service.
        result = post_json(origin, path, payload, timeout)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise MiniAppBeastError("invalid_response")
        if result.get("ok") is False:
            raise MiniAppBeastError(result.get("error") or "miniapp_request_failed")
        return result

    # A manifestation boundary is a rare, user-visible deadline. Try one direct
    # request even while the shared breaker is cooling; failures still update
    # the shared health state. Ordinary background traffic keeps waiting.
    decision = (
        await _run_blocking(_MINIAPP_CIRCUIT.acquire, origin, executor=executor)
        if not time_critical
        else MiniAppCircuitDecision(permit=MiniAppCircuitPermit())
    )
    if decision.permit is None:
        # Never hold a transport lock or sleep inside a worker while the service
        # is down. The caller records a paused state and schedules the next check.
        raise MiniAppCircuitOpenError(decision.wait_seconds, decision.retry_at)
    permit = decision.permit

    def request():
        if network_trace is not None:
            network_trace["sent_monotonic"] = time.monotonic()
        try:
            return (request_sync or _post_json_sync)(origin, path, payload, timeout)
        finally:
            if network_trace is not None:
                network_trace["received_monotonic"] = time.monotonic()

    try:
        # World-boss windows are deadline-driven.  Keep their blocking HTTP calls
        # out of asyncio's shared default pool, which is also used by background
        # Mini App refreshes and can otherwise delay a /hit.
        result = await _run_blocking(
            request, executor=executor
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, MiniAppReadDeadlineError) and not permit.is_probe:
            # A short UI deadline says nothing about shared service health.
            # Neither open the circuit nor clear another request's failures.
            raise
        if miniapp_upstream_failure(exc) or isinstance(exc, MiniAppReadDeadlineError):
            # An elected half-open probe still needs a real response to recover.
            await _run_blocking(
                _MINIAPP_CIRCUIT.record_failure,
                origin,
                permit,
                exc.code if isinstance(exc, MiniAppBeastError) else type(exc).__name__.lower(),
                int(getattr(exc, "status", 0) or 0),
                executor=executor,
            )
            circuit_error = await _run_blocking(miniapp_circuit_preflight, origin, executor=executor)
            if circuit_error is not None:
                # The circuit transition already emitted the useful outage log.
                # Pause callers instead of logging or retrying the same failure
                # as an independent task error.
                raise circuit_error from exc
        else:
            # A structured application/authentication error proves the HTTP
            # service answered; a half-open transport probe therefore succeeded.
            await _run_blocking(_MINIAPP_CIRCUIT.record_success, origin, permit, executor=executor)
        raise
    await _run_blocking(_MINIAPP_CIRCUIT.record_success, origin, permit, executor=executor)
    if time_critical:
        # A successful deadline request is authoritative evidence that the
        # upstream has recovered, even though it did not hold the elected
        # half-open probe lease.
        await _run_blocking(_MINIAPP_CIRCUIT.force_recover, origin, executor=executor)
    return result


async def request_webview_init_data(client, bot_username, start_param):
    # Dashboard imports the file/state helpers without opening a Telegram client.
    from telethon import types, utils
    from telethon.tl.functions.messages import RequestMainWebViewRequest

    bot_entity = await client.get_entity(bot_username)
    result = await client(RequestMainWebViewRequest(
        peer=await client.get_input_entity(bot_entity),
        bot=utils.get_input_user(bot_entity),
        platform="android",
        start_param=start_param,
        theme_params=types.DataJSON(data="{}"),
    ))
    return extract_webview_init_data(getattr(result, "url", ""))


async def fetch_miniapp_beast_snapshot(
    client,
    entry_url,
    cached_spirit_token="",
    bot_username=DEFAULT_BOT_USERNAME,
    timeout=20,
    post_json=None,
):
    """Fetch one authoritative roster without sending any Telegram chat command."""
    entry_token = miniapp_entry_start_param(entry_url)
    origin = miniapp_origin(entry_url)
    if post_json is None:
        circuit_error = miniapp_circuit_preflight(origin)
        if circuit_error is not None:
            raise circuit_error
    init_data = await request_webview_init_data(client, bot_username, entry_token)

    spirit_token = str(cached_spirit_token or "").strip()
    if spirit_token:
        try:
            roster = await _post_json(
                origin,
                "/api/miniapp/xianxia-spirit-beast/start",
                {"token": spirit_token, "initData": init_data},
                timeout,
                post_json=post_json,
            )
            return {
                "beasts": normalize_spirit_beast_roster(roster),
                "player": roster.get("player") or {},
                "spirit_token": spirit_token,
            }
        except MiniAppCircuitOpenError:
            raise
        except MiniAppBeastError as exc:
            if miniapp_upstream_failure(exc):
                # A stale cached token cannot be repaired while the shared
                # upstream is unhealthy. Do not spend another request on the
                # external-token exchange in the same cycle.
                raise
            spirit_token = ""

    await _post_json(
        origin,
        "/api/miniapp/xianxia-dwelling/start",
        {"token": entry_token, "initData": init_data},
        timeout,
        post_json=post_json,
    )
    external = await _post_json(
        origin,
        "/api/miniapp/xianxia-dwelling/external",
        {"token": entry_token, "initData": init_data, "action": "spirit_beast"},
        timeout,
        post_json=post_json,
    )
    spirit_token = extract_spirit_token(external.get("url"))
    roster = await _post_json(
        origin,
        "/api/miniapp/xianxia-spirit-beast/start",
        {"token": spirit_token, "initData": init_data},
        timeout,
        post_json=post_json,
    )
    return {
        "beasts": normalize_spirit_beast_roster(roster),
        "player": roster.get("player") or {},
        "spirit_token": spirit_token,
    }


def refresh_request_path(config_dir):
    return os.path.join(os.path.abspath(config_dir), REFRESH_REQUEST_FILE)


def session_cache_path(config_dir):
    return os.path.join(os.path.abspath(config_dir), SESSION_CACHE_FILE)


def entry_fingerprint(entry_url):
    return hashlib.sha256(str(entry_url or "").strip().encode("utf-8")).hexdigest()


def read_cached_spirit_token(config_dir, entry_url):
    try:
        with open(session_cache_path(config_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("entry_fingerprint") != entry_fingerprint(entry_url):
            return ""
        token = str(data.get("spirit_token") or "").strip()
        return token if token.lower().startswith("spiritbeast_") else ""
    except Exception:
        return ""


def write_cached_spirit_token(config_dir, entry_url, spirit_token):
    token = str(spirit_token or "").strip()
    if not token.lower().startswith("spiritbeast_"):
        raise MiniAppBeastError("spirit_beast_token_missing")
    path = session_cache_path(config_dir)
    data = {
        "entry_fingerprint": entry_fingerprint(entry_url),
        "spirit_token": token,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_refresh_request(config_dir):
    try:
        with open(refresh_request_path(config_dir), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_refresh_request(config_dir, requested_by="dashboard"):
    path = refresh_request_path(config_dir)
    data = {
        "request_id": uuid.uuid4().hex,
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "requested_by": str(requested_by or "dashboard")[:80],
    }
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data
