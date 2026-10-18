from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import nangongque_protocol as nq

CONFIG = Path(__file__).resolve().parent / "nangongque_config.json"


ACCOUNTS = (
    "tg9100000001",
    "tg9100000002",
    "tg9100000003",
    "tg9100000004",
    "tg9100000005",
    "tg9100000006",
)


class NangongqueProtocolTests(unittest.TestCase):
    def test_config_is_disabled_shadow_and_uses_canonical_six_business_accounts(self):
        config = nq.load_config(CONFIG)
        self.assertFalse(config["enabled"])
        self.assertEqual("shadow", config["mode"])
        self.assertEqual(ACCOUNTS, tuple(config["accounts"]))
        self.assertEqual("tg9100000007", config["credential_sources"]["tg9100000006"])
        self.assertNotIn("tg9100000007", config["accounts"])

    def test_entry_requires_nqb_token_and_allowed_bot(self):
        entry = nq.validate_entry(
            "https://t.me/hantianzun23_bot?startapp=nqb_TEST_1234",
            allowed_bot_patterns=(r"hantianzun\d+_bot",),
        )
        self.assertEqual("hantianzun23_bot", entry.bot_username)
        self.assertEqual("nqb_", entry.token[:4])
        for bad in (
            "https://t.me/hantianzun23_bot?startapp=qyz_TEST_1234",
            "https://t.me/evil_bot?startapp=nqb_TEST_1234",
            "https://example.com/hantianzun23_bot?startapp=nqb_TEST_1234",
        ):
            with self.subTest(bad=bad), self.assertRaises(nq.ProtocolError):
                nq.validate_entry(bad, allowed_bot_patterns=(r"hantianzun\d+_bot",))

    def test_start_state_input_claim_requests_are_strict_and_redacted(self):
        start = nq.build_request("start", init_data="SECRET_INIT", token="nqb_SECRET", player_id=7)
        state = nq.build_request("state", session_token="SECRET_SESSION", room_id="room-1", player_id=7)
        action = nq.build_request(
            "input",
            session_token="SECRET_SESSION",
            room_id="room-1",
            player_id=7,
            input_payload={"seq": 1, "action": "attack", "moveX": 0, "moveY": 1},
        )
        claim = nq.build_request("claim", session_token="SECRET_SESSION")
        self.assertEqual({"token", "initData", "playerId"}, set(start.payload))
        self.assertEqual({"sessionToken", "roomId", "playerId"}, set(state.payload))
        self.assertEqual({"sessionToken", "roomId", "playerId", "input"}, set(action.payload))
        self.assertEqual({"sessionToken"}, set(claim.payload))
        serialized = json.dumps([r.safe_summary() for r in (start, state, action, claim)])
        self.assertNotIn("SECRET_INIT", serialized)
        self.assertNotIn("SECRET_SESSION", serialized)
        self.assertNotIn("nqb_SECRET", serialized)

    def test_state_machine_requires_authoritative_reconciliation_between_mutations(self):
        machine = nq.NangongqueStateMachine(account="tg9100000001", player_id=7)
        machine.observe_start({"ok": True, "sessionToken": "s", "roomId": "r", "playerId": 7})
        self.assertEqual("needs_state", machine.phase)
        with self.assertRaises(nq.ProtocolError):
            machine.next_input()
        machine.observe_state({"ok": True, "room": {"id": "r", "status": "battle"}, "player": {"id": 7}})
        self.assertEqual("needs_decision", machine.phase)
        with self.assertRaises(nq.ProtocolError):
            machine.next_input()
        machine.set_decision({"seq": 1, "action": "dodge", "moveX": -1, "moveY": 0})
        request = machine.next_input()
        self.assertEqual("input_uncertain", machine.phase)
        self.assertEqual("dodge", request.payload["input"]["action"])
        with self.assertRaises(nq.ProtocolError):
            machine.next_input()
        machine.observe_state({"ok": True, "room": {"id": "r", "status": "settled"}, "player": {"id": 7}})
        self.assertEqual("needs_claim", machine.phase)
        machine.next_claim()
        self.assertEqual("claim_uncertain", machine.phase)
        machine.observe_claim({"ok": True, "claimed": True, "settlement": {"damage": 12}})
        self.assertEqual("complete", machine.phase)

    def test_terminal_error_and_identity_mismatch_fail_closed(self):
        machine = nq.NangongqueStateMachine(account="tg9100000006", player_id=9100000006)
        with self.assertRaises(nq.ProtocolError):
            machine.observe_start({"ok": True, "sessionToken": "s", "roomId": "r", "playerId": 9100000007})
        self.assertEqual("failed", machine.phase)
        machine = nq.NangongqueStateMachine(account="tg9100000001", player_id=7)
        machine.observe_error("nangongque_join_closed")
        self.assertEqual("failed", machine.phase)
        self.assertTrue(machine.terminal)

    def test_unknown_mechanic_never_generates_an_action(self):
        machine = nq.NangongqueStateMachine(account="tg9100000001", player_id=7)
        machine.observe_start({"ok": True, "sessionToken": "s", "roomId": "r", "playerId": 7})
        machine.observe_state({
            "ok": True,
            "room": {"id": "r", "status": "battle"},
            "player": {"id": 7},
            "mechanic": {"type": "unseen_server_mechanic", "payload": {"secret": "x"}},
        })
        self.assertEqual("blocked_unknown_mechanic", machine.phase)
        with self.assertRaises(nq.ProtocolError):
            machine.next_input()

    def test_disabled_coordinator_check_is_network_free_and_creates_no_account_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = nq.coordinator_check(CONFIG, state_root=root)
            self.assertEqual("disabled_shadow", result["status"])
            self.assertFalse(result["network_enabled"])
            self.assertEqual(list(ACCOUNTS), result["accounts"])
            self.assertFalse((root / "accounts").exists())

    def test_account_plans_are_unique_and_demo_player_06_state_is_not_attributed_to_credential_source(self):
        config = nq.load_config(CONFIG)
        plans = nq.build_account_plans(config, state_root=Path("/private/state"))
        self.assertEqual(6, len(plans))
        self.assertEqual(6, len({plan.state_dir for plan in plans}))
        demo_player_06 = next(plan for plan in plans if plan.account == "tg9100000006")
        self.assertEqual("tg9100000007", demo_player_06.credential_source)
        self.assertEqual(Path("/private/state/accounts/tg9100000006/nangongque_boss"), demo_player_06.state_dir)
        self.assertNotIn("tg9100000007", str(demo_player_06.state_dir))

    def test_admission_batch_uses_one_shared_deadline_and_rejects_expired_entry(self):
        config = nq.load_config(CONFIG)
        batch = nq.build_admission_batch(
            config,
            entry_url="https://t.me/hantianzun23_bot?startapp=nqb_TEST_1234",
            entry_ts=1000.0,
            now=1020.0,
            join_window_seconds=60.0,
            state_root=Path("/private/state"),
        )
        self.assertEqual(1060.0, batch.deadline_ts)
        self.assertEqual(6, len(batch.plans))
        self.assertEqual({1060.0}, {plan.deadline_ts for plan in batch.plans})
        with self.assertRaises(nq.ProtocolError):
            nq.build_admission_batch(
                config,
                entry_url="https://t.me/hantianzun23_bot?startapp=nqb_TEST_1234",
                entry_ts=1000.0,
                now=1060.0,
                join_window_seconds=60.0,
                state_root=Path("/private/state"),
            )

    def test_shadow_replay_writes_only_redacted_account_scoped_evidence(self):
        fixture = {
            "account": "tg9100000001",
            "player_id": 7,
            "events": [
                {"step": "start", "response": {"ok": True, "sessionToken": "SECRET", "roomId": "r", "playerId": 7}},
                {"step": "state", "response": {"ok": True, "room": {"id": "r", "status": "settled"}, "player": {"id": 7}}},
                {"step": "claim", "response": {"ok": True, "claimed": True, "settlement": {"damage": 1}}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = nq.replay_fixture(fixture, state_root=Path(tmp))
            output = Path(result["output"])
            self.assertEqual(Path(tmp) / "accounts" / "tg9100000001" / "nangongque_boss" / "shadow_replay.json", output)
            content = output.read_text(encoding="utf-8")
            self.assertNotIn("SECRET", content)
            self.assertNotIn("sessionToken", content)
            self.assertEqual("complete", json.loads(content)["phase"])
            self.assertEqual(0o600, output.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
