"""Run unittest checks in a disposable copy without deployment credentials/state.

Examples:
    python tools/run_tests.py
    python tools/run_tests.py tests.test_state_io --report-dir test-results
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RUNNER = r'''
import contextlib,json,sys,time,unittest
from pathlib import Path
modules,log_path,result_path=json.loads(sys.argv[1])
started=time.monotonic()
with open(log_path,'w',encoding='utf-8') as output:
    with contextlib.redirect_stdout(output),contextlib.redirect_stderr(output):
        loader=unittest.TestLoader()
        suite=(loader.loadTestsFromNames(modules) if modules
               else loader.discover('tests',pattern='test_*.py',top_level_dir='.'))
        result=unittest.TextTestRunner(stream=output,verbosity=2).run(suite)
report={'tests':result.testsRun,'failures':[t.id() for t,_ in result.failures],
        'errors':[t.id() for t,_ in result.errors],'skipped':len(result.skipped),
        'seconds':round(time.monotonic()-started,2),'python':sys.version,
        'successful':result.wasSuccessful() and result.testsRun>0}
Path(result_path).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False),flush=True)
if not report['successful']:
    for test,traceback in result.failures+result.errors:
        print('\n'+test.id()+'\n'+traceback,file=sys.stderr)
sys.exit(0 if report['successful'] else 1)
'''


def copy_test_sources(destination: Path) -> None:
    """Only copy source and public fixtures; never copy runtime JSON or .env."""
    sources = [
        path for path in ROOT.glob("*.py")
        if not path.name.startswith((".", "_"))
    ]
    sources.extend((ROOT / "tests").rglob("*.py"))
    sources.extend(ROOT.glob("*.example.json"))
    sources.extend(ROOT / name for name in ("dashboard.html", "start_all.sh", "xuangu_question_bank.json"))
    for source in sources:
        if not source.is_file() or source.is_symlink():
            continue
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="*", help="unittest module, class, or test names")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "test-results")
    parser.add_argument("--label", default="unittest")
    args = parser.parse_args()
    if not args.label or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in args.label):
        parser.error("--label must contain only letters, digits, '_' or '-'")
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    log_path = report_dir / f"{args.label}.log"
    result_path = report_dir / f"{args.label}.json"
    env = dict(os.environ, PYTHONUTF8="1")
    # Avoid inheriting a caller's module paths or real Dashboard credentials.
    env.pop("PYTHONPATH", None)
    for key in list(env):
        if key.startswith("DASHBOARD_"):
            env.pop(key)
    temp_parent = Path(tempfile.gettempdir()).resolve()
    with tempfile.TemporaryDirectory(prefix="fanren-tests-", dir=temp_parent) as temporary:
        sandbox = Path(temporary).resolve()
        if sandbox.parent != temp_parent:
            raise RuntimeError("Unexpected test sandbox location")
        copy_test_sources(sandbox)
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", RUNNER,
             json.dumps([args.modules, str(log_path), str(result_path)])],
            cwd=sandbox, env=env,
        )
    print(f"Test log: {log_path}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
