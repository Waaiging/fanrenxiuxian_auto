import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from soul_curse_features import SOUL_CURSE_SETTINGS_FILE


class _FakeActor:
    """Minimal actor exposing only what the switch needs."""

    def __init__(self, account_key="main"):
        self.account_key = account_key


class SoulCurseIdentitySwitchTests(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile

        self._temp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        self._original = SOUL_CURSE_SETTINGS_FILE
        self.temp_path = self._temp.name
        self._temp.write(json.dumps({
            "enabled": True,
            "identities": {
                "main": {"主魂": True, "无咎子": False},
                "sub": ["主魂"],
                "xiaohao": {"主魂": False},
            },
        }))
        self._temp.close()
        # Patch the module-level constant for the duration of the test.
        import soul_curse_features
        self._module = soul_curse_features
        self._orig_settings = soul_curse_features.SOUL_CURSE_SETTINGS_FILE
        soul_curse_features.SOUL_CURSE_SETTINGS_FILE = self.temp_path

    def tearDown(self):
        self._module.SOUL_CURSE_SETTINGS_FILE = self._orig_settings
        os.unlink(self.temp_path)

    def _enabled(self, account, identity="主魂"):
        from soul_curse_features import SoulCurseMixin

        class _Actor(SoulCurseMixin, _FakeActor):
            pass

        return _Actor(account_key=account).soul_curse_identity_enabled(
            account=account, identity=identity
        )

    def test_dict_entry_enabled(self):
        self.assertTrue(self._enabled("main", "主魂"))

    def test_dict_entry_disabled(self):
        self.assertFalse(self._enabled("main", "无咎子"))

    def test_list_entry(self):
        self.assertTrue(self._enabled("sub", "主魂"))
        self.assertFalse(self._enabled("sub", "厚土"))

    def test_dict_entry_false(self):
        self.assertFalse(self._enabled("xiaohao", "主魂"))

    def test_unknown_account_defaults_disabled(self):
        self.assertFalse(self._enabled("waaiging", "主魂"))


class SoulCurseNoSettingsTests(unittest.TestCase):
    """Missing/broken settings file => everything disabled (safe default)."""

    def setUp(self):
        import soul_curse_features

        self._module = soul_curse_features
        self._orig_settings = soul_curse_features.SOUL_CURSE_SETTINGS_FILE
        soul_curse_features.SOUL_CURSE_SETTINGS_FILE = "/nonexistent/soul_curse_settings.json"

    def tearDown(self):
        self._module.SOUL_CURSE_SETTINGS_FILE = self._orig_settings

    def _enabled(self, account, identity="主魂"):
        from soul_curse_features import SoulCurseMixin

        class _Actor(SoulCurseMixin, _FakeActor):
            pass

        return _Actor(account_key=account).soul_curse_identity_enabled(
            account=account, identity=identity
        )

    def test_missing_file_disabled(self):
        self.assertFalse(self._enabled("main", "主魂"))
        self.assertFalse(self._enabled("sub", "主魂"))

    def test_switch_off_publisher(self):
        from soul_curse_features import SoulCurseMixin

        class _Actor(SoulCurseMixin, _FakeActor):
            pass

        actor = _Actor(account_key="main")
        self.assertIsNone(
            _Actor(account_key="waaiging").soul_curse_publisher_profile()
        )


if __name__ == "__main__":
    unittest.main()
