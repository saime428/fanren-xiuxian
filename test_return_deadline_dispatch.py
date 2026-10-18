import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.return_deadline_dispatch import collect_due_labels
from scripts import return_deadline_dispatch as dispatch


def confirmed_countdown(account, flow, due):
    return {"status": "countdown_pending", "not_before_ts": due, "acked": True,
            "actor_id": int(account[2:]), "flow_slug": flow, "message": ".查看闭关" if flow == "shendubiguan" else ".元婴状态",
            "target": "@example_game_group", "topic_id": 7310786, "sent_message_id": 1,
            "reply_message_id": 2, "reply_sender_id": 8917921351}


class ReturnDeadlineDispatchTests(unittest.TestCase):
    def test_due_dispatch_preserves_an_existing_waiting_sender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = {'pid': 123}
            def launchctl(command, **options):
                if '-k' in command:
                    live['pid'] += 1
                return SimpleNamespace(returncode=0)
            with patch.dict(dispatch.os.environ, {'XIU_STATE_ROOT': tmp}), \
                 patch.object(dispatch, 'dispatch_intervals', return_value=set()), \
                 patch.object(dispatch, 'collect_due_labels', return_value=['ai.hermes.xiuxian.tg1.shendubiguan.send']), \
                 patch.object(dispatch.subprocess, 'run', side_effect=launchctl):
                self.assertEqual(dispatch.main(), 0)
            self.assertEqual(live['pid'], 123)
            state = json.loads((Path(tmp) / 'return_deadline_dispatch.json').read_text())
            self.assertIn('ai.hermes.xiuxian.tg1.shendubiguan.send', state['last_kick_by_label'])

    def test_only_due_countdown_flows_are_kicked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            due = root / "accounts/tg1/shendubiguan"
            future = root / "accounts/tg2/yuanyingchuqiao"
            stale = root / "accounts/tg3/shendubiguan"
            for path in (due, future, stale):
                path.mkdir(parents=True)
            (due / "status_probe.json").write_text(json.dumps(confirmed_countdown("tg1", "shendubiguan", 995)), encoding="utf-8")
            (future / "status_probe.json").write_text(json.dumps(confirmed_countdown("tg2", "yuanyingchuqiao", 1200)), encoding="utf-8")
            (stale / "status_probe.json").write_text(json.dumps({"status": "clear", "not_before_ts": 900}), encoding="utf-8")

            registry = root / "active.json"
            registry.write_text(json.dumps({"flows": [{"account": "tg1", "flow": flow, "enabled": True, "kinds": ["send"]} for flow in ("shendubiguan", "yuanyingchuqiao")]}))
            labels = collect_due_labels(root, registry_path=registry, now_ts=1000, lead_seconds=10, overdue_max_seconds=300)

            self.assertEqual(["ai.hermes.xiuxian.tg1.shendubiguan.send"], labels)

    def test_dispatch_deduplicates_recent_kick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            flow = root / "accounts/tg1/yuanyingchuqiao"
            flow.mkdir(parents=True)
            (flow / "status_probe.json").write_text(json.dumps(confirmed_countdown("tg1", "yuanyingchuqiao", 995)), encoding="utf-8")
            dispatch_state = root / "return_deadline_dispatch.json"
            dispatch_state.write_text(json.dumps({"last_kick_by_label": {"ai.hermes.xiuxian.tg1.yuanyingchuqiao.send": 950}}), encoding="utf-8")

            registry = root / "active.json"
            registry.write_text(json.dumps({"flows": [{"account": "tg1", "flow": flow, "enabled": True, "kinds": ["send"]} for flow in ("shendubiguan", "yuanyingchuqiao")]}))
            labels = collect_due_labels(root, registry_path=registry, now_ts=1000, lead_seconds=10, overdue_max_seconds=300, dedupe_seconds=120)

            self.assertEqual([], labels)

    def test_shendubiguan_runner_never_sleeps_for_entire_countdown(self) -> None:
        text = Path("scripts/lib/run_shendubiguan_send.sh").read_text(encoding="utf-8")
        self.assertIn("still cooling down; skip this run", text)
        self.assertNotIn('sleep "${wait_seconds}"', text)

    def test_dispatch_launchagent_runs_every_thirty_seconds(self) -> None:
        text = Path("scripts/ai.hermes.xiuxian.return-deadline-dispatch.plist").read_text(encoding="utf-8")
        self.assertIn("<integer>30</integer>", text)
        self.assertIn("return_deadline_dispatch.py", text)

    def test_all_sender_launchagents_poll_at_most_every_sixty_seconds(self) -> None:
        plists = sorted(Path("scripts").glob("ai.hermes.xiuxian.tg*.shendubiguan.send.plist"))
        plists += sorted(Path("scripts").glob("ai.hermes.xiuxian.tg*.yuanyingchuqiao.send.plist"))
        # Six deep-retreat senders and five yuanying senders remain active.
        self.assertEqual(11, len(plists))
        for path in plists:
            payload = plistlib.loads(path.read_bytes())
            self.assertLessEqual(payload["StartInterval"], 60, path.name)


if __name__ == "__main__":
    unittest.main()
