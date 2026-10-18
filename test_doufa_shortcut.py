"""Offline tests: never contact Telegram, launch a duel, or load the live gateway."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).parent / "scripts/hermes_doufa_plugin/__init__.py"


class DoufaShortcutTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SOURCE.is_file(), "The zero-LLM /doufa handler must exist")
        spec = importlib.util.spec_from_file_location("doufa_plugin_test", SOURCE)
        assert spec is not None and spec.loader is not None
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "state").mkdir()

    def test_register_uses_direct_handler_profile_root_and_catches_errors(self):
        import types
        from unittest.mock import Mock
        constants = types.ModuleType("hermes_constants")
        setattr(constants, "get_hermes_home", lambda: self.root)
        ctx = Mock()
        ctx.get_config.return_value = "12345"
        with patch.dict("sys.modules", {"hermes_constants": constants}):
            self.plugin.register(ctx)
        ctx.register_command.assert_called_once()
        call = ctx.register_command.call_args
        self.assertEqual("doufa", call.args[0])
        handler = call.kwargs["handler"]
        with patch.object(self.plugin, "handle_slash", return_value="DIRECT") as handle:
            self.assertEqual("DIRECT", handler("status"))
            handle.assert_called_once_with("status", self.root / "workspace/projects/xiuxian", "12345")
        with patch.object(self.plugin, "handle_slash", side_effect=ValueError("private detail")):
            self.assertIn("未確認", handler(""))
            self.assertNotIn("private detail", handler(""))

    def test_only_configured_owner_private_chat_can_dispatch(self):
        from contextvars import ContextVar
        keys = {
            "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_USER_ID": "12345",
            "HERMES_SESSION_CHAT_ID": "12345", "HERMES_SESSION_CHAT_TYPE": "dm",
        }
        vars_ = {k: ContextVar(k) for k in keys}
        tokens = {k: vars_[k].set(v) for k, v in keys.items()}
        try:
            with patch.object(self.plugin, "handle_local", return_value="SAFE") as handle:
                self.assertEqual("SAFE", self.plugin.handle_slash("status", self.root, "12345"))
                handle.assert_called_once_with("status", self.root)
                handle.reset_mock()
                for key, bad in [("HERMES_SESSION_USER_ID", "999"), ("HERMES_SESSION_CHAT_ID", "-100123"), ("HERMES_SESSION_CHAT_TYPE", "group"), ("HERMES_SESSION_PLATFORM", "discord")]:
                    token = vars_[key].set(bad)
                    self.assertIn("只接受", self.plugin.handle_slash("", self.root, "12345"))
                    vars_[key].reset(token)
                self.assertIn("只接受", self.plugin.handle_slash("", self.root, ""))
                handle.assert_not_called()
        finally:
            for k, token in tokens.items():
                vars_[k].reset(token)
        with patch.dict("os.environ", keys), patch.object(self.plugin, "handle_local") as handle:
            self.assertIn("只接受", self.plugin.handle_slash("", self.root, "12345"))
            handle.assert_not_called()

    def test_concurrent_start_does_not_invoke_activation(self):
        import fcntl
        with (self.root / "state/doufa_shortcut.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.plugin.subprocess, "run") as run:
                reply = self.plugin.handle_local("start", self.root)
                self.assertIn("處理中", reply)
                run.assert_not_called()

    def test_activation_never_kills_existing_campaign(self):
        script = SOURCE.parents[1] / "activate_doufa_demo_player_02.sh"
        self.assertNotIn("kickstart -k", script.read_text())

    def test_launchctl_failure_is_reported_without_start_or_exception(self):
        for failure in [subprocess.TimeoutExpired("launchctl", 5), OSError("denied")]:
            with patch.object(self.plugin.subprocess, "run", side_effect=failure) as run:
                reply = self.plugin.handle_local("", self.root)
                self.assertIn("未確認", reply)
                self.assertEqual(1, run.call_count)
        with patch.object(self.plugin.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")) as run:
            reply = self.plugin.handle_local("", self.root)
            self.assertIn("未確認", reply)
            self.assertEqual(1, run.call_count)

    def test_status_is_read_only_and_unknown_args_do_not_execute(self):
        stopped = subprocess.CompletedProcess([], 0, "state = not running\n", "")
        with patch.object(self.plugin.subprocess, "run", return_value=stopped) as run:
            reply = self.plugin.handle_local("status", self.root)
            self.assertIn("未執行", reply)
            self.assertEqual(1, run.call_count)
            for arg in ["stop", "; echo BAD", "start demo_player_01", "help"]:
                reply = self.plugin.handle_local(arg, self.root)
                self.assertIn("/doufa", reply)
            self.assertEqual(1, run.call_count)

    def test_stopped_campaign_activates_existing_wrapper_and_verifies_pid(self):
        wrapper = self.root / "scripts/activate_doufa_demo_player_02.sh"
        wrapper.parent.mkdir()
        wrapper.write_text("#!/bin/bash\n")
        stopped = subprocess.CompletedProcess([], 0, "state = not running\n", "")
        started = subprocess.CompletedProcess([], 0, "state = running\n pid = 123\n", "")
        with patch.object(self.plugin.subprocess, "run", side_effect=[stopped, subprocess.CompletedProcess([], 0, "", ""), started]) as run:
            reply = self.plugin.handle_local("start", self.root)
        self.assertIn("已啟動", reply)
        self.assertEqual(["/bin/bash", str(wrapper)], run.call_args_list[1].args[0])
        self.assertEqual(3, run.call_count)

    def test_running_campaign_returns_without_activation(self):
        running = subprocess.CompletedProcess([], 0, "state = running\n pid = 123\n", "")
        with patch.object(self.plugin.subprocess, "run", return_value=running) as run:
            reply = self.plugin.handle_local("", self.root)
        self.assertIn("執行中", reply)
        self.assertEqual(1, run.call_count)
        self.assertEqual("/bin/launchctl", run.call_args.args[0][0])
        self.assertEqual("print", run.call_args.args[0][1])


if __name__ == "__main__":
    unittest.main()
