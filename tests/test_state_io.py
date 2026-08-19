import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import state_io


class StateIOTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "state.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_atomic_save_writes_valid_json_without_leftover_temp_file(self):
        state_io.save_json_state(str(self.path), {"value": "中文", "count": 1})

        self.assertEqual(self.read(), {"value": "中文", "count": 1})
        self.assertEqual(list(self.path.parent.glob(f".{self.path.name}.*.tmp")), [])

    def test_second_save_keeps_previous_valid_document_as_backup(self):
        state_io.save_json_state(str(self.path), {"version": 1})
        state_io.save_json_state(str(self.path), {"version": 2})

        self.assertEqual(self.read(), {"version": 2})
        self.assertEqual(
            json.loads(Path(f"{self.path}.bak").read_text(encoding="utf-8")),
            {"version": 1},
        )

    def test_load_restores_corrupt_main_file_from_last_valid_backup(self):
        state_io.save_json_state(str(self.path), {"version": 1})
        state_io.save_json_state(str(self.path), {"version": 2})
        self.path.write_text('{"version":', encoding="utf-8")

        recovered = state_io.load_json_state(str(self.path), default={})

        self.assertEqual(recovered, {"version": 1})
        self.assertEqual(self.read(), {"version": 1})

    def test_replace_failure_leaves_original_document_valid(self):
        state_io.save_json_state(str(self.path), {"version": 1})

        with patch("state_io.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                state_io.save_json_state(str(self.path), {"version": 2})

        self.assertEqual(self.read(), {"version": 1})

    def test_serialization_failure_never_touches_original_or_backup(self):
        state_io.save_json_state(str(self.path), {"version": 1})
        backup_path = Path(f"{self.path}.bak")
        self.assertFalse(backup_path.exists())

        with self.assertRaises(TypeError):
            state_io.save_json_state(str(self.path), {"bad": object()})

        self.assertEqual(self.read(), {"version": 1})
        self.assertFalse(backup_path.exists())

    def test_concurrent_transactions_do_not_lose_updates(self):
        state_io.save_json_state(str(self.path), {"count": 0})
        failures = []

        def increment_many():
            try:
                for _ in range(25):
                    state_io.update_json_state(
                        str(self.path),
                        lambda value: {"count": int(value.get("count") or 0) + 1},
                        default={},
                    )
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        workers = [threading.Thread(target=increment_many) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(failures, [])
        self.assertEqual(self.read(), {"count": 100})

    def test_lock_timeout_is_bounded_and_does_not_modify_state(self):
        state_io.save_json_state(str(self.path), {"version": 1})
        locked = threading.Event()
        release = threading.Event()

        def hold_lock():
            with state_io.json_file_lock(str(self.path), timeout=1):
                locked.set()
                release.wait(2)

        holder = threading.Thread(target=hold_lock)
        holder.start()
        self.assertTrue(locked.wait(1))
        try:
            with self.assertRaises(state_io.StateFileLockTimeout):
                state_io.update_json_state(
                    str(self.path),
                    lambda value: {"version": 2},
                    default={},
                    lock_timeout=0.05,
                )
        finally:
            release.set()
            holder.join()

        self.assertEqual(self.read(), {"version": 1})


if __name__ == "__main__":
    unittest.main()
