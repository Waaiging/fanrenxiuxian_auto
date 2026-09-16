"""Serial, unattended Turnstile verification through a dedicated native browser.

The browser loads the real Mini App and uses its current Turnstile configuration.
Only Cloudflare's callback can produce a token. No Telegram credentials are sent
to the browser, and tokens never appear in diagnostics. The worker remains the
only process that sends /begin.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import uuid

from world_boss_turnstile import (
    WorldBossTurnstileBroker, TurnstileRequestError, MAX_TOKEN_LENGTH, BROWSER_ORIGIN,
    BROWSER_ACTIVITIES,
)


ORIGIN = BROWSER_ORIGIN
LOG = logging.getLogger("world_boss_browser")
REUSABLE_BROWSER_ERRORS = frozenset({
    "browser_protocol_timeout", "turnstile_script_unavailable", "verification_request_finished",
})


class BrowserVerificationError(RuntimeError):
    def __init__(self, code: str, cf_code: str = ""):
        self.code = code
        self.cf_code = cf_code if re.fullmatch(r"[0-9]{3,6}", str(cf_code)) else ""
        super().__init__(code)


def _snap_launcher(candidate: str) -> bool:
    if str(candidate).replace("\\", "/").startswith("/snap/"):
        return True
    path = Path(candidate)
    try:
        if path.resolve().name == "snap":
            return True
        if path.stat().st_size <= 16384:
            return b"/snap/bin/chromium" in path.read_bytes()
    except OSError:
        pass
    return False


def find_chrome(explicit: str | None = None) -> str:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        raise BrowserVerificationError("browser_executable_missing")
    snap_candidates = []
    for name in ("google-chrome", "chromium", "chromium-browser"):
        candidate = shutil.which(name)
        if candidate:
            if _snap_launcher(candidate):
                snap_candidates.append(candidate)
            else:
                return candidate
    candidates = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")]
    candidates += sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/*/chrome"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    # Snap startup repeatedly exceeded 25 s on the small VPS. Prefer an
    # already-installed native binary; keep Snap usable when it is the only one.
    if snap_candidates:
        return snap_candidates[0]
    raise BrowserVerificationError("browser_executable_missing")


class NativeTurnstileBrowser:
    def __init__(self, profile_dir, *, chrome=None, no_sandbox=False):
        self.profile_dir = Path(profile_dir).resolve()
        self.chrome = chrome
        self.no_sandbox = no_sandbox
        self.process = None
        self.connection = None
        self.sequence = 0
        self.origin = ""
        self.activity = ""

    @staticmethod
    def _remaining(deadline, still_pending=None, code="turnstile_browser_timeout"):
        if still_pending and not still_pending():
            raise BrowserVerificationError("verification_request_finished")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserVerificationError(code)
        return remaining

    def start(self, *, timeout=25, still_pending=None):
        deadline = time.monotonic() + max(0, min(45, float(timeout)))
        self._remaining(deadline, still_pending, "browser_connection_timeout")
        if self.connection is not None:
            return
        try:
            import websocket
        except ImportError as exc:
            raise BrowserVerificationError("websocket_client_missing") from exc
        executable = find_chrome(self.chrome)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.profile_dir, 0o700)
        except OSError:
            pass
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        args = [executable, f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile_dir}", "--no-first-run",
                "--no-default-browser-check", "--disable-dev-shm-usage",
                "--disable-background-networking", "--disable-component-update",
                "--disable-default-apps", "--disable-sync",
                "--window-size=1100,950", "about:blank"]
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            args.append("--window-position=-32000,-32000")
        else:
            # The VPS has a software display and no GPU. Match the verified
            # native-browser launch without starting a GPU process.
            args.append("--disable-gpu")
            if self.no_sandbox:
                args.append("--no-sandbox")
            if not os.environ.get("DISPLAY"):
                xvfb = shutil.which("xvfb-run")
                if not xvfb:
                    raise BrowserVerificationError("virtual_display_missing")
                args = [xvfb, "-a", "-s", "-screen 0 1280x1024x24", *args]
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=startup, start_new_session=os.name != "nt",
        )
        try:
            while True:
                remaining = self._remaining(deadline, still_pending, "browser_connection_timeout")
                if self.process.poll() is not None:
                    raise BrowserVerificationError("browser_launch_failed")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=min(1, remaining)) as response:
                        pages = json.load(response)
                    page = next(item for item in pages if item.get("type") == "page")
                    remaining = self._remaining(deadline, still_pending, "browser_connection_timeout")
                    self.connection = websocket.create_connection(
                        page["webSocketDebuggerUrl"], timeout=min(1, remaining), suppress_origin=True,
                    )
                    self.command("Page.enable", deadline=deadline, still_pending=still_pending)
                    return
                except BrowserVerificationError:
                    raise
                except Exception:
                    time.sleep(min(0.25, self._remaining(deadline, still_pending, "browser_connection_timeout")))
        except BrowserVerificationError as exc:
            if exc.code not in REUSABLE_BROWSER_ERRORS or self.connection is None:
                self.close()
            raise
        except BaseException:
            self.close()
            raise

    def command(self, method, params=None, *, deadline=None, still_pending=None):
        from websocket import WebSocketTimeoutException

        deadline = min(deadline, time.monotonic() + 10) if deadline is not None else time.monotonic() + 10
        self.sequence += 1
        sequence = self.sequence
        try:
            remaining = self._remaining(deadline, still_pending, "browser_protocol_timeout")
            self.connection.settimeout(min(1, remaining))
            self.connection.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            while True:
                remaining = self._remaining(deadline, still_pending, "browser_protocol_timeout")
                self.connection.settimeout(min(1, remaining))
                try:
                    message = json.loads(self.connection.recv())
                except (WebSocketTimeoutException, TimeoutError):
                    continue
                if message.get("id") == sequence:
                    if "error" in message:
                        raise BrowserVerificationError("browser_protocol_error")
                    return message.get("result") or {}
        except BrowserVerificationError:
            raise
        except Exception as exc:
            raise BrowserVerificationError("browser_disconnected") from exc

    def evaluate(self, expression, *, deadline=None, still_pending=None):
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                              deadline=deadline, still_pending=still_pending)
        if result.get("exceptionDetails"):
            raise BrowserVerificationError("browser_script_error")
        return result.get("result", {}).get("value")

    def prepare(self, origin=ORIGIN, *, timeout=45, on_event=None, still_pending=None,
                activity="qingyuanzi"):
        """Warm the normal browser and page; no widget or token is created here."""
        if str(origin).rstrip("/") != ORIGIN or activity not in BROWSER_ACTIVITIES:
            raise BrowserVerificationError("browser_origin_not_allowed")
        page_path, action = BROWSER_ACTIVITIES[activity]
        deadline = time.monotonic() + max(0, min(90, float(timeout)))
        if on_event:
            on_event("browser_starting", "")
        self.start(timeout=self._remaining(deadline, still_pending), still_pending=still_pending)
        if on_event:
            on_event("browser_ready", "")
        self.command("Page.bringToFront", deadline=deadline, still_pending=still_pending)
        if self.origin != origin or self.activity != activity:
            self.command("Page.navigate", {"url": ORIGIN + page_path},
                         deadline=deadline, still_pending=still_pending)
            while True:
                self._remaining(deadline, still_pending, "turnstile_script_unavailable")
                # Navigation is asynchronous. An old page's still-live config
                # must never mint a token for the other activity's action.
                ready = ("Boolean(window.turnstile && window.__QYZ_TURNSTILE_CONFIG__"
                         " && location.origin === " + json.dumps(ORIGIN)
                         + " && location.pathname === " + json.dumps(page_path)
                         + " && window.__QYZ_TURNSTILE_CONFIG__.action === " + json.dumps(action) + ")")
                if self.evaluate(ready,
                                 deadline=deadline, still_pending=still_pending):
                    self.origin = origin
                    self.activity = activity
                    break
                time.sleep(min(0.25, self._remaining(deadline, still_pending, "turnstile_script_unavailable")))
        if on_event:
            on_event("page_ready", "")

    def verify(self, origin=ORIGIN, *, timeout=55, on_event=None, still_pending=None,
               activity="qingyuanzi"):
        deadline = time.monotonic() + max(0, min(90, float(timeout)))

        def emit(event, code=""):
            if on_event:
                on_event(event, code)

        self.prepare(origin, timeout=max(0, deadline - time.monotonic()),
                     on_event=on_event, still_pending=still_pending,
                     **({"activity": activity} if activity != "qingyuanzi" else {}))
        emit("helper_ready")
        generation = uuid.uuid4().hex
        self.evaluate("""(generation => {
            const old = window.__qyzAutomaticVerification;
            if (old && old.widgetId != null) { try { window.turnstile.remove(old.widgetId); } catch (_) {} }
            document.getElementById('__qyz_automatic_widget')?.remove();
            const box = document.createElement('div'); box.id = '__qyz_automatic_widget';
            box.style.cssText = 'position:fixed;left:30px;top:30px;padding:30px;background:#fff;z-index:2147483647';
            document.body.append(box);
            const state = {generation, phase:'loading', token:'', error:'', created:performance.now(), widgetId:null};
            window.__qyzAutomaticVerification = state;
            const active = () => window.__qyzAutomaticVerification === state;
            const config = window.__QYZ_TURNSTILE_CONFIG__;
            if (!config.enabled || !config.siteKey) { state.phase='config_error'; return; }
            window.turnstile.ready(() => {
                if (!active()) return;
                state.widgetId = window.turnstile.render(box, {
                    sitekey:config.siteKey, action:config.action,
                    theme:'dark', appearance:'always', execution:'render', size:'normal', retry:'never',
                    'refresh-expired':'manual',
                    callback:token=>{if(active()){state.token=String(token||'');state.phase='solved';}},
                    'error-callback':code=>{if(active()){state.error=String(code||'');state.phase='error';}},
                    'expired-callback':()=>{if(active()){state.token='';state.phase='expired';}},
                    'timeout-callback':()=>{if(active()){state.token='';state.phase='timeout';}},
                    'unsupported-callback':()=>{if(active()){state.phase='unsupported';}},
                    'before-interactive-callback':()=>{if(active()){state.phase='interactive';}},
                });
                if(state.phase==='loading') state.phase='ready';
            });
        })(""" + json.dumps(generation) + ")", deadline=deadline, still_pending=still_pending)
        emit("widget_ready")
        clicked = False
        interactive_since = None
        while time.monotonic() < deadline:
            self._remaining(deadline, still_pending)
            state = self.evaluate("""(() => {
                const s=window.__qyzAutomaticVerification;
                const box=document.getElementById('__qyz_automatic_widget');
                if(!s||!box) return {};
                const rect=box.getBoundingClientRect();
                return {phase:s.phase,error:s.error,generation:s.generation,age:performance.now()-s.created,
                        x:rect.left+51,y:rect.top+63};
            })()""", deadline=deadline, still_pending=still_pending) or {}
            if state.get("generation") != generation:
                raise BrowserVerificationError("verification_generation_changed")
            phase = state.get("phase")
            if phase == "interactive" and interactive_since is None:
                interactive_since = time.monotonic()
            if phase == "solved":
                token = self.evaluate("""(() => {const s=window.__qyzAutomaticVerification;
                    const token=s.token;s.token='';return token;})()""",
                    deadline=deadline, still_pending=still_pending)
                if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or any(ord(c) < 32 for c in token):
                    raise BrowserVerificationError("browser_token_invalid")
                emit("token_generated")
                return token
            if phase in {"error", "unsupported", "expired", "timeout", "config_error"}:
                code = str(state.get("error") or "")
                emit({"error":"widget_error", "timeout":"widget_timeout"}.get(phase, phase), code if code.isdigit() else "")
                raise BrowserVerificationError("turnstile_browser_" + phase, code)
            # Managed Turnstile has a standard checkbox at this position in a
            # normal-size widget. Only one click is attempted per fresh widget.
            if (not clicked and phase == "interactive" and state.get("age", 0) >= 8000
                    and interactive_since is not None and time.monotonic() - interactive_since >= 1):
                emit("interaction_required")
                point = {"x": state["x"], "y": state["y"]}
                self.command("Input.dispatchMouseEvent", {"type":"mouseMoved", **point}, deadline=deadline, still_pending=still_pending)
                time.sleep(0.12)
                self.command("Input.dispatchMouseEvent", {"type":"mousePressed", "button":"left", "buttons":1, "clickCount":1, **point}, deadline=deadline, still_pending=still_pending)
                time.sleep(0.12)
                self.command("Input.dispatchMouseEvent", {"type":"mouseReleased", "button":"left", "buttons":0, "clickCount":1, **point}, deadline=deadline, still_pending=still_pending)
                clicked = True
            time.sleep(min(0.25, self._remaining(deadline, still_pending)))
        emit("widget_timeout")
        raise BrowserVerificationError("turnstile_browser_timeout")

    def close(self):
        if self.connection is not None:
            try:
                self.command("Browser.close", deadline=time.monotonic() + 1)
            except Exception:
                pass
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.origin = ""
        self.activity = ""
        if self.process is not None:
            # xvfb-run may exit before its children. Always terminate this
            # launch's process group, even when the wrapper has already exited.
            try:
                if os.name == "nt":
                    if self.process.poll() is None:
                        self.process.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "nt":
                        self.process.kill()
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)
        self.process = None


class AutomaticTurnstileWorker:
    def __init__(self, broker, browser, *, clock=time.monotonic, max_attempts=2):
        self.broker, self.browser, self.clock = broker, browser, clock
        self.max_attempts = max(1, min(3, max_attempts))
        self.attempts = {}
        self.last_attempt = {}
        self.last_work = clock()
        self.warmup_fingerprint = ""
        self.warmup_attempts = 0
        self.warmup_ready = False
        self.last_warmup = -1000.0

    def _warmup(self):
        row = self.broker.get_warmup()
        if not row:
            return False
        fingerprint = row["event_fingerprint"]
        if fingerprint != self.warmup_fingerprint:
            self.warmup_fingerprint = fingerprint
            self.warmup_attempts = 0
            self.warmup_ready = False
        if self.warmup_ready or self.warmup_attempts >= 2 or self.clock() - self.last_warmup < 5:
            return True
        row = self.broker.claim_warmup(fingerprint)
        if not row:
            return True
        self.warmup_attempts = row["attempts"]
        started = self.clock()

        def pending():
            current = self.broker.get_warmup()
            return bool(current and current.get("event_fingerprint") == fingerprint)

        try:
            self.browser.prepare(row["origin"], timeout=min(45, row["expires_epoch"] - self.broker.clock()),
                                 still_pending=pending,
                                 **({"activity": row["activity"]} if row.get("activity", "qingyuanzi") != "qingyuanzi" else {}))
            self.warmup_ready = True
            LOG.info("Qing Yuanzi browser prewarm ready in %sms", round((self.clock() - started) * 1000))
        except BrowserVerificationError as exc:
            LOG.warning("Qing Yuanzi browser prewarm attempt %s: %s (%sms)",
                        self.warmup_attempts, exc.code, round((self.clock() - started) * 1000))
            if exc.code not in REUSABLE_BROWSER_ERRORS:
                self.browser.close()
        finally:
            self.last_work = self.last_warmup = self.clock()
        return True

    def run_once(self):
        rows = [row for row in self.broker.list_requests() if row.get("status") == "pending"]
        live = {row["request_id"] for row in rows}
        self.attempts = {key: value for key, value in self.attempts.items() if key in live}
        self.last_attempt = {key: value for key, value in self.last_attempt.items() if key in live}
        rows = [row for row in rows if self.attempts.get(row["request_id"], 0) < self.max_attempts
                and self.clock() - self.last_attempt.get(row["request_id"], -1000) >= 5]
        rows.sort(key=lambda row: (self.attempts.get(row["request_id"], 0),
                                  float(row.get("expires_epoch") or 0),
                                  float(row.get("created_epoch") or 0)))
        if not rows:
            if not live and self._warmup():
                return False
            exhausted = bool(live) and all(self.attempts.get(key, 0) >= self.max_attempts for key in live)
            if exhausted or self.clock() - self.last_work >= 20:
                self.browser.close()
            return False
        row = rows[0]
        request_id = row["request_id"]
        self.broker.finish_warmup(row.get("event_fingerprint", ""))
        self.attempts[request_id] = self.attempts.get(request_id, 0) + 1
        # A slow first widget previously occupied the one shared browser for
        # 55 s. Give every account a bounded first turn before longer retries.
        budget = 20 if self.attempts[request_id] == 1 else 35
        budget = max(0, min(budget, float(row.get("expires_epoch") or self.broker.clock() + budget) - self.broker.clock()))
        started = self.clock()
        failure, cf_code = "", ""
        last_stage, failed_stage = "", ""

        def pending():
            return (self.broker.get_request(request_id) or {}).get("status") == "pending"

        def event(stage, code=""):
            nonlocal last_stage
            last_stage = stage
            self.broker.record_browser_event(request_id, stage, code, source="automatic")

        try:
            token = self.browser.verify(row.get("origin"), timeout=budget, on_event=event, still_pending=pending,
                                        **({"activity": row["activity"]} if row.get("activity", "qingyuanzi") != "qingyuanzi" else {}))
            if pending():
                self.broker.submit_token(request_id, token)
                LOG.info("[%s/%s] automatic browser token submitted", row.get("account"), row.get("identity"))
            token = ""
            if not any(item.get("status") == "pending" for item in self.broker.list_requests()):
                # Verification and battle share a small VPS. Once the queue is
                # complete, release Chromium now instead of keeping its working
                # set alive throughout another 20 seconds of combat.
                self.browser.close()
        except (BrowserVerificationError, TurnstileRequestError) as exc:
            failure, cf_code = exc.code, getattr(exc, "cf_code", "")
            failed_stage = last_stage
            LOG.warning("[%s/%s] automatic verification attempt %s: %s CF=%s (%sms, budget %sms)",
                        row.get("account"), row.get("identity"), self.attempts[request_id],
                        failure, cf_code, round((self.clock() - started) * 1000), round(budget * 1000))
            if (isinstance(exc, BrowserVerificationError)
                    and exc.code not in REUSABLE_BROWSER_ERRORS
                    and not exc.code.startswith("turnstile_browser_")):
                if pending():
                    event("config_error")
                self.browser.close()
        finally:
            try:
                self.broker.record_browser_attempt(
                    request_id, attempt=self.attempts[request_id],
                    duration_ms=round((self.clock() - started) * 1000),
                    budget_ms=round(budget * 1000), error=failure, cf_code=cf_code,
                    stage=failed_stage or last_stage,
                )
            except Exception as exc:
                LOG.warning("Browser attempt timing could not be saved: %s", type(exc).__name__)
            self.last_work = self.clock()
            self.last_attempt[request_id] = self.last_work
        return True


@contextmanager
def worker_lease(queue_dir, *, cleanup=None):
    """One browser across all accounts, keeping the small VPS within its budget."""
    path = Path(queue_dir) / ".browser-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.write(b"0"); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BrowserVerificationError("browser_worker_already_running") from exc
        try:
            yield
        finally:
            try:
                if cleanup:
                    cleanup()
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Verify callbacks without joining a battle")
    parser.add_argument("--count", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--chrome")
    parser.add_argument("--queue-dir")
    parser.add_argument("--no-sandbox", action="store_true")
    args = parser.parse_args()
    broker = WorldBossTurnstileBroker(args.queue_dir)
    broker.queue_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler()]
    if not args.probe:
        handlers.append(RotatingFileHandler(
            broker.queue_dir / "browser-worker.log", maxBytes=512 * 1024,
            backupCount=1, encoding="utf-8",
        ))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    profile = broker.queue_dir / "browser_profile"
    browser = NativeTurnstileBrowser(profile, chrome=args.chrome, no_sandbox=args.no_sandbox)

    def stopped(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stopped)
    try:
        with worker_lease(broker.queue_dir, cleanup=browser.close):
            if args.probe:
                for index in range(args.count):
                    started = time.monotonic()
                    token = browser.verify(on_event=lambda event, code: LOG.info("Probe browser stage: %s CF=%s", event, code))
                    print(json.dumps({"verification": index + 1, "success": True,
                                      "token_length": len(token),
                                      "duration_seconds": round(time.monotonic() - started, 2)}), flush=True)
                    token = ""
            else:
                worker = AutomaticTurnstileWorker(broker, browser)
                LOG.info("Automatic Qing Yuanzi browser verification is ready")
                while True:
                    try:
                        worker.run_once()
                    except Exception as exc:
                        LOG.error("Automatic verification interrupted: %s", type(exc).__name__)
                        browser.close()
                        time.sleep(2)
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except BrowserVerificationError as exc:
        print(json.dumps({"success": False, "error": exc.code, "cf_code": exc.cf_code}), flush=True)
        return 1
    finally:
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
