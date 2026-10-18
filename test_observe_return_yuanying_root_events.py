import datetime
import unittest
from types import SimpleNamespace

from observe_return import event_matches


class ObserveReturnYuanyingRootEventTests(unittest.TestCase):
    def test_root_yuanying_return_summary_can_match_when_explicitly_allowed(self) -> None:
        message = SimpleNamespace(
            id=521385,
            message=(
                "📜 修士 @demo_player_01 元神归窍总结\n"
                "你的元婴在虚空中神游八小时，带回了以下收获：\n"
                "元婴成长:\n - 获得了 800 点经验。"
            ),
            sender_id=8861328042,
            date=datetime.datetime.fromtimestamp(1785267969),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["元神归窍总结", "你的元婴在虚空中神游八小时"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8861328042},
            last_seen_event_id=0,
            allow_root_topic_events=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 521385)

    def test_demo_player_01_root_yuanying_return_from_sender_8788895566_matches(self) -> None:
        message = SimpleNamespace(
            id=537589,
            message=(
                "📜 修士 @demo_player_01 元神归窍总结\n"
                "你的元婴在虚空中神游八小时，带回了以下收获：\n\n"
                " - 【四级妖丹】x2\n"
                " - 【法则碎片·木】x1\n"
                " - 【法则碎片·金】x1\n"
                " - 【法则碎片·土】x1\n"
                " - 【法则碎片·水】x1\n"
                " - 天道感悟: 你的修为额外增加了 2204 点！\n\n"
                "元婴成长:\n - 获得了 2002 点经验。"
            ),
            sender_id=8788895566,
            date=datetime.datetime.fromtimestamp(1785326218),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["元神归窍总结", "你的元婴在虚空中神游八小时"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8788895566},
            last_seen_event_id=0,
            allow_root_topic_events=True,
            root_last_seen_event_id=530210,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 537589)
        self.assertTrue(result["root_topic_event"])

    def test_new_hantianzun_sender_matches_demo_player_01_root_yuanying_return(self) -> None:
        message = SimpleNamespace(
            id=594040,
            message=(
                "📜 修士 @demo_player_01 元神归窍总结\\n"
                "你的元婴在虚空中神游八小时，带回了以下收获：\\n"
                " - 【法则碎片·土】x2"
            ),
            sender_id=8917921351,
            date=datetime.datetime.fromtimestamp(1785528278),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["元神归窍总结", "你的元婴在虚空中神游八小时"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8917921351},
            last_seen_event_id=584811,
            allow_root_topic_events=True,
            root_last_seen_event_id=584811,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 594040)
        self.assertTrue(result["root_topic_event"])

    def test_root_yuanying_return_summary_still_rejected_by_default(self) -> None:
        message = SimpleNamespace(
            id=521385,
            message=(
                "📜 修士 @demo_player_01 元神归窍总结\n"
                "你的元婴在虚空中神游八小时，带回了以下收获："
            ),
            sender_id=8861328042,
            date=datetime.datetime.fromtimestamp(1785267969),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["元神归窍总结", "你的元婴在虚空中神游八小时"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8861328042},
            last_seen_event_id=0,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
