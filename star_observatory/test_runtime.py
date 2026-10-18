import unittest

from miniapp_shared.dwelling import MiniAppError
from star_observatory.runtime import drive_observatory


def _domain(plots):
    return {"mode": "stars", "sectName": "星宫", "plots": plots}


class StarObservatoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_soothe_collect_then_pull_every_empty_plot_and_wait_on_server_timer(self):
        sent = []
        responses = [
            {"actionResult": {"ok": True, "message": "安抚完成"}, "domain": _domain([
                {"key": "1", "empty": False, "name": "天雷星", "status": "可收集"},
                {"key": "2", "empty": False, "name": "天雷星", "status": "可收集"},
            ])},
            {"actionResult": {"ok": True, "message": "收集完成"}, "domain": _domain([
                {"key": "1", "empty": True}, {"key": "2", "empty": True},
            ])},
            {"actionResult": {"ok": True, "message": "牵引完成"}, "domain": _domain([
                {"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 129600},
                {"key": "2", "empty": True},
            ])},
            {"actionResult": {"ok": True, "message": "牵引完成"}, "domain": _domain([
                {"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 129599},
                {"key": "2", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 129600},
            ])},
        ]

        async def send_action(payload):
            sent.append(payload)
            return responses.pop(0)

        result = await drive_observatory({"domain": _domain([
            {"key": "1", "empty": False, "name": "天雷星", "status": "星光黯淡"},
            {"key": "2", "empty": False, "name": "天雷星", "status": "可收集"},
        ])}, send_action)

        self.assertEqual("wait", result["status"])
        self.assertEqual([
            {"action": "soothe", "plotKey": ""},
            {"action": "collect", "plotKey": ""},
            {"action": "pull", "plotKey": "1", "starName": "天雷星"},
            {"action": "pull", "plotKey": "2", "starName": "天雷星"},
        ], sent)
        self.assertEqual({"soothe": 1, "collect": 1, "pull": 2}, result["action_counts"])
        self.assertEqual(129599, result["next_wait_sec"])

    async def test_transport_failure_is_unknown_and_never_replayed(self):
        calls = []

        async def send_action(payload):
            calls.append(payload)
            raise MiniAppError("request_failed", uncertain=True)

        result = await drive_observatory({"domain": _domain([
            {"key": "8", "empty": False, "name": "天雷星", "status": "星光黯淡"},
        ])}, send_action)

        self.assertEqual("unknown_mutation_outcome", result["status"])
        self.assertEqual([{"action": "soothe", "plotKey": ""}], calls)
        self.assertEqual([{"key": "8", "empty": False, "name": "天雷星", "status": "星光黯淡"}],
                         result["pending_before"])

    async def test_server_rejection_stops_cycle_with_error_code(self):
        async def send_action(_payload):
            return {"actionResult": {"ok": False, "error": "cultivation_insufficient"}}

        result = await drive_observatory({"domain": _domain([{"key": "1", "empty": True}])}, send_action)

        self.assertEqual(("action_failed", "cultivation_insufficient", "pull"),
                         (result["status"], result["error"], result["action"]))
        self.assertEqual({"soothe": 0, "collect": 0, "pull": 0}, result["action_counts"])

    async def test_benign_noop_does_not_loop_forever(self):
        calls = []
        board = _domain([{"key": "1", "empty": False, "name": "天雷星", "status": "可收集"}])

        async def send_action(payload):
            calls.append(payload["action"])
            return {"actionResult": {"ok": False, "error": "nothing_ready"}, "domain": board}

        result = await drive_observatory({"domain": board}, send_action)

        self.assertEqual(["collect"], calls)
        self.assertEqual("inspect", result["status"])
        self.assertEqual(["collect:nothing_ready"], result["benign"])

    async def test_top_level_benign_error_is_reconciled_by_refresh(self):
        board = _domain([{"key": "1", "empty": False, "name": "天雷星", "status": "星光黯淡"}])
        refreshed = _domain([{"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中",
                              "remainingSeconds": 500}])
        reads = []

        async def send_action(_payload):
            raise MiniAppError("nothing_to_soothe")

        async def refresh():
            reads.append(1)
            return {"domain": refreshed}

        result = await drive_observatory({"domain": board}, send_action, refresh=refresh)

        self.assertEqual(("wait", 500, [1]), (result["status"], result["next_wait_sec"], reads))
        self.assertEqual(["soothe:nothing_to_soothe"], result["benign"])

    async def test_other_sect_is_refused_before_any_action(self):
        async def send_action(_payload):
            raise AssertionError("must not act")

        result = await drive_observatory({"domain": {"mode": "stars", "sectName": "太一门", "plots": [
            {"key": "1", "empty": True}]}}, send_action)

        self.assertEqual("sect_mismatch", result["status"])


if __name__ == "__main__":
    unittest.main()
