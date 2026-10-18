import unittest

from journey_miniapp.protocol import action_payload, classify_attempt, parse_wild_state


def _payload(count, *, available=True, limit=8, remaining_seconds=0, ready_at=0):
    return {"account": {"journey": {"wildExperience": {
        "available": available, "dailyCount": count, "dailyLimit": limit, "dailyRemaining": limit - count,
        "remainingSeconds": remaining_seconds, "readyAt": ready_at, "resetAt": 1789660800000,
        "modes": [{"key": "cautious", "label": "谨慎"}, {"key": "balanced", "label": "均衡"},
                  {"key": "deep", "label": "深入"}]}}}}


class JourneyProtocolTests(unittest.TestCase):
    def test_live_camel_case_state_is_normalized(self):
        state = parse_wild_state(_payload(3, available=False, remaining_seconds=5400))
        self.assertEqual((3, 8, 5, False, 5400), (state["daily_count"], state["daily_limit"], state["daily_remaining"],
                                                   state["available"], state["remaining_seconds"]))
        self.assertEqual(1789660800.0, state["reset_at"])
        self.assertEqual(["cautious", "balanced", "deep"], [mode["key"] for mode in state["modes"]])

    def test_ready_at_milliseconds_derive_remaining_seconds(self):
        state = parse_wild_state(_payload(1, available=False, ready_at=1_800_000_600_000), now_ts=1_800_000_000)
        self.assertEqual(600, state["remaining_seconds"])
        self.assertEqual({}, parse_wild_state({"account": {}}))

    def test_action_payload_is_fixed_to_cautious(self):
        payload = action_payload({"token": "df_PARENT", "initData": "INIT"}, 123)
        self.assertEqual({"token": "df_PARENT", "initData": "INIT", "playerId": 123, "action": "wild_experience",
                          "mode": "cautious"}, payload)
        with self.assertRaises(ValueError):
            action_payload({"token": "t", "initData": "i"}, 1, mode="deep")

    def test_attempt_confirmation_requires_counter_to_advance_by_one(self):
        confirmed = classify_attempt({**_payload(4), "actionResult": {"type": "wild_experience", "ok": True,
                                                                     "completed": True, "handled": True,
                                                                     "outcome": "victory", "dailyCount": 4,
                                                                     "mode": {"key": "cautious"},
                                                                     "cultivationDelta": 120}}, before_count=3)
        self.assertEqual(("confirmed", "victory", "cautious", 120),
                         (confirmed["status"], confirmed["outcome"], confirmed["mode"], confirmed["cultivation_delta"]))
        stale = classify_attempt({**_payload(3), "actionResult": {"type": "wild_experience", "ok": True,
                                                                 "completed": True, "handled": True, "dailyCount": 3}},
                                 before_count=3)
        self.assertEqual(("unconfirmed", "counter_not_advanced"), (stale["status"], stale["error"]))
        rejected = classify_attempt({"actionResult": {"ok": False, "error": "wild_experience_unavailable"}},
                                    before_count=3)
        self.assertEqual(("rejected", "wild_experience_unavailable"), (rejected["status"], rejected["error"]))
        wrong_type = classify_attempt({**_payload(4), "actionResult": {"type": "hunt", "ok": True}}, before_count=3)
        self.assertEqual("unconfirmed", wrong_type["status"])


    def test_malformed_optional_fields_do_not_raise_after_an_attempt(self):
        # A reply to a spent attempt must still be classified, not crash on an odd field.
        payload = {"account": {"journey": {"wildExperience": {
            "dailyCount": 4, "dailyLimit": 8, "cooldownHours": "2h", "modes": 3, "resetAt": "soon"}}},
            "actionResult": {"type": "wild_experience", "ok": True, "dailyCount": 4}}
        verdict = classify_attempt(payload, before_count=3)
        self.assertEqual(("confirmed", 4, [], 0.0), (verdict["status"], verdict["state"]["daily_count"],
                                                      verdict["state"]["modes"], verdict["state"]["reset_at"]))

if __name__ == "__main__":
    unittest.main()
