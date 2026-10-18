"""Official-client examples, with no game requests or synthetic server receipts."""
import unittest

from qyz_combat import CombatState


class CombatBookkeepingTests(unittest.TestCase):
    def battle(self, phase=1):
        rows = []
        combat = CombatState(lambda: 0, lambda phase, **fields: rows.append({"phase": phase, **fields}))
        combat.configure({"player": {"maxHp": 100}, "challenge": {"phase": phase}})
        combat.started_at = 0
        for index, center in enumerate((2200, 5400, 8600)):
            combat.register({"id": f"fixture_{index}", "centerMs": center, "hitMs": 560, "perfectMs": 180})
        return combat, rows

    def test_expired_window_applies_one_counter_at_each_official_phase(self):
        for phase, hp in ((1, 84), (2, 78), (3, 70)):
            with self.subTest(phase=phase):
                combat, rows = self.battle(phase)
                combat.advance(2840)
                self.assertEqual(combat.player_hp, 100)
                combat.advance(2841)
                combat.advance(3000)
                self.assertEqual(combat.player_hp, hp)
                self.assertEqual(len(rows), 1)
                self.assertNotIn("fixture_0", str(rows))

    def test_late_overcharge_includes_counter_and_overload_without_double_counting(self):
        combat, _ = self.battle()
        combat.release("fixture_0", {"t": 4000, "holdMs": 2000, "stance": "强攻"})
        self.assertEqual(combat.player_hp, 76)
        combat.advance(5000)
        self.assertEqual(combat.player_hp, 76)
        self.assertEqual(combat.stats["combo"], 0)

    def test_pending_receipt_is_already_a_local_release_but_never_server_evidence(self):
        combat, rows = self.battle()
        action = {"t": 2200, "holdMs": 900, "stance": "强攻"}
        combat.release("fixture_0", action)
        combat.advance(4000)
        combat.release("fixture_0", action)
        self.assertEqual(combat.player_hp, 100)
        self.assertEqual(combat.stats["perfects"], 1)
        self.assertEqual(len(combat.actions), 1)
        self.assertFalse(rows)


if __name__ == "__main__":
    unittest.main()
