import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class DoufaDailyQuotaTests(unittest.TestCase):
    def test_anti_repeat_reply_is_a_distinct_terminal_pause_outcome(self):
        from reply_parsing import AckPolicy, classify_reply_messages
        from types import SimpleNamespace

        from datetime import datetime, timezone
        sent = SimpleNamespace(id=100, sender_id=9100000005, message=".斗法 demo_player_02", reply_to_msg_id=7310786, reply_to=None, date=datetime.now(timezone.utc))
        reply = SimpleNamespace(
            id=101,
            sender_id=8944702077,
            message="天道有则！你与 @demo_player_02 在24小时内已交锋过多，暂不可再次斗法！",
            reply_to_msg_id=100,
            reply_to=None,
            date=datetime.now(timezone.utc),
        )
        outcome = classify_reply_messages(
            [sent, reply],
            topic_id=7310786,
            own_sender_id=9100000005,
            target_message=".斗法 demo_player_02",
            sent_message_id=100,
            reset_triggers=[],
            cooldown_markers=[],
            policy=AckPolicy(reply_bot_id=8944702077),
        )
        self.assertTrue(outcome.acked)
        self.assertTrue(outcome.duel_anti_repeat)
        self.assertEqual(101, outcome.ack_reply_message_id)

    def test_runner_persists_account_pause_after_anti_repeat(self):
        text = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        self.assertIn('duel_anti_repeat', text)
        self.assertIn('"mode": "manual_resume_required"', text)
        self.assertIn('DOUFA_PAUSE_PATH', text)
        self.assertIn('quota unchanged after anti-repeat', text)
        self.assertIn('anti_repeat_paused', text)

    def test_reconciler_requires_exact_direct_reply_to_the_sent_message(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from scripts.reconcile_doufa_anti_repeat import anti_repeat_reply_for_sent

        direct = SimpleNamespace(
            id=201,
            message="天道有则！你与 @demo_player_02 在24小时内已交锋过多，暂不可再次斗法！",
            reply_to_msg_id=100,
            reply_to=None,
            date=datetime.now(timezone.utc),
        )
        unrelated = SimpleNamespace(
            id=202,
            message=direct.message,
            reply_to_msg_id=99,
            reply_to=None,
            date=datetime.now(timezone.utc),
        )
        self.assertIs(direct, anti_repeat_reply_for_sent([unrelated, direct], 100))
        self.assertIsNone(anti_repeat_reply_for_sent([unrelated], 100))

    def test_quota_artifact_is_account_and_flow_scoped(self):
        for account in ("tg9100000005", "tg9100000003", "tg9100000004", "tg9100000002", "tg9100000006"):
            path = Path("state/accounts") / account / "doufa_demo_player_02" / "daily_quota.json"
            self.assertEqual(account, path.parts[-3])

    def test_daily_limit_and_interval_are_explicit_in_wrappers(self):
        for account in ("tg9100000005", "tg9100000003", "tg9100000004", "tg9100000002", "tg9100000006"):
            text = Path(f"scripts/send_doufa_{account}.sh").read_text(encoding="utf-8")
            self.assertIn("DOUFA_DAILY_LIMIT='5'", Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8"))
            runner = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
            self.assertIn("TG_MIN_REPEAT_SECONDS='300'", runner)
            self.assertIn("DOUFA_INTERVAL_SECONDS='300'", runner)
            self.assertIn("TG_MESSAGE='.斗法 demo_player_01'", text)
            self.assertIn("XIU_FLOW_SLUG='doufa_demo_player_02'", text)

    def test_near_guard_schedule_collision_is_deferred_not_dropped(self):
        text = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        self.assertIn("DOUFA_DEFER_MAX_SECONDS='90'", text)
        self.assertIn("斗法 automatic defer", text)
        self.assertIn('sleep "${remaining_seconds}"', text)

    def test_dynamic_rotation_uses_global_five_minute_gap_not_wall_clock_slots(self):
        quota_text = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertNotIn("rotation_slot", quota_text)
        self.assertIn("DOUFA_GLOBAL_INTERVAL_SECONDS='300'", dispatcher)
        self.assertIn("last_actual_send_ts", dispatcher)
        self.assertIn("next_index", dispatcher)
        self.assertIn("doufa_rotation_select.py", dispatcher)
        selector = Path("scripts/doufa_rotation_select.py").read_text(encoding="utf-8")
        self.assertIn("sent < limit", selector)
        self.assertIn('account == last_account', selector)

    def test_selector_never_chooses_the_last_sender_consecutively(self):
        selector = Path("scripts/doufa_rotation_select.py").read_text(encoding="utf-8")
        self.assertIn('last_account = str(state.get("last_account") or "")', selector)
        self.assertIn('if account == last_account:', selector)

    def test_selector_uses_global_latest_account_attempt_even_when_rotation_state_is_stale(self):
        accounts = ("a", "b", "c", "d", "e")
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            root = Path(tmp)
            state_path = root / "rotation.json"
            stale_ts = time.time() - 3600
            state_path.write_text(json.dumps({
                "last_actual_send_ts": stale_ts,
                "last_account": "a",
                "next_index": 0,
            }), encoding="utf-8")
            latest_ts = time.time() - 10
            for account in accounts:
                base = root / "state" / "accounts" / account / "doufa_demo_player_02"
                base.mkdir(parents=True)
                (base / "daily_quota.json").write_text(json.dumps({
                    "date": __import__("datetime").datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).date().isoformat(),
                    "sent_count": 0,
                    "limit": 5,
                }), encoding="utf-8")
            latest_attempt = root / "state" / "accounts" / "c" / "doufa_demo_player_02" / "last_attempt.json"
            latest_attempt.write_text(json.dumps({"actual_send_ts": latest_ts}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path.cwd() / "scripts/doufa_rotation_select.py"), str(state_path), "300", *accounts],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            selection = json.loads(completed.stdout)
            self.assertEqual("wait", selection["action"])
            self.assertGreaterEqual(selection["remaining"], 285)

    def test_campaign_stops_when_its_shanghai_day_ends(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state/accounts/a/doufa_demo_player_02").mkdir(parents=True)

            def select(campaign_date):
                completed = subprocess.run(
                    [sys.executable, str(Path.cwd() / "scripts/doufa_rotation_select.py"),
                     str(root / "rotation.json"), "300", "a"],
                    cwd=root, check=True, capture_output=True, text=True,
                    env={**os.environ, "DOUFA_CAMPAIGN_DATE": campaign_date},
                )
                return json.loads(completed.stdout)

            self.assertEqual({"action": "send", "account": "a", "next_index": 0}, select(today.isoformat()))
            stale = select((today - timedelta(days=1)).isoformat())
            self.assertEqual("exhausted", stale["action"])
            self.assertIn("ended", stale["reason"])
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertIn("export DOUFA_CAMPAIGN_DATE", dispatcher)

    def test_locks_are_kernel_released_and_one_failure_does_not_cascade(self):
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        runner = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        # flock on an fd the shell holds: no mkdir lock to go stale, no EXIT
        # trap that a timed-out waiter could run against another run's lock.
        self.assertIn('exec 9>>"$DOUFA_ROTATION_LOCK_PATH"', dispatcher)
        self.assertIn('exec 8>>"${DOUFA_QUOTA_LOCK}"', runner)
        for text in (dispatcher, runner):
            self.assertNotIn("mkdir \"$DOUFA", text)
            self.assertNotIn("mkdir \"${DOUFA", text)
            self.assertNotIn("trap ", text)
        # A failed account send continues the campaign; quota and pause are
        # persisted independently, both with the project interpreter.
        self.assertIn('bash "./scripts/send_doufa_${account}.sh" || send_rc=$?', dispatcher)
        self.assertIn(')" || quota_rc=$?', runner)
        self.assertIn("<<'PY' || pause_rc=$?", runner)
        self.assertNotRegex(runner, r"(?m)(^|[\s(])python3 ")

    def test_doufa_rotation_is_manual_activation_only(self):
        text = Path("scripts/ai.hermes.xiuxian.doufa_demo_player_02.rotation.plist").read_text(encoding="utf-8")
        self.assertIn("run_doufa_dynamic_rotation.sh", text)
        self.assertNotIn("<key>StartInterval</key>", text)
        self.assertNotIn("<key>StartCalendarInterval</key>", text)
        self.assertIn("<key>RunAtLoad</key>", text)
        self.assertIn("<false/>", text)

    def test_manual_activation_runs_campaign_until_all_accounts_are_done(self):
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertIn("while true; do", dispatcher)
        self.assertIn('sleep "$remaining"', dispatcher)
        self.assertIn("斗法 campaign complete", dispatcher)
        self.assertIn("斗法 campaign skips paused account after 24-hour anti-repeat", dispatcher)
        self.assertNotIn("stopped by 24-hour anti-repeat", dispatcher)
        self.assertNotIn('exit 0\n      fi', dispatcher)

    def test_manual_activation_clears_only_under_quota_account_pauses(self):
        text = Path("scripts/activate_doufa_demo_player_02.sh").read_text(encoding="utf-8")
        self.assertIn("clear_doufa_manual_pauses.py", text)
        self.assertIn("launchctl enable", text)
        self.assertIn("launchctl bootstrap", text)
        self.assertIn("launchctl kickstart", text)

    def test_dynamic_rotation_includes_demo_player_02_sender(self):
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertIn("tg9100000002", dispatcher)
        wrapper = Path("scripts/send_doufa_tg9100000002.sh").read_text(encoding="utf-8")
        self.assertIn("config/accounts/tg9100000002.env", wrapper)
        self.assertIn("TG_MESSAGE='.斗法 demo_player_01'", wrapper)

    def test_dynamic_rotation_includes_demo_player_06_sender_with_alias_session(self):
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertIn(
            "accounts=(tg9100000005 tg9100000003 tg9100000004 tg9100000002 tg9100000006)",
            dispatcher,
        )
        wrapper = Path("scripts/send_doufa_tg9100000006.sh").read_text(encoding="utf-8")
        self.assertIn("config/accounts/tg9100000007.env", wrapper)
        self.assertIn("XIU_ACCOUNT_SLUG='tg9100000006'", wrapper)
        self.assertIn("TG_MESSAGE='.斗法 demo_player_01'", wrapper)

    def test_legacy_per_account_doufa_schedulers_are_absent(self):
        for account in ("tg9100000005", "tg9100000003", "tg9100000004", "tg9100000002", "tg9100000006"):
            self.assertFalse(Path(f"scripts/ai.hermes.xiuxian.{account}.doufa_demo_player_02.send.plist").exists())

    def test_dispatch_guard_advances_even_when_sender_state_does_not(self):
        dispatcher = Path("scripts/run_doufa_dynamic_rotation.sh").read_text(encoding="utf-8")
        self.assertIn("recorded = after if after > before else time.time()", dispatcher)
        self.assertIn("sender_state_advanced", dispatcher)

    def test_quota_reset_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "daily_quota.json"
            path.write_text(json.dumps({"date": "2000-01-01", "sent_count": 10}), encoding="utf-8")
            self.assertNotEqual(json.loads(path.read_text())["date"], "2026-08-03")

    def test_no_ack_is_still_a_confirmed_transport_send(self):
        text = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        self.assertIn("今日神念", text)
        self.assertIn("daily_mind_remaining", text)
        self.assertIn("export TG_REQUIRE_ACK_REPLY='1'", text)
        self.assertIn("no immediate resend", text)

    def test_acknowledged_duel_outcome_consumes_one_local_quota_when_daily_mind_is_absent(self):
        # A direct, account-attributed duel outcome is the fallback receipt when
        # the game omits 今日神念.  Without it, successful duels stayed at 0/5.
        text = Path("scripts/lib/run_doufa_daily_quota.sh").read_text(encoding="utf-8")
        self.assertIn("apply_receipt", text)
        self.assertIn('quota["sent_count"] = min(limit, current + 1)', Path("doufa_receipts.py").read_text())

    def test_daily_mind_reply_is_authoritative(self):
        from reply_parsing import parse_daily_mind
        self.assertEqual(parse_daily_mind("今日神念：9/10"), (9, 10))
        self.assertEqual(parse_daily_mind("今日神念: 0 / 10"), (0, 10))
        self.assertIsNone(parse_daily_mind("今日神念：11/10"))

    def test_daily_mind_reply_qualifies_without_bot_id(self):
        from reply_parsing import AckPolicy, classify_reply_messages
        from datetime import datetime, timezone

        class Msg:
            def __init__(self, mid, sender_id, text, reply_to_msg_id=None):
                self.id = mid
                self.sender_id = sender_id
                self.message = text
                self.reply_to_msg_id = reply_to_msg_id
                self.reply_to = None
                self.date = datetime.now(timezone.utc)

        own = Msg(100, 111, ".斗法 @demo_player_02")
        reply = Msg(101, 8917921351, "🏁终局\n🧠 今日神念：9/10", reply_to_msg_id=100)
        outcome = classify_reply_messages(
            [own, reply], topic_id=7310786, own_sender_id=111,
            target_message=".斗法 @demo_player_02", sent_message_id=100,
            reset_triggers=[], cooldown_markers=[], policy=AckPolicy(), log=lambda *args: None,
        )
        self.assertTrue(outcome.acked)
        self.assertEqual((outcome.daily_mind_remaining, outcome.daily_mind_limit), (9, 10))

    def test_duel_escape_reply_is_a_qualifying_reply_for_the_sending_account(self):
        from reply_parsing import AckPolicy, classify_reply_messages
        from datetime import datetime, timezone

        class Msg:
            def __init__(self, mid, sender_id, text, reply_to_msg_id=None):
                self.id = mid
                self.sender_id = sender_id
                self.message = text
                self.reply_to_msg_id = reply_to_msg_id
                self.reply_to = None
                self.date = datetime.now(timezone.utc)

        own = Msg(840987, 9100000003, ".斗法 demo_player_02")
        reply = Msg(
            840988,
            8816935632,
            "面对境界压制，@demo_player_03 凭借神通侥幸逃脱！(成功率: 49%)",
            reply_to_msg_id=840987,
        )
        outcome = classify_reply_messages(
            [own, reply], topic_id=7310786, own_sender_id=9100000003,
            target_message=".斗法 demo_player_02", sent_message_id=840987,
            reset_triggers=[], cooldown_markers=[], policy=AckPolicy(), log=lambda *args: None,
        )
        self.assertTrue(outcome.acked)
        self.assertEqual(outcome.ack_reply_message_id, 840988)
        self.assertIn(840987, outcome.matched_ids)



if __name__ == "__main__":
    unittest.main()
