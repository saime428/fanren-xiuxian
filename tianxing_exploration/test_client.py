import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from miniapp_shared.dwelling import MiniAppError

from . import PLAYER_ID
from .client import LiveClient
from .protocol import ExplorationError
from .run_cycle import drain_reports
from .runtime import Store, queue_report
from .test_protocol import NOW


def make_client(tmp_path, transport):
    client = LiveClient(tmp_path, SimpleNamespace(), {}, reader=transport, writer=transport)
    client.session = SimpleNamespace(base_payload=lambda player: {"playerId": player})
    client.commands = {".推命", ".探寻裂缝"}
    client.group_command = AsyncMock(return_value={"group": True})
    return client


@pytest.mark.parametrize("code,uncertain", [("request_failed", True), ("http_429", False), ("http_403", False)])
def test_network_and_auth_errors_do_not_fallback_to_group(tmp_path, code, uncertain):
    async def fail(*args, **kwargs):
        raise MiniAppError(code, uncertain=uncertain)
    client = make_client(tmp_path, fail)
    with pytest.raises(ExplorationError):
        asyncio.run(client.command(".推命 探索"))
    client.group_command.assert_not_called()


def test_explicit_unmigrated_rejection_allows_group_once(tmp_path):
    async def rejected(*args, **kwargs):
        return {"ok": False, "error": "command_not_allowed"}
    client = make_client(tmp_path, rejected)
    assert asyncio.run(client.command(".探寻裂缝")) == {"group": True}
    client.group_command.assert_awaited_once()
    assert ".探寻裂缝" not in client.commands


def test_known_unsupported_command_does_not_probe_write_api(tmp_path):
    transport = AsyncMock()
    client = make_client(tmp_path, transport)
    assert asyncio.run(client.command(".搜寻节点")) == {"group": True}
    transport.assert_not_called()


def test_wrong_player_or_command_never_confirms(tmp_path):
    async def wrong(*args, **kwargs):
        return {"ok": True, "account": {"playerId": PLAYER_ID+1},
                "actionResult": {"command": ".推命 探索", "completed": True}}
    client = make_client(tmp_path, wrong)
    with pytest.raises(ExplorationError, match="identity_mismatch"):
        asyncio.run(client.command(".推命 探索"))
    client.group_command.assert_not_called()


def test_unknown_commands_block_before_any_transport(tmp_path):
    transport = AsyncMock()
    client = make_client(tmp_path, transport)
    with pytest.raises(ExplorationError, match="allowlisted"):
        asyncio.run(client.command(".消劫"))
    client.group_command.assert_not_called()
    transport.assert_not_called()


def test_forwarded_or_other_players_group_reply_is_rejected():
    from command_confirmation import CANONICAL_CHAT_ID
    msg = SimpleNamespace(sender_id=8788895566, fwd_from=None, chat_id=CANONICAL_CHAT_ID,
                          reply_to=SimpleNamespace(reply_to_msg_id=42))
    assert LiveClient._matched(msg, 42, CANONICAL_CHAT_ID)
    assert not LiveClient._matched(msg, 43, CANONICAL_CHAT_ID)
    msg.fwd_from = object()
    assert not LiveClient._matched(msg, 42, CANONICAL_CHAT_ID)


def test_delivery_requires_receipt_and_failed_notification_does_not_repeat_games(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    queue_report(store)
    calls = []
    class Notifier:
        def send_once(self, key, text):
            calls.append(key)
            return {"status": "delivered", "receipt": {"message_id": 1234}}
    drain_reports(store, Notifier())
    drain_reports(store, Notifier())
    assert len(calls) == 1
    assert store.data["outbox"][0]["message_id"] == 1234
    assert not store.data["events"]


def test_unknown_delivery_is_not_blindly_retried(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    queue_report(store)
    calls = []
    class Notifier:
        def send_once(self, key, text):
            calls.append(key)
            return {"status": "delivery_failed", "error": "hermes_send_unconfirmed"}
    drain_reports(store, Notifier())
    drain_reports(store, Notifier())
    assert len(calls) == 1
    assert store.data["outbox"][0]["status"] == "delivery_uncertain"
