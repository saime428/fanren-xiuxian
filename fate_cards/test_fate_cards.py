"""Offline tests for 天机命脉 + 噬金虫 play; no game or Telegram access."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from miniapp_shared.dwelling import MiniAppError
from miniapp_shared.state import write_json
from miniapp_shared.summaries import beast_notification, fate_notification

from fate_cards import run_cycle
from fate_cards.protocol import COLS, ROWS, apply_move, board_full, choose_column
from fate_cards.runtime import (
    CANARY_ACCOUNT, decide_choice, fill_beast_runs, play_beast_run, read_policy, run_identity,
)

CHOICES = [{"key": "accept", "rewardTrace": 2}, {"key": "defy", "rewardTrace": 4}, {"key": "hide", "rewardTrace": 1}]
QUESTS = {
    "accept": {"metric": "cultivation_gain", "target": 30, "progress": 30, "canSettle": True, "status": "active"},
    "defy": {"metric": "beast_merge_runs", "target": 1, "progress": 0, "canSettle": False, "status": "active"},
    "hide": {"metric": "wait_seconds", "target": 180, "progress": 0, "canSettle": False, "status": "active"},
}


MORNING = 1790366400.0  # 2026-09-26 04:00 Asia/Shanghai, far from the 青元子 window
BEAST_LIMIT = 5


async def no_sleep(_seconds):
    return None


class FakeClient:
    """Server-shaped fate + beast endpoints with counters that tests can steer."""

    def __init__(self, *, drawn=None, runs_needed=1, beast_used=0, choose_uncertain=False, player_id=1001,
                 attempts_missing=False):
        self.player_id = player_id
        self.calls: list[str] = []
        self.record = drawn
        self.runs_needed, self.runs_done = runs_needed, 0
        self.beast_used = beast_used
        self.attempts_missing = attempts_missing
        self.choose_uncertain = choose_uncertain
        self.hide_waited = False
        self.run = None

    def _quest(self):
        quest = dict(self.record["quest"])
        if self.record["choiceKey"] == "defy":
            quest["progress"] = self.runs_done
            quest["canSettle"] = self.runs_done >= self.runs_needed
        if self.record["choiceKey"] == "hide" and self.hide_waited:
            quest.update(progress=180, canSettle=True)
        return quest

    def _view(self):
        if self.record is None:
            return None
        view = dict(self.record)
        if view.get("choiceKey"):
            view["quest"] = self._quest()
        return view

    async def fate(self, endpoint, **fields):
        self.calls.append("fate:" + endpoint)
        if endpoint == "start":
            return {"ok": True, "choices": CHOICES, "hasDrawn": self.record is not None, "record": self._view()}
        if endpoint == "draw":
            assert self.record is None
            self.record = {"questionKey": fields["questionKey"], "cards": [{}, {}, {}], "aiReading": {}}
            return {"ok": True, "record": self._view()}
        if endpoint == "interpret":
            self.record["aiReading"] = {"overview": "fixture"}
            return {"ok": True, "record": self._view()}
        if endpoint == "choose":
            assert not self.record.get("choiceKey"), "a choice must never be sent twice"
            self.record.update(choiceKey=fields["choiceKey"], quest=dict(QUESTS[fields["choiceKey"]]))
            if self.choose_uncertain:
                raise MiniAppError("request_failed", uncertain=True)
            return {"ok": True, "record": self._view()}
        if endpoint == "settle":
            quest = self._quest()
            if not quest["canSettle"]:
                raise MiniAppError("quest_incomplete")
            self.record["quest"] = {**quest, "status": "settled"}
            reward = {"accept": 2, "defy": 4, "hide": 1}[self.record["choiceKey"]]
            return {"ok": True, "record": dict(self.record), "reward": {"tianjiTrace": reward}}
        raise AssertionError(endpoint)

    async def beast(self, endpoint, **fields):
        self.calls.append("beast:" + endpoint)
        if endpoint == "start":
            if self.attempts_missing:
                return {"ok": True}
            return {"ok": True, "attempts": {"limit": BEAST_LIMIT, "used": self.beast_used}}
        if endpoint == "run/start":
            if self.beast_used >= BEAST_LIMIT:
                raise MiniAppError("beast_attempts_exhausted")
            self.beast_used += 1
            self.run = {"board": [0] * (COLS * ROWS), "piece": 1, "seq": 0}
            return {"ok": True, "runToken": "run-fixture", "board": self.run["board"], "nextPiece": 1, "seq": 0}
        if endpoint == "move":
            assert fields["seq"] == self.run["seq"]
            outcome = apply_move(self.run["board"], fields["column"], self.run["piece"])
            self.run.update(board=outcome["board"], seq=self.run["seq"] + 1, piece=2 if self.run["seq"] % 5 == 4 else 1)
            over = board_full(self.run["board"]) or self.run["seq"] >= 20
            return {"ok": True, "accepted": True, "board": self.run["board"], "nextPiece": self.run["piece"],
                    "seq": self.run["seq"], "gameOver": over}
        if endpoint == "submit":
            self.runs_done += 1
            return {"ok": True, "verified": {"score": 100 * self.run["seq"], "maxTier": 4}, "reward": {"tianjiTrace": 3}}
        raise AssertionError(endpoint)


def drawn(choice=None, settled=False):
    record = {"questionKey": "wealth", "cards": [{}, {}, {}], "aiReading": {"overview": "fixture"}}
    if choice:
        quest = dict(QUESTS[choice], status="settled" if settled else "active")
        record.update(choiceKey=choice, quest=quest)
    return record


class BoardTests(unittest.TestCase):
    def test_merge_chain_matches_the_official_rules(self):
        board = [0] * (COLS * ROWS)
        board[(ROWS - 1) * COLS + 0] = 1
        outcome = apply_move(board, 1, 1)
        # Two tier-1 pieces side by side become one tier-2 piece: 10 * 2**1 points.
        self.assertEqual((1, 20), (outcome["merges"], outcome["gained"]))
        self.assertEqual(2, max(outcome["board"]))

    def test_greedy_prefers_a_merge_and_never_a_full_column(self):
        board = [0] * (COLS * ROWS)
        board[(ROWS - 1) * COLS + 3] = 1
        self.assertIn(choose_column(board, 1), {2, 3, 4})
        for row in range(ROWS):
            board[row * COLS + 0] = 9
        self.assertNotEqual(0, choose_column(board, 1))


class DecisionTests(unittest.TestCase):
    rewards = {"accept": 2, "defy": 4, "hide": 1}

    def test_canary_only_until_defy_is_verified(self):
        canary = {"defy": "canary", "canary_account": CANARY_ACCOUNT}
        self.assertEqual("defy", decide_choice(CANARY_ACCOUNT, canary, self.rewards, 5))
        self.assertEqual("accept", decide_choice("tg9100000002", canary, self.rewards, 5))
        self.assertEqual("defy", decide_choice("tg9100000002", {"defy": "verified"}, self.rewards, 5))
        self.assertEqual("accept", decide_choice(CANARY_ACCOUNT, {"defy": "failed"}, self.rewards, 5))
        # No 噬金虫 run left today: never take a quest that cannot be finished.
        self.assertEqual("accept", decide_choice("tg9100000002", {"defy": "verified"}, self.rewards, 0))


class RunIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def run_identity(self, account, client):
        return asyncio.run(run_identity(self.root, account, client, now=lambda: MORNING, sleep=no_sleep))

    def test_manual_accept_that_is_complete_is_settled_without_choosing(self):
        client = FakeClient(drawn=drawn("accept"))
        result = self.run_identity("tg9100000002", client)
        self.assertEqual(("settled", 2), (result["status"], result["reward"]["tianjiTrace"]))
        self.assertNotIn("fate:choose", client.calls)
        self.assertNotIn("fate:draw", client.calls)

    def test_settled_day_is_read_only(self):
        client = FakeClient(drawn=drawn("accept", settled=True))
        self.assertEqual("already_settled", self.run_identity("tg9100000002", client)["status"])
        self.assertEqual(["fate:start"], client.calls)

    def test_fresh_day_on_a_normal_account_draws_interprets_and_accepts(self):
        client = FakeClient()
        result = self.run_identity("tg9100000002", client)
        self.assertEqual(("accept", "settled"), (result["choice"], result["status"]))
        self.assertEqual(["fate:start", "fate:draw", "fate:interpret", "beast:start", "fate:choose", "fate:settle"],
                         client.calls)

    def test_canary_defy_plays_one_run_settles_and_promotes_defy_for_everyone(self):
        client = FakeClient()
        result = self.run_identity(CANARY_ACCOUNT, client)
        self.assertEqual(("defy", "settled", 4), (result["choice"], result["status"], result["reward"]["tianjiTrace"]))
        self.assertEqual(1, len(result["beast_runs"]))
        self.assertEqual(1, client.calls.count("beast:submit"))
        self.assertEqual("verified", read_policy(self.root)["defy"])
        self.assertEqual("defy", self.run_identity("tg9100000002", FakeClient())["choice"])

    def test_defy_uses_every_run_left_before_it_demotes_the_policy(self):
        client = FakeClient(runs_needed=99)
        result = self.run_identity(CANARY_ACCOUNT, client)
        self.assertEqual("defy_unmet", result["status"])
        self.assertEqual(BEAST_LIMIT, client.calls.count("beast:submit"))
        self.assertNotIn("fate:settle", client.calls)
        policy = read_policy(self.root)
        self.assertEqual("failed", policy["defy"])
        self.assertEqual((CANARY_ACCOUNT, BEAST_LIMIT), (policy["evidence"]["account"], len(policy["evidence"]["runs"])))
        # A later tick the same day has no run left and keeps the evidence.
        again = self.run_identity(CANARY_ACCOUNT, client)
        self.assertEqual(BEAST_LIMIT, client.calls.count("beast:submit"))
        self.assertEqual("defy_unmet", again["status"])
        self.assertEqual(BEAST_LIMIT, len(read_policy(self.root)["evidence"]["runs"]))
        self.assertEqual("accept", self.run_identity("tg9100000002", FakeClient())["choice"])

    def test_defy_quest_met_on_a_later_run_still_settles(self):
        client = FakeClient(runs_needed=3)
        result = self.run_identity(CANARY_ACCOUNT, client)
        self.assertEqual(("settled", 3), (result["status"], len(result["beast_runs"])))
        self.assertEqual("verified", read_policy(self.root)["defy"])

    def test_uncertain_choose_is_reconciled_from_the_server_never_resent(self):
        client = FakeClient(choose_uncertain=True)
        result = self.run_identity("tg9100000002", client)
        self.assertEqual(1, client.calls.count("fate:choose"))
        self.assertEqual(("accept", "settled"), (result["choice"], result["status"]))

    def test_hide_waits_out_its_three_minutes_before_settling(self):
        client = FakeClient(drawn=drawn("hide"))
        waits = []

        async def sleep(seconds):
            waits.append(seconds)
            client.hide_waited = True
        result = asyncio.run(run_identity(self.root, "tg9100000002", client, sleep=sleep))
        self.assertEqual("settled", result["status"])
        self.assertGreaterEqual(waits[0], 180)

    def test_beast_run_follows_server_seq_and_submits_once(self):
        client = FakeClient()
        asyncio.run(client.beast("start"))
        run = asyncio.run(play_beast_run(client, sleep=no_sleep))
        self.assertEqual(20, run["moves"])
        self.assertEqual(1, client.calls.count("beast:submit"))

    def test_lost_move_and_submit_replies_are_resent_as_the_same_request(self):
        client = FakeClient()
        original = client.beast
        lost = {"move": 1, "submit": 2}

        async def flaky(endpoint, **fields):
            if lost.get(endpoint):
                lost[endpoint] -= 1
                client.calls.append("lost:" + endpoint)
                raise MiniAppError("request_failed", uncertain=True)
            return await original(endpoint, **fields)
        client.beast = flaky
        run = asyncio.run(play_beast_run(client, sleep=no_sleep))
        self.assertEqual((20, 2000), (run["moves"], run["score"]))
        self.assertEqual((1, 1, 2), (client.calls.count("beast:run/start"), client.calls.count("beast:submit"),
                                     client.calls.count("lost:submit")))

    def test_a_certain_rejection_is_never_resent(self):
        client = FakeClient()
        original = client.beast

        async def rejected(endpoint, **fields):
            if endpoint == "submit":
                client.calls.append("beast:submit")
                raise MiniAppError("run_too_fast")
            return await original(endpoint, **fields)
        client.beast = rejected
        with self.assertRaises(MiniAppError):
            asyncio.run(play_beast_run(client, sleep=no_sleep))
        self.assertEqual(1, client.calls.count("beast:submit"))


class BeastFillTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def fill(self, client, ts=MORNING):
        return asyncio.run(fill_beast_runs(self.root, "tg9100000002", client, now=lambda: ts, sleep=no_sleep))

    def test_plays_exactly_the_runs_the_server_has_left_and_logs_them(self):
        client = FakeClient(beast_used=1)  # one run already played by hand
        beast = self.fill(client)
        self.assertEqual(("full", BEAST_LIMIT, BEAST_LIMIT), (beast["status"], beast["used"], beast["limit"]))
        self.assertEqual(4, client.calls.count("beast:submit"))
        self.assertEqual(({"fill"}, 4), ({run["purpose"] for run in beast["runs"]}, len(beast["runs"])))
        # A later tick reads the counter, plays nothing and keeps today's log.
        again = self.fill(client)
        self.assertEqual(("full", 4), (again["status"], len(again["runs"])))
        self.assertEqual(4, client.calls.count("beast:submit"))

    def test_never_starts_a_run_that_could_reach_the_qyz_window(self):
        client = FakeClient()
        near = 1790400660.0  # 13:31: a five-minute run could still be going at 13:36
        beast = self.fill(client, ts=near)
        self.assertEqual(("yielding_to_qyz", 0), (beast["status"], beast["used"]))
        self.assertNotIn("beast:run/start", client.calls)
        self.assertEqual("full", self.fill(client, ts=near + 22 * 60)["status"])  # 13:53

    def test_missing_attempts_counter_is_not_counted_as_full(self):
        client = FakeClient(attempts_missing=True)
        self.assertEqual("unavailable", self.fill(client)["status"])
        self.assertNotIn("beast:run/start", client.calls)


class BeastBoardShapeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_a_changed_board_shape_spends_one_run_and_halts_the_day(self):
        client = FakeClient()
        original = client.beast

        async def wide_board(endpoint, **fields):
            reply = await original(endpoint, **fields)
            if endpoint == "run/start":
                reply["board"] = [0] * 48  # e.g. a 6x8 board after a game update
            return reply
        client.beast = wide_board
        fill = asyncio.run(fill_beast_runs(self.root, "tg9100000002", client, now=lambda: MORNING, sleep=no_sleep))
        self.assertEqual(("halted", 1), (fill["status"], client.calls.count("beast:run/start")))
        self.assertNotIn("beast:move", client.calls)
        self.assertEqual("beast_board_invalid", fill["runs"][0]["halted"])
        # Later ticks the same day start nothing more.
        again = asyncio.run(fill_beast_runs(self.root, "tg9100000002", client, now=lambda: MORNING + 900, sleep=no_sleep))
        self.assertEqual(("halted", 1), (again["status"], client.calls.count("beast:run/start")))


class RunAccountTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.client = FakeClient()

        @asynccontextmanager
        async def bridge_factory(_root, account):
            yield SimpleNamespace(account=account)
        self.bridge_factory = bridge_factory
        self.patches = []
        for name, value in {
            "verify_username": self._async_none,
            "open_dwelling": self._dwelling,
            "FateIdentityClient": lambda transport, dwelling, player_id: self.client,
        }.items():
            original = getattr(run_cycle, name)
            setattr(run_cycle, name, value)
            self.patches.append((name, original))

    def tearDown(self):
        for name, original in self.patches:
            setattr(run_cycle, name, original)

    @staticmethod
    async def _async_none(*_args, **_kwargs):
        return None

    @staticmethod
    async def _dwelling(*_args, **_kwargs):
        return SimpleNamespace(identities=[SimpleNamespace(player_id=1001, label="主号")])

    def run_account(self, account, ts, **options):
        return asyncio.run(run_cycle.run_account(self.root, account, now=ts, bridge_factory=self.bridge_factory,
                                                 transport=object(), sleep=no_sleep, clock=lambda: ts, **options))

    def test_waits_for_the_0300_slot_then_completes_and_stays_complete(self):
        before = 1790362700.0  # 2026-09-26 02:58 Asia/Shanghai
        self.assertEqual("waiting_admission", self.run_account("tg9100000001", before)["status"])
        self.assertEqual([], self.client.calls)
        done = self.run_account("tg9100000001", before + 300)
        self.assertEqual(("daily_complete", "full"), (done["status"], done["beast_status"]))
        # The canary's defy quest took one run; the other four were filled after it.
        beast = done["identities"][0]["beast"]
        self.assertEqual((5, ["defy", "fill", "fill", "fill", "fill"]),
                         (beast["used"], [run["purpose"] for run in beast["runs"]]))
        calls = len(self.client.calls)
        self.assertEqual("daily_already_complete", self.run_account("tg9100000001", before + 900)["status"])
        self.assertEqual(calls, len(self.client.calls))

    def test_day_completed_before_the_fill_existed_plays_the_runs_left(self):
        # 2026-09-26: 天机命脉 had settled everywhere before 噬金虫 runs were automated.
        write_json(self.root / "state/accounts/tg9100000002/fate_cards/summary.json",
                   {"account": "tg9100000002", "day": "2026-09-26", "status": "daily_complete", "identities": []})
        self.client = FakeClient(drawn=drawn("accept", settled=True), beast_used=1)
        result = self.run_account("tg9100000002", MORNING)
        self.assertEqual(("daily_complete", "full"), (result["status"], result["beast_status"]))
        self.assertEqual(4, self.client.calls.count("beast:submit"))
        self.assertEqual("daily_already_complete", self.run_account("tg9100000002", MORNING + 300)["status"])

    def test_runs_wait_for_the_choice_until_late_evening(self):
        async def fate_down(_endpoint, **_fields):
            raise MiniAppError("request_failed")
        self.client.fate = fate_down
        morning = self.run_account("tg9100000002", MORNING)
        self.assertEqual(("pending", "pending"), (morning["status"], morning["beast_status"]))
        self.assertNotIn("beast:run/start", self.client.calls)
        late = self.run_account("tg9100000002", MORNING + 17.5 * 3600, force=True)  # 21:30
        self.assertEqual("full", late["beast_status"])
        self.assertEqual(BEAST_LIMIT, self.client.calls.count("beast:submit"))

    def test_failed_fill_is_retried_on_the_pending_timer(self):
        self.client = FakeClient(drawn=drawn("accept", settled=True))
        original = self.client.beast

        async def submit_down(endpoint, **fields):
            if endpoint == "submit":
                raise MiniAppError("request_failed", uncertain=True)
            return await original(endpoint, **fields)
        self.client.beast = submit_down
        result = self.run_account("tg9100000002", MORNING)
        self.assertEqual(("daily_complete", "pending"), (result["status"], result["beast_status"]))
        self.assertEqual("failed", result["identities"][0]["beast"]["status"])
        self.assertEqual(MORNING + run_cycle.PENDING_RETRY_SECONDS, result["next_due_at"])
        calls = len(self.client.calls)
        # Before the retry timer the fate day still reads as done, the runs as pending.
        waiting = self.run_account("tg9100000002", MORNING + 60)
        self.assertEqual(("daily_already_complete", "pending"), (waiting["status"], waiting["beast_status"]))
        self.assertEqual(calls, len(self.client.calls))
        summary = {"day": "2026-09-26", "accounts": [waiting]}
        self.assertEqual("2026-09-26:complete", fate_notification(summary, now_ts=1790437800)[0])  # 23:50
        self.assertEqual("2026-09-26:beast:partial", beast_notification(summary, now_ts=1790437800)[0])
        self.client.beast = original
        retried = self.run_account("tg9100000002", MORNING + run_cycle.PENDING_RETRY_SECONDS)
        self.assertEqual(("daily_complete", "full"), (retried["status"], retried["beast_status"]))

    def test_account_offsets_follow_the_timetable(self):
        slot = 1790362800.0  # 03:00
        self.assertTrue(run_cycle.admitted("tg9100000001", slot))
        self.assertFalse(run_cycle.admitted("tg9100000006", slot + 14 * 60))
        self.assertTrue(run_cycle.admitted("tg9100000006", slot + 15 * 60))


class NotificationTests(unittest.TestCase):
    def test_complete_summary_lists_choice_reward_and_runs(self):
        summary = {"day": "2026-09-27", "accounts": [
            {"account": "tg9100000001", "status": "daily_complete", "identities": [
                {"identity": "示例修士一", "status": "settled", "choice": "defy",
                 "quest": {"progress": 1, "target": 1, "unit": "局"}, "reward": {"tianjiTrace": 4},
                 "beast_runs": [{"score": 11525}]}]},
            {"account": "tg9100000002", "status": "daily_already_complete", "identities": [
                {"identity": "主号", "status": "already_settled", "choice": "accept", "quest": {}, "reward": {}}]},
        ]}
        key, text = fate_notification(summary, now_ts=1790400000)
        self.assertTrue(key.endswith(":complete"))
        self.assertIn("逆势改命，已验命，1/1局，天机残痕 +4，噬金虫 11525分", text)
        self.assertIn("全部账号完成 2/2", text)

    def test_beast_summary_once_every_account_has_played_every_run(self):
        summary = {"day": "2026-09-26", "accounts": [
            {"account": "tg9100000001", "status": "daily_complete", "beast_status": "full", "identities": [
                {"identity": "示例修士一", "beast": {"status": "full", "used": 5, "limit": 5, "runs": [
                    {"score": 11525, "max_tier": 8, "trace": 3}, {"score": 9800, "max_tier": 7, "trace": 3}]}}]},
            {"account": "tg9100000002", "status": "scheduled_wait", "beast_status": "full", "identities": [
                {"identity": "锋承玄", "beast": {"status": "full", "used": 5, "limit": 5, "runs": []}}]},
        ]}
        key, text = beast_notification(summary, now_ts=1790400000)
        self.assertEqual("2026-09-26:beast:complete", key)
        self.assertIn("【噬金虫进化】2026-09-26 全部账号打满 2/2", text)
        self.assertIn("@demo_player_01 示例修士一: 5/5 局，今日自动 2 局 11525/9800 分，最高 8 阶，天机残痕 +6", text)
        self.assertIn("@demo_player_02 锋承玄: 5/5 局", text)
        summary["accounts"][1]["beast_status"] = "pending"
        summary["accounts"][1]["identities"][0]["beast"].update(status="failed", used=3, error="request_failed")
        self.assertIsNone(beast_notification(summary, now_ts=1790400000))
        key, text = beast_notification(summary, now_ts=1790437800)  # 23:50
        self.assertEqual("2026-09-26:beast:partial", key)
        self.assertIn("@demo_player_02 锋承玄: 3/5 局，状态 failed request_failed", text)

    def test_nothing_before_day_end_while_incomplete(self):
        summary = {"day": "2026-09-27", "accounts": [{"account": "tg9100000001", "status": "pending"}]}
        self.assertIsNone(fate_notification(summary, now_ts=1790400000))


if __name__ == "__main__":
    unittest.main()
