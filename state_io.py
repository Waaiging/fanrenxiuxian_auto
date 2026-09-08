"""Crash-safe JSON persistence helpers for runtime state files.

The account workers and the Dashboard share the same JSON files.  A direct
``open(path, "w")`` leaves a window in which a snapshot (or another reader)
can observe a truncated document.  This module keeps the public surface small
and deliberately avoids any application-specific state schema:

* serialize before touching the destination;
* serialize writers with a short cross-process lock;
* write a same-directory temporary file, ``fsync`` it, then ``os.replace``;
* retain the last valid document as ``.bak``;
* recover a damaged/missing main file from that backup on load.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class StateFileLockTimeout(TimeoutError):
    """Raised when another process holds a state-file lock too long."""


def _thread_lock(path: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[path] = lock
        return lock


def _lock_path(path: str) -> str:
    return f"{os.path.abspath(os.fspath(path))}.lock"


def _try_windows_lock(handle) -> bool:
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock_windows(handle) -> None:
    import msvcrt

    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def json_file_lock(
    path: str,
    *,
    timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold an advisory lock associated with *path*.

    The lock is intentionally separate from the JSON document so replacing
    the document cannot invalidate a lock held by another process.  A
    re-entrant in-process lock prevents two threads in the Dashboard from
    racing between read and replace.
    """

    path = os.path.abspath(os.fspath(path))
    lock_path = _lock_path(path)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    thread_lock = _thread_lock(lock_path)
    acquired_thread = thread_lock.acquire(timeout=max(0.0, float(timeout)))
    if not acquired_thread:
        raise StateFileLockTimeout(f"state lock timeout: {path}")

    handle = None
    acquired_file = False
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        handle = open(lock_path, "a+b")
        while not acquired_file:
            if os.name == "nt":
                acquired_file = _try_windows_lock(handle)
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_file = True
                except BlockingIOError:
                    acquired_file = False
            if acquired_file:
                break
            if time.monotonic() >= deadline:
                raise StateFileLockTimeout(f"state lock timeout: {path}")
            time.sleep(0.02)
        yield
    finally:
        if handle is not None:
            if acquired_file:
                if os.name == "nt":
                    _unlock_windows(handle)
                else:
                    import fcntl

                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            handle.close()
        thread_lock.release()


def _logger_call(logger: Any, level: str, message: str, *args: Any) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        try:
            method(message, *args)
        except TypeError:
            # Tiny test loggers often only accept one already-formatted value.
            method(message % args if args else message)


def _read_json(path: str, *, expected_type: Any = None) -> tuple[Any, bytes | None, Exception | None]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        value = json.loads(raw.decode("utf-8"))
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"expected {expected_type!r}, got {type(value).__name__}"
            )
        return value, raw, None
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return None, None, exc


def _fsync_directory(directory: str) -> None:
    if os.name == "nt":
        return
    try:
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _destination_mode(path: str) -> int:
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return 0o600


def _atomic_replace_bytes(path: str, payload: bytes, *, mode: int | None = None) -> None:
    path = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(path) or os.curdir
    os.makedirs(directory, exist_ok=True)
    fd = -1
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            dir=directory,
        )
        if mode is not None:
            try:
                os.fchmod(fd, mode)
            except (AttributeError, OSError):
                pass
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = ""
        _fsync_directory(directory)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def save_json_state(
    path: str,
    data: Any,
    *,
    backup: bool = True,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    logger: Any = None,
) -> None:
    """Persist JSON data without exposing a partially written destination."""

    # Do this before acquiring the lock or creating a temp file.  A failed
    # serialization must never alter either the main file or its backup.
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path = os.path.abspath(os.fspath(path))
    backup_path = f"{path}.bak"

    with json_file_lock(path, timeout=lock_timeout):
        if backup and os.path.isfile(path):
            current_value, current_raw, current_error = _read_json(path)
            if current_error is None and current_raw is not None:
                try:
                    _atomic_replace_bytes(
                        backup_path,
                        current_raw,
                        mode=_destination_mode(backup_path),
                    )
                except OSError as exc:
                    # A backup is best effort; the new main document can still
                    # be written safely.  Never copy a known-corrupt document.
                    _logger_call(logger, "warning", "state backup failed for %s: %s", path, exc)
        _atomic_replace_bytes(path, encoded, mode=_destination_mode(path))


