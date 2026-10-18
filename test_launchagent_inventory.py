import json
import plistlib
import unittest
from pathlib import Path

from flow_registry import SHARED_LAUNCHAGENTS, active_entries, launchagent_labels


SCRIPTS_DIR = Path("scripts")
ACTIVE_CONFIG = Path("config/active_flows.json")
# Repo plists that intentionally have no registry label of their own.
UNREGISTERED_PLISTS = {
    # Interval flows are started by return-deadline-dispatch; this plist is the
    # documented StartInterval template for the 7200-second 抚摸法宝 flow.
    'ai.hermes.xiuxian.tg9100000002.fumofabao_qingzhufengyunjian_shenlei.send',
}

class LaunchAgentInventoryTests(unittest.TestCase):
    def _repo_plist_labels(self) -> dict[str, Path]:
        labels: dict[str, Path] = {}
        roots = (SCRIPTS_DIR, Path('qingyuanzi-new-source'), Path('fishing'))
        paths = [path for root in roots for path in root.glob('ai.hermes.xiuxian.*.plist')]
        for path in sorted(paths):
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
            label = payload.get("Label")
            self.assertIsInstance(label, str, f"{path} missing string Label")
            self.assertNotIn(label, labels, f"duplicate LaunchAgent label {label}")
            labels[label] = path
        return labels

    def _active_expected_labels(self) -> list[str]:
        payload = json.loads(ACTIVE_CONFIG.read_text(encoding="utf-8"))
        return launchagent_labels(active_entries(payload))

    def test_registry_uses_business_account_for_demo_player_06_credential_source(self) -> None:
        payload = json.loads(ACTIVE_CONFIG.read_text(encoding="utf-8"))
        credential_source = "tg9100000007"
        business_account = "tg9100000006"
        entries = payload["flows"]
        leaked = [
            entry
            for entry in entries
            if entry.get("account") == credential_source
        ]
        self.assertEqual([], leaked)
        demo_player_06_flows = {
            entry["flow"]
            for entry in entries
            if entry.get("account") == business_account and entry.get("enabled", True)
        }
        self.assertTrue(
            {"jiaohuan_fabao", "shendubiguan", "tanxunliefeng", "wendao"}.issubset(demo_player_06_flows)
        )
        self.assertNotIn("jiaohuan_gongfa", demo_player_06_flows)
        self.assertNotIn("yuanyingchuqiao", demo_player_06_flows)

    def test_removed_demo_player_06_yuanying_has_no_runner_or_scheduler(self) -> None:
        prefix = 'ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.'
        self.assertFalse(any(label.startswith(prefix) for label in self._active_expected_labels()))
        self.assertFalse(any(label.startswith(prefix) for label in self._repo_plist_labels()))
        for credential in ('tg9100000006', 'tg9100000007'):
            self.assertFalse(list(SCRIPTS_DIR.glob(f'*yuanying*{credential}*')))
        self.assertFalse(Path('yuanying_retreat.py').exists())

    def test_every_active_flow_has_repo_plist_or_explicit_alias(self) -> None:
        labels = self._repo_plist_labels()
        missing: list[str] = []
        for expected in self._active_expected_labels():
            if expected not in labels:
                missing.append(expected)
        self.assertEqual([], missing)

    def test_every_repo_plist_is_registered_or_an_explicit_exception(self) -> None:
        # A leftover plist is one bootstrap away from running an unregistered
        # (often crash-looping) job; delete it or register it.
        expected = set(self._active_expected_labels())
        stray = sorted(set(self._repo_plist_labels()) - expected - UNREGISTERED_PLISTS)
        self.assertEqual([], stray)
        self.assertEqual(set(), UNREGISTERED_PLISTS & expected)

    def test_keepalive_jobs_back_off_between_crash_relaunches(self) -> None:
        # Every listener wrapper probes Telegram authorization before exec; a
        # revoked session must not make all of an account's listeners reconnect
        # every 5-10 seconds. (The trade listeners' throttle is pinned by
        # test_jiaohuan_fabao_short_deadline.)
        fast = []
        for path in sorted(SCRIPTS_DIR.glob('ai.hermes.xiuxian.*.plist')):
            payload = plistlib.loads(path.read_bytes())
            if (payload.get('KeepAlive') is True and payload.get('ThrottleInterval', 10) < 30
                    and '.jiaohuan_fabao.' not in path.name):
                fast.append(path.name)
        self.assertEqual([], fast)

    def test_log_retention_applies_weekly_at_a_fixed_time(self) -> None:
        payload = plistlib.loads((SCRIPTS_DIR / 'ai.hermes.xiuxian.log-retention.plist').read_bytes())
        # A StartInterval timer restarts on every reload, so a weekly job may never fire.
        self.assertNotIn('StartInterval', payload)
        self.assertEqual({'Weekday', 'Hour', 'Minute'}, set(payload['StartCalendarInterval']))
        arguments = payload['ProgramArguments']
        self.assertTrue(arguments[1].endswith('/scripts/xiuxian_log_retention_apply.py'))
        self.assertEqual('10', arguments[arguments.index('--keep-mib') + 1])

    def test_jobs_run_the_project_venv_not_an_unpinned_uv_environment(self) -> None:
        # `uv run --with telethon` resolves whatever release is newest at run time.
        uv_jobs = [str(path) for path in self._repo_plist_labels().values()
                   if Path(plistlib.loads(path.read_bytes())['ProgramArguments'][0]).name == 'uv']
        self.assertEqual([], uv_jobs)

    def test_intentional_aliases_and_helpers_point_to_repo_plists(self) -> None:
        labels = self._repo_plist_labels()
        missing_alias_targets = sorted(
            target for target in set(SHARED_LAUNCHAGENTS.values()) if target not in labels
        )
        self.assertEqual([], missing_alias_targets)
        registry = active_entries(json.loads(ACTIVE_CONFIG.read_text(encoding='utf-8')))
        missing_helpers = sorted(entry['label'] for entry in registry
                                 if 'helper' in entry['kinds'] and entry['label'] not in labels)
        self.assertEqual([], missing_helpers)

    def test_shell_wrappers_locate_the_checkout_relative_to_themselves(self) -> None:
        # The VPS image runs the checkout at another path; only plists (launchd
        # templates) may carry the macOS absolute project root.
        pinned = sorted(str(path) for path in SCRIPTS_DIR.rglob('*.sh')
                        if '/Users/operator/.hermes/workspace/projects/xiuxian'
                        in path.read_text(encoding='utf-8'))
        self.assertEqual([], pinned)

    def test_shell_jobs_keep_launchd_stderr_in_their_flow_log(self) -> None:
        # Failures before the `>> log 2>&1` redirect (login shell, cd, mkdir)
        # would otherwise be lost.
        import re

        wrong = []
        for path in sorted(SCRIPTS_DIR.glob('ai.hermes.xiuxian.*.plist')):
            payload = plistlib.loads(path.read_bytes())
            arguments = payload.get('ProgramArguments') or []
            if arguments[:2] != ['/bin/bash', '-lc']:
                continue
            log = re.search(r'>> (\S+) 2>&1\s*$', arguments[2]).group(1)
            if payload.get('StandardErrorPath') != f"{payload['WorkingDirectory']}/{log}":
                wrong.append(path.name)
        self.assertEqual([], wrong)
        restore = (SCRIPTS_DIR / 'restore_xiuxian_launchagents.sh').read_text(encoding='utf-8')
        self.assertIn("for key in ('StandardOutPath', 'StandardErrorPath'):", restore)

    def test_wrappers_do_not_restate_the_forced_outbound_route(self) -> None:
        # account_flow_paths.sh forces the canonical send/reply-scan route for
        # every flow; copies after it are dead and hide where the guard lives.
        pinned_elsewhere = {'run_recipe_notice_push_tg9100000001.sh'}  # test_recipe_notice_monitor pins its copy
        route = ('export TG_TARGET=', 'export TG_TOPIC_ID=', 'export TG_REPLY_SCAN_TARGET=',
                 'export TG_REPLY_SCAN_TOPIC_ID=')
        restated = []
        for path in sorted(SCRIPTS_DIR.rglob('*.sh')):
            text = path.read_text(encoding='utf-8')
            after_helper = (path.parent.name == 'lib' and path.name != 'account_flow_paths.sh'
                            or 'source ./scripts/lib/account_flow_paths.sh' in text)
            if after_helper and path.name not in pinned_elsewhere and any(
                    line.startswith(route) for line in text.splitlines()):
                restated.append(path.name)
        self.assertEqual([], restated)

    def test_shared_transport_env_is_optional_everywhere(self) -> None:
        # account_flow_paths.sh treats config/accounts/mtproto.env as optional;
        # an unguarded source would crash-loop a KeepAlive job when it is absent.
        unguarded = []
        for path in sorted(SCRIPTS_DIR.rglob('*.sh')):
            lines = path.read_text(encoding='utf-8').splitlines()
            for index, line in enumerate(lines):
                if line.strip().startswith('source ./config/accounts/mtproto.env') or \
                        '; then source ./config/accounts/mtproto.env' in line:
                    guard = line if 'then source' in line else lines[index - 1]
                    if '[[ -r ./config/accounts/mtproto.env ]]' not in guard:
                        unguarded.append(f'{path}:{index + 1}')
        self.assertEqual([], unguarded)

    def test_loaded_live_launchagents_have_repo_plist_snapshot_when_available(self) -> None:
        # launchctl may be unavailable in CI/non-macOS; skip rather than making
        # static tests platform-dependent. On the live macOS host this catches
        # scheduler reality that exists under ~/Library/LaunchAgents but was not
        # committed to scripts/.
        import shutil
        import subprocess

        if shutil.which("launchctl") is None:
            self.skipTest("launchctl unavailable")
        try:
            output = subprocess.check_output(["launchctl", "list"], text=True, errors="replace")
        except Exception as exc:  # pragma: no cover - platform permission guard
            self.skipTest(f"launchctl list unavailable: {exc}")
        loaded = {
            line.split()[-1]
            for line in output.splitlines()
            if "ai.hermes.xiuxian." in line
        }
        labels = self._repo_plist_labels()
        missing = sorted(label for label in loaded if label not in labels)
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
