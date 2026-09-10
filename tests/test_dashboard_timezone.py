"""Regression checks for a Dashboard moved between VPS/browser time zones."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
TIMEZONES = ("Etc/UTC", "America/Los_Angeles", "Asia/Tokyo", "Asia/Shanghai")


class DashboardServerTimezoneTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(time, "tzset"), "The production Linux clock uses tzset")
    def test_standalone_dashboard_matches_worker_deadlines_and_daily_reset(self):
        # A fresh process matters: importing an account worker elsewhere in the
        # suite used to hide the Dashboard's missing timezone initialization.
        script = r'''
import json
import time
from datetime import datetime, timezone
from unittest.mock import patch

time.tzset()
import dashboard_server as dashboard

instant = datetime(2026, 9, 9, 16, 30, tzinfo=timezone.utc).timestamp()
class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(instant, tz)

with patch.object(dashboard, "datetime", FixedDateTime):
    future = dashboard.time_command({"next": "2026-09-10 01:00:00"}, "next", ".test")
    expired = dashboard.time_command({"next": "2026-09-10 00:00:00"}, "next", ".test")
    daily = dashboard.daily_done_command(
        {"last_date": "2026-09-10"}, ".daily", date_key="last_date")
    print(json.dumps({
        "clock": str(FixedDateTime.now()),
        "c_clock": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(instant)),
        "future_seconds": future["next_seconds"], "future_tone": future["tone"],
        "expired_seconds": expired["next_seconds"], "expired_tone": expired["tone"],
        "daily_tone": daily["tone"],
    }))
'''
        for zone in TIMEZONES:
            with self.subTest(host_timezone=zone):
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-c", script], cwd=ROOT,
                    env=dict(os.environ, TZ=zone), capture_output=True,
                    text=True, encoding="utf-8", timeout=45,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                self.assertEqual(data, {
                    "clock": "2026-09-10 00:30:00", "c_clock": "2026-09-10 00:30:00",
                    "future_seconds": 1800, "future_tone": "cooldown",
                    "expired_seconds": 0, "expired_tone": "ready", "daily_tone": "done",
                })


class DashboardBrowserTimezoneTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required to check browser dates")
    def test_countdowns_and_calendar_dates_do_not_follow_browser_timezone(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        formatter = re.search(
            r"^        const dashboardDateFormatter =.*?^        \}\);",
            html, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(formatter)
        scripts = [formatter.group(0)]
        for name in (
            "dashboardDateParts", "parseDashboardDate", "dashboardToday", "formatTime",
            "commandNextSeconds", "recordDateText", "duelDateValue",
        ):
            function = re.search(
                rf"^        function {name}\(.*?^        \}}",
                html, re.MULTILINE | re.DOTALL,
            )
            self.assertIsNotNone(function, name)
            scripts.append(function.group(0))
        script = r'''
const assert = require('node:assert/strict');
const NativeDate = Date;
const instant = NativeDate.parse('2026-09-09T16:30:00Z');
globalThis.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [instant])); }
    static now() { return instant; }
};
const document = { getElementById: () => null };
''' + "\n".join(scripts) + r'''
for (const value of [
    '2026-09-10 01:00:00', '2026-09-10 01:00',
    '2026-09-10T01:00:00+08:00', '2026-09-10T02:00:00+09:00',
    '2026-09-09T17:00:00Z'
]) {
    assert.equal(parseDashboardDate(value).toISOString(), '2026-09-09T17:00:00.000Z', value);
    assert.equal(commandNextSeconds({at: value}), 1800, value);
    assert.equal(formatTime(value), '0h 30m 0s', value);
    assert.equal(recordDateText(value), '01:00:00', value);
}
assert.equal(parseDashboardDate('2026-09-10').toISOString(), '2026-09-09T16:00:00.000Z');
assert.equal(commandNextSeconds({at: '2026-09-10 00:00:00'}), 0);
assert.equal(commandNextSeconds({at: '2026-09-10 01:00:00', next_seconds: 75}), 75);
assert.equal(commandNextSeconds({at: '2026-09-10 01:00:00', control_disabled: true}), null);
for (const value of ['', '---', '就绪', '本轮已无下一次', 'not-a-date']) {
    assert.equal(parseDashboardDate(value), null);
}
assert.equal(formatTime('就绪'), '就绪');
assert.equal(formatTime('2026-09-10', true), '✅ 已执行');
assert.equal(dashboardToday(), '2026-09-10');
assert.equal(duelDateValue(), '2026-09-10');
assert.equal(recordDateText('2026-09-09 23:00:00'), '09-09 23:00:00');
console.log(JSON.stringify({offset: new Date().getTimezoneOffset()}));
'''
        offsets = (0, 420, -540, -480)
        for zone, offset in zip(TIMEZONES, offsets):
            with self.subTest(browser_timezone=zone):
                result = subprocess.run(
                    [shutil.which("node"), "-"], input=script, cwd=ROOT,
                    env=dict(os.environ, TZ=zone), capture_output=True,
                    text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["offset"], offset)


if __name__ == "__main__":
    unittest.main()
