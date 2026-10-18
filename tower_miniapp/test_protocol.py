import unittest

from tower_miniapp.protocol import attempted_today, classify_challenge_response, parse_state, result_text


class TowerProtocolTests(unittest.TestCase):
    def test_start_state_and_attempted_today_counters(self):
        state = parse_state({"state": {"canChallenge": True, "todayHighest": 0, "failedFloor": 0, "resetsToday": 0,
                                       "recordHighest": 12, "towerMarks": "1,024", "daoName": "示例修士一"}})
        self.assertEqual((True, 12, 1024, "示例修士一"), (state["can_challenge"], state["record_highest"],
                                                       state["tower_marks"], state["dao_name"]))
        self.assertFalse(attempted_today(state))
        self.assertTrue(attempted_today(parse_state({"data": {"state": {"canChallenge": False, "todayHighest": 9}}})))
        self.assertEqual({}, parse_state({"state": {"todayHighest": 3}}))

    def test_challenge_classification_requires_replay_and_terminal_state(self):
        settled = classify_challenge_response({"state": {"canChallenge": False, "todayHighest": 8},
                                               "replay": {"clearedCount": 8, "endFloor": 9, "failedFloor": 9,
                                                          "report": "第9层失败"}})
        self.assertEqual("settled_confirmed", settled["status"])
        self.assertEqual("通过 8 层，抵达第 9 层，止步第 9 层", result_text(settled["replay"], settled["state"]))
        self.assertEqual("oracle_missing", classify_challenge_response({"state": {"canChallenge": True}})["status"])
        self.assertEqual("settled_state_missing",
                         classify_challenge_response({"replay": {"clearedCount": 2, "endFloor": 3}})["status"])
        self.assertEqual("服务器已记录今日登塔", result_text({}, {"today_highest": 3}))


if __name__ == "__main__":
    unittest.main()
