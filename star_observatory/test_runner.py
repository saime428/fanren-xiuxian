import tempfile
import unittest
from pathlib import Path

from miniapp_shared.dwelling import DWELLING_EXTERNAL, DWELLING_START, JsonTransport
from star_observatory.adapter import ALLOWED_PATHS
from star_observatory.runner import run_account
from star_observatory.state import read_account_state, write_account_state


class _Me:
    username = "demo_player_04"


class _Client:
    async def get_me(self):
        return _Me()


class _EntryProtocol:
    async def _pinned_cave_entry(self, _client):
        return {"token": "df_PARENT"}

    async def _cave_webview(self, _client, _entry):
        return "query_id=TEST&hash=SECRET"


class _Bridge:
    account = "tg9100000004"
    client = _Client()
    _entry_protocol = _EntryProtocol()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _transport(board, mutations):
    def post(path, payload, *, mutation):
        if path == DWELLING_START:
            return {"identity": {"choices": [{"playerId": 9100000004, "daoName": "竹衍衡", "source": "personal"}]}}
        if path == DWELLING_EXTERNAL:
            return {"url": "/miniapp/xianxia-sect-farm?startapp=farm_CHILD"}
        if mutation:
            mutations.append(payload["action"])
            raise AssertionError("uncertain mutation must not be replayed")
        return {"domain": {"mode": "stars", "sectName": "星宫", "plots": board}}

    return JsonTransport(ALLOWED_PATHS, post=post)


class StarObservatoryRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_does_not_authenticate_before_server_due_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_account_state(root, "tg9100000004", {"status": "wait", "next_due_at": 200.0})

            def forbidden(_root, _account):
                raise AssertionError("bridge must not be opened before due")

            result = await run_account(root, "tg9100000004", now=100.0, bridge_factory=forbidden,
                                       transport=object())

        self.assertEqual(("scheduled_wait", 200.0), (result["status"], result["next_due_at"]))

    async def test_due_account_persists_server_timer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = [{"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 120}]
            result = await run_account(root, "tg9100000004", now=1000.0, bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=_transport(board, []))

            self.assertEqual("wait", result["status"])
            self.assertEqual(1135.0, result["next_due_at"])
            self.assertEqual(1000.0, result["last_success_at"])
            self.assertEqual("wait", read_account_state(root, "tg9100000004")["status"])

    async def test_long_server_timer_is_capped_to_periodic_rechecks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = [{"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 129600}]
            result = await run_account(root, "tg9100000004", now=1000.0, bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=_transport(board, []))

        self.assertEqual(("wait", 1000.0 + 7200), (result["status"], result["next_due_at"]))
        self.assertEqual(129600, result["min_wait_sec"])

    async def test_uncertain_mutation_is_reconciled_read_only_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_account_state(root, "tg9100000004", {
                "status": "unknown_mutation_outcome", "next_due_at": 0, "updated_at": 900.0,
                "pending_action": {"action": "soothe", "plotKey": ""},
                "pending_before": [{"key": "8", "empty": False, "name": "天雷星", "status": "星光黯淡"}],
            })
            mutations = []
            board = [{"key": "8", "empty": False, "name": "天雷星", "status": "星光黯淡"}]
            result = await run_account(root, "tg9100000004", now=1000.0, bridge_factory=lambda _r, _a: _Bridge(),
                                       transport=_transport(board, mutations))

            self.assertEqual("unknown_mutation_outcome", result["status"])
            self.assertEqual([], mutations)
            self.assertEqual(2800.0, result["next_due_at"])

            # After the hold window a fresh board read decides again.
            acted = []

            def post(path, payload, *, mutation):
                if path == DWELLING_START:
                    return {"identity": {"choices": [{"playerId": 9100000004, "daoName": "竹衍衡",
                                                      "source": "personal"}]}}
                if path == DWELLING_EXTERNAL:
                    return {"url": "/miniapp/xianxia-sect-farm?startapp=farm_CHILD"}
                if mutation:
                    acted.append(payload["action"])
                    return {"actionResult": {"ok": True}, "domain": {"mode": "stars", "sectName": "星宫", "plots": [
                        {"key": "8", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 600}]}}
                return {"domain": {"mode": "stars", "sectName": "星宫", "plots": board}}

            later = await run_account(root, "tg9100000004", now=2800.0, bridge_factory=lambda _r, _a: _Bridge(),
                                      transport=JsonTransport(ALLOWED_PATHS, post=post))
            self.assertEqual(("wait", ["soothe"]), (later["status"], acted))

    async def test_username_mismatch_blocks_every_action(self):
        class WrongMe:
            username = "sample_peer_12_else"

        class WrongClient:
            async def get_me(self):
                return WrongMe()

        class WrongBridge(_Bridge):
            client = WrongClient()

        with tempfile.TemporaryDirectory() as tmp:
            result = await run_account(Path(tmp), "tg9100000004", now=1.0, bridge_factory=lambda _r, _a: WrongBridge(),
                                       transport=_transport([{"key": "1", "empty": True}], []))

        self.assertEqual("account_identity_mismatch", result["status"])


if __name__ == "__main__":
    unittest.main()