def update_json_state(
    path: str,
    update: Callable[[Any], Any],
    *,
    expected_type: Any = dict,
    default: Any = None,
    backup: bool = True,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    logger: Any = None,
) -> Any:
    """Atomically run one read/modify/write transaction for a JSON state file."""

    path = os.path.abspath(os.fspath(path))
    backup_path = f"{path}.bak"
    with json_file_lock(path, timeout=lock_timeout):
        current, current_raw, error = _read_json(path, expected_type=expected_type)
        if error is not None and backup:
            current, current_raw, backup_error = _read_json(
                backup_path,
                expected_type=expected_type,
            )
            if backup_error is None:
                _logger_call(
                    logger,
                    "warning",
                    "state transaction for %s recovered from last valid backup",
                    path,
                )
            else:
                current = copy.deepcopy(default)
                current_raw = None
        elif error is not None:
            current = copy.deepcopy(default)
            current_raw = None

        result = update(current)
        if result is None:
            result = current
        if expected_type is not None and not isinstance(result, expected_type):
            raise TypeError(
                f"expected updated {expected_type!r}, got {type(result).__name__}"
            )
        encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        # A read/check transaction must not rotate a useful backup or fsync an
        # unchanged document. Recovery still writes when the main file failed.
        if error is None and encoded == current_raw:
            return result

        if backup and current_raw is not None:
            try:
                _atomic_replace_bytes(
                    backup_path,
                    current_raw,
                    mode=_destination_mode(backup_path),
                )
            except OSError as exc:
                _logger_call(logger, "warning", "state backup failed for %s: %s", path, exc)
        _atomic_replace_bytes(path, encoded, mode=_destination_mode(path))
        return result


def load_json_state(
    path: str,
    *,
    expected_type: Any = dict,
    backup: bool = True,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    logger: Any = None,
    default: Any = None,
) -> Any:
    """Load a JSON document and recover the last valid ``.bak`` if needed."""

    path = os.path.abspath(os.fspath(path))
    backup_path = f"{path}.bak"
    try:
        with json_file_lock(path, timeout=lock_timeout):
            value, _, error = _read_json(path, expected_type=expected_type)
            if error is None:
                return value

            if backup:
                recovered, recovered_raw, backup_error = _read_json(
                    backup_path,
                    expected_type=expected_type,
                )
                if backup_error is None and recovered_raw is not None:
                    try:
                        _atomic_replace_bytes(
                            path,
                            recovered_raw,
                            mode=_destination_mode(path),
                        )
                        _logger_call(
                            logger,
                            "warning",
                            "state file %s was invalid; restored last valid backup",
                            path,
                        )
                        return recovered
                    except OSError as exc:
                        _logger_call(
                            logger,
                            "error",
                            "state recovery failed for %s: %s",
                            path,
                            exc,
                        )
                elif os.path.exists(backup_path):
                    _logger_call(
                        logger,
                        "error",
                        "state file %s and backup are invalid (%s / %s)",
                        path,
                        error,
                        backup_error,
                    )
            if os.path.exists(path):
                _logger_call(logger, "error", "load state JSON failed for %s: %s", path, error)
    except StateFileLockTimeout as exc:
        _logger_call(logger, "error", "%s", exc)
    except OSError as exc:
        _logger_call(logger, "error", "state file access failed for %s: %s", path, exc)

    return copy.deepcopy(default)


__all__ = [
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "StateFileLockTimeout",
    "json_file_lock",
    "load_json_state",
    "save_json_state",
    "update_json_state",
]
