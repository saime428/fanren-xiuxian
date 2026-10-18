import unittest

from star_observatory.protocol import choose_action, classify_action, parse_farm_state


def _state(plots):
    return parse_farm_state({"domain": {"mode": "stars", "sectName": "星宫", "plots": plots}})


class StarObservatoryProtocolTests(unittest.TestCase):
    def test_soothe_has_priority_over_collect_and_pull(self):
        state = _state([
            {"key": "1", "empty": True},
            {"key": "2", "empty": False, "name": "天雷星", "status": "可收集"},
            {"key": "3", "empty": False, "name": "天雷星", "status": "元磁紊乱"},
        ])
        decision = choose_action(state)
        self.assertEqual(("soothe", "", ["3"]), (decision["action"], decision["plotKey"], decision["keys"]))

    def test_collect_is_one_click_when_any_plot_is_ready(self):
        state = _state([
            {"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 300},
            {"key": "2", "empty": False, "name": "天雷星", "status": "精华已成"},
        ])
        decision = choose_action(state)
        self.assertEqual(("collect", "", ["2"]), (decision["action"], decision["plotKey"], decision["keys"]))

    def test_pull_fills_first_empty_plot_with_tianlei(self):
        state = _state([
            {"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 300},
            {"key": "2", "empty": True},
            {"key": "3", "empty": True},
        ])
        self.assertEqual({"action": "pull", "plotKey": "2", "starName": "天雷星", "reason": "empty_plot"},
                         choose_action(state))

    def test_wait_uses_earliest_server_timer(self):
        state = _state([
            {"key": "1", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingSeconds": 100},
            {"key": "2", "empty": False, "name": "天雷星", "status": "凝聚中", "remainingText": "剩余 31小时9分"},
        ])
        self.assertEqual({"action": "wait", "plotKey": "", "wait_sec": 100, "reason": "maturing"},
                         choose_action(state))
        self.assertEqual(31 * 3600 + 9 * 60, state["max_wait"])

    def test_plots_as_mapping_and_idle_status_count_as_empty(self):
        state = parse_farm_state({"data": {"domain": {"mode": "stars", "plots": {
            "1": {"status": "空闲"}, "2": {"name": "天雷星", "status": "凝聚中", "remainingSeconds": 5}}}}})
        self.assertEqual(["1"], state["empty_keys"])
        self.assertEqual(["2"], state["busy_keys"])

    def test_non_star_domain_is_rejected(self):
        self.assertEqual({}, parse_farm_state({"domain": {"mode": "herbs", "plots": [{"key": "1"}]}}))
        self.assertEqual("inspect", choose_action({})["action"])

    def test_classify_action_accepts_benign_noops_and_rejects_real_failures(self):
        ok = classify_action({"actionResult": {"ok": True, "message": "已收集"}, "domain": {"mode": "stars"}}, "collect")
        self.assertEqual((True, False), (ok["ok"], ok["benign"]))
        benign = classify_action({"actionResult": {"ok": False, "error": "nothing_ready"}}, "collect")
        self.assertEqual((True, True, "nothing_ready"), (benign["ok"], benign["benign"], benign["error"]))
        failed = classify_action({"actionResult": {"ok": False, "error": "cultivation_insufficient"}}, "pull")
        self.assertEqual((False, "cultivation_insufficient"), (failed["ok"], failed["error"]))
        missing = classify_action({"ok": True}, "pull")
        self.assertEqual((False, "action_result_missing"), (missing["ok"], missing["error"]))


if __name__ == "__main__":
    unittest.main()
