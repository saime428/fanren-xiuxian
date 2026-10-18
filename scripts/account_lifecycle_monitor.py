#!/usr/bin/env python3
"""Read-only singleton lifecycle observer and notification outbox owner."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from account_lifecycle import (CHAT_ID, READ_CHAT_IDS, flush_notifications, observe_reply,
                               queue_ready_reminders, transaction)
from fishing.fishing_accounts import read_account_env
from miniapp_shared.accounts import (CREDENTIAL_SOURCE, DWELLING_SESSIONS, USER_PATH, accounts_for,
                                     feature_options)
from miniapp_shared.notify import Notifier
from telethon.errors import FloodWaitError
from transport import ensure_connected, telegram_client

PRODUCTION_ROOT = Path("/Users/operator/.hermes/workspace/projects/xiuxian")
# The account whose session listens for every account's lifecycle replies: the
# account_lifecycle account with "listener": true, else the first one.
LISTENER = next((account for account in accounts_for("account_lifecycle")
                 if feature_options(account, "account_lifecycle").get("listener") is True),
                next(iter(accounts_for("account_lifecycle")), None))


def load_transport_env():
    """launchd runs this file directly, so source mtproto.env like the shell wrappers do."""
    try:
        values = read_account_env("mtproto")
    except FileNotFoundError:
        return
    for key, value in values.items():
        os.environ.setdefault(key, value)


async def reconcile(client, *, bootstrap=False):
    # Recovery is intentionally read-only: search official death/rebirth receipts,
    # then catch short arbitrary starred replies in the recent message horizon.
    rows = {}
    for chat_id in READ_CHAT_IDS:
        if bootstrap:
            terms = (("元婴遁逃", "夺舍成功", "请再温养", "成功夺舍重生")
                     if chat_id == CHAT_ID else ("成功夺舍重生",))
            for term in terms:
                for message in await client.get_messages(chat_id, search=term, limit=50):
                    rows[(chat_id, message.id)] = message
        for message in await client.get_messages(chat_id, limit=300):
            rows[(chat_id, message.id)] = message
    # Message IDs only order messages within one chat; recovery announcements
    # live in a different group, so merge the two streams by official timestamp.
    for message in sorted(rows.values(), key=lambda item: (item.date.timestamp(), item.id)):
        await observe_reply(client, message)
    return max((message.id for message in rows.values()), default=0)


async def reconcile_with_backoff(client, *, bootstrap=False):
    while True:
        try:
            return await reconcile(client, bootstrap=bootstrap)
        except FloodWaitError as exc:
            # Keep the push listener connected and respect Telegram's cooldown;
            # restarting would immediately repeat the same history requests.
            logging.warning("lifecycle history rate limited; waiting %s seconds", exc.seconds)
            await asyncio.sleep(max(1, exc.seconds))


async def run(args):
    # Only a deployment may notify: a checkout with its own accounts config, or
    # the original operator's production root.
    if args.notify and not USER_PATH.is_file() and ROOT.resolve() != PRODUCTION_ROOT.resolve():
        raise RuntimeError("lifecycle notifications refused outside production root")
    if LISTENER is None:
        raise RuntimeError("no account enables account_lifecycle")
    env = read_account_env(CREDENTIAL_SOURCE[LISTENER])
    load_transport_env()
    source = ROOT / DWELLING_SESSIONS[LISTENER]
    state_dir = ROOT / "state/account_lifecycle"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "observer.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("lifecycle observer already running")
        with tempfile.TemporaryDirectory(prefix="lifecycle-session-") as directory:
            copied = Path(directory) / "observer.session"
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(copied) as dst:
                src.backup(dst)
            # The shared factory enforces the MTProxy settings and keeps
            # Telethon's internal request retries and flood sleep disabled;
            # ensure_connected goes through the fleet's connection gate.
            client = telegram_client(str(copied), int(env["TG_API_ID"]), env["TG_API_HASH"],
                                     connection_retries=3, auto_reconnect=True, lifecycle_observer=True)
            await ensure_connected(client)
            try:
                if not await client.is_user_authorized():
                    raise RuntimeError("lifecycle observer session unauthorized")
                notifier = Notifier(ROOT, "account_lifecycle") if args.notify else None
                bootstrap = True
                while True:
                    if args.message_id:
                        # Resolve newer possession evidence before seeding an old
                        # link. The watermark then rejects obsolete incidents.
                        await reconcile_with_backoff(client, bootstrap=True)
                        message = await client.get_messages(CHAT_ID, ids=args.message_id)
                        await observe_reply(client, message)
                    else:
                        await reconcile_with_backoff(client, bootstrap=bootstrap)
                    queue_ready_reminders()
                    if notifier:
                        await asyncio.to_thread(flush_notifications, notifier)
                    with transaction() as state:
                        state["observer"] = {"last_success_at": time.time(), "read_only": True}
                    if args.once:
                        break
                    bootstrap = False
                    await asyncio.sleep(60)
            finally:
                await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--message-id", type=int)
    parser.add_argument("--notify", action="store_true", help="Deliver real lifecycle notifications (production only)")
    args = parser.parse_args()
    if args.message_id and not args.once:
        parser.error("--message-id requires --once")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
