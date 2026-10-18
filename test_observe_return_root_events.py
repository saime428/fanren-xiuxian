import datetime
import unittest
from types import SimpleNamespace

from observe_return import event_matches


class ReturnObserverRootEventTests(unittest.TestCase):
    def test_not_in_retreat_reply_matches_only_when_reply_targets_own_status_probe(self) -> None:
        message = SimpleNamespace(
            id=651454,
            sender_id=8918867302,
            message="你并未处于深度闭关之中。",
            reply_to_msg_id=7001,
            reply_to=SimpleNamespace(reply_to_msg_id=7001, reply_to_top_id=7310786),
            date=datetime.datetime.fromtimestamp(1785750000),
        )
        kwargs = dict(
            markers=["你并未处于深度闭关之中"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8918867302},
            last_seen_event_id=0,
            allow_root_topic_events=False,
            owned_status_probe_ids={7001},
        )
        self.assertIsNotNone(event_matches(message, **kwargs))
        self.assertIsNone(event_matches(message, **{**kwargs, "owned_status_probe_ids": {7002}}))

    def test_root_return_summary_can_match_when_explicitly_allowed(self) -> None:
        message = SimpleNamespace(
            id=521387,
            message="📜 修士 @demo_player_01 深度闭关总结\n【深度闭关总结】",
            sender_id=8918867302,
            date=datetime.datetime.fromtimestamp(1785267972),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["深度闭关总结", "深度閉關總結"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8918867302},
            last_seen_event_id=0,
            allow_root_topic_events=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 521387)

    def test_root_return_summary_still_rejected_by_default(self) -> None:
        message = SimpleNamespace(
            id=521387,
            message="📜 修士 @demo_player_01 深度闭关总结\n【深度闭关总结】",
            sender_id=8918867302,
            date=datetime.datetime.fromtimestamp(1785267972),
            reply_to=None,
        )

        result = event_matches(
            message,
            markers=["深度闭关总结", "深度閉關總結"],
            normalized_topic_id=7310786,
            target_username="demo_player_01",
            trusted_sender_ids={8918867302},
            last_seen_event_id=0,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
