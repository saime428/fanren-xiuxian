"""Telegram write boundary shared by command, MiniApp and QYZ clients."""
from __future__ import annotations

import logging

from telethon import TelegramClient, events

from account_lifecycle import assert_account_allowed, observe_reply

# Check at _call, after Telethon has built the actual RPC, not just at the
# high-level send_message convenience method. Retries recheck the disk state.
def _is_dot_command(request):
    name = type(request).__name__
    if name == "SendMultiMediaRequest":
        return any(str(getattr(item, "message", "") or "").lstrip().startswith((".", "。"))
                   for item in request.multi_media)
    return name in {"SendMessageRequest", "SendMediaRequest", "EditMessageRequest"} and str(
        getattr(request, "message", "") or ""
    ).lstrip().startswith((".", "。"))


class _GuardedSender:
    """Recheck even Telethon's internal server/FloodWait retry submissions."""
    def __init__(self, sender, identity):
        self.sender = sender
        self.identity = identity

    def __getattr__(self, name):
        return getattr(self.sender, name)

    def send(self, *args, **kwargs):
        assert_account_allowed(self.identity)
        return self.sender.send(*args, **kwargs)


class LifecycleTelegramClient(TelegramClient):
    def __init__(self, *args, lifecycle_observer=False, **kwargs):
        super().__init__(*args, **kwargs)
        self._lifecycle_identity = None
        # Only the singleton observer resolves reply parents. Registering the
        # same reader on all ~30 workers would amplify each lookup thirtyfold.
        if lifecycle_observer:
            self.add_event_handler(self._lifecycle_observe, events.NewMessage())
            self.add_event_handler(self._lifecycle_observe, events.MessageEdited())

    async def _lifecycle_observe(self, event):
        try:
            await observe_reply(self, event.message)
        except Exception:
            logging.getLogger(__name__).exception("account lifecycle observation failed")

    async def _call(self, sender, request, *args, **kwargs):
        requests = request if isinstance(request, (list, tuple)) else (request,)
        if any(_is_dot_command(item) for item in requests):
            if self._lifecycle_identity is None:
                me = await self.get_me()
                if not me or not getattr(me, "id", None):
                    raise RuntimeError("account_lifecycle_identity_unknown")
                self._lifecycle_identity = int(me.id)
            assert_account_allowed(self._lifecycle_identity)
            sender = _GuardedSender(sender, self._lifecycle_identity)
        return await super()._call(sender, request, *args, **kwargs)
