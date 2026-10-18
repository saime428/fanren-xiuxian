#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from dotenv import load_dotenv

from state_store import FlowLockBusy, flow_lock, load_json_dict, persist_json_atomic
from event_monitor_runtime import EventMonitorHealth
from command_confirmation import OFFICIAL_REPLY_SENDER_IDS
from rare_item_notice import RewardOutbox, telegram_rewards, CHAT_ID
from miniapp_shared.accounts import ACCOUNT_USERNAMES


BONUS_MARKER = re.compile(r"天[赐賜][机機][缘緣]")
REWARD_ITEM_PATTERN = re.compile(r"【([^】]+)】")
ACTOR_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])")
DEFAULT_TARGET_USERNAMES = frozenset({"demo_player_01", "demo_player_03"})
FILTER_VERSION = "reward-recipient-v4-rare-six-accounts"


@dataclass(frozen=True)
class RecipeNotice:
    # Keep the legacy field name so already delivered notices remain deduplicated.
    recipe: str
    actors: tuple[str, ...]
    reward_text: str = ""


def extract_recipe_notice(text: str) -> RecipeNotice | None:
    marker = BONUS_MARKER.search(text)
    if marker is None:
        return None
    reward_text = text[marker.end():].lstrip(" \t\r\n!！:：").strip() or marker.group(0)
    items = tuple(dict.fromkeys(REWARD_ITEM_PATTERN.findall(reward_text)))
    # Every extra reward is eligible, including new item names and unbracketed
    # formats. The verified source, bonus event and recipient checks set scope.
    summary = "、".join(items) if items else " ".join(reward_text.split())
    actors = tuple(dict.fromkeys(ACTOR_PATTERN.findall(text)))
    return RecipeNotice(recipe=summary, actors=actors, reward_text=reward_text)


def eligible_recipe_notice(
    message: object,
    trusted_sender_ids: set[int],
    topic_id: int,
    target_usernames: frozenset[str] = DEFAULT_TARGET_USERNAMES,
) -> RecipeNotice | None:
    if not trusted_sender_ids or getattr(message, "sender_id", None) not in trusted_sender_ids:
        return None
    if getattr(message, "fwd_from", None) is not None:
        return None
    reply_to = getattr(message, "reply_to", None)
    if (getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)) != topic_id:
        return None
    if getattr(message, "chat_id", None) != -1009400000004:
        return None
    text = str(getattr(message, "raw_text", "") or getattr(message, "message", "") or "")
    notice = extract_recipe_notice(text)
    if notice is None:
        return None
    allowed = {username.lstrip('@').casefold() for username in target_usernames}
    actors = tuple(dict.fromkeys(actor.casefold() for actor in notice.actors if actor.casefold() in allowed))
    return RecipeNotice(notice.recipe, actors, notice.reward_text) if actors else None


def format_notification(
    notice: RecipeNotice,
    *,
    chat_username: str,
    topic_id: int,
    message_id: int,
) -> str:
    actor_text = "、".join(f"@{actor}" for actor in notice.actors) or "未知角色"
    permalink = f"https://t.me/{chat_username.lstrip('@')}/{topic_id}/{message_id}"
    allowed = {actor.casefold() for actor in notice.actors}
    detail = notice.reward_text or f"获得【{notice.recipe}】"
    detail = ACTOR_PATTERN.sub(
        lambda match: f"@{match.group(1).casefold()}" if match.group(1).casefold() in allowed else "同修",
        detail,
    )
    return f"✨ 额外收获：{actor_text}\n{detail}\n{permalink}"


def recipe_notification_key(message_id: int, notice: RecipeNotice) -> str:
    material = f"{message_id}\0{notice.recipe}\0{'|'.join(notice.actors)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def process_recipe_message(
    message: object,
    *,
    state_path: Path,
    trusted_sender_ids: set[int],
    topic_id: int,
    chat_username: str,
    deliver: Callable[[str], Awaitable[dict | None]],
    target_usernames: frozenset[str] = DEFAULT_TARGET_USERNAMES,
) -> bool:
    notice = eligible_recipe_notice(message, trusted_sender_ids, topic_id, target_usernames)
    message_id = int(getattr(message, "id", 0) or 0)
    if notice is None or message_id <= 0:
        return False

    state = load_json_dict(state_path)
    delivered = [item for item in state.get("delivered", []) if isinstance(item, dict)]
    key = recipe_notification_key(message_id, notice)
    if any(item.get("key") == key or (item.get("message_id") == message_id and item.get("recipe") == notice.recipe)
           for item in delivered):
        return False

    text = format_notification(
        notice,
        chat_username=chat_username,
        topic_id=topic_id,
        message_id=message_id,
    )
    delivery_receipt = await deliver(text)
    delivered.append(
        {
            "key": key,
            "message_id": message_id,
            "recipe": notice.recipe,
            "actors": list(notice.actors),
            "reward_text": notice.reward_text,
            "delivery_receipt": delivery_receipt,
            "delivered_at_ts": time.time(),
        }
    )
    state.update(
        {
            "status": "delivered",
            "last_message_id": message_id,
            "last_recipe": notice.recipe,
            "updated_at_ts": time.time(),
            "delivered": delivered[-100:],
        }
    )
    persist_json_atomic(state_path, state)
    return True


async def deliver_with_hermes(text: str, target: str, hermes_bin: str) -> dict:
    def _send() -> dict:
        completed = subprocess.run(
            [hermes_bin, "send", "--json", "--to", target, "--file", "-"],
            input=text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "delivery failed").strip()
            raise RuntimeError(f"hermes_send_failed: {detail[:300]}")
        try:
            receipt = json.loads(completed.stdout)
            message_id = int(receipt.get("message_id") or 0)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RuntimeError("hermes_send_unconfirmed: missing delivery receipt") from exc
        if (receipt.get("success") is not True or receipt.get("skipped") or receipt.get("error")
                or isinstance(receipt.get("message_id"), bool) or message_id <= 0):
            raise RuntimeError("hermes_send_unconfirmed: no confirmed message delivery")
        return {"target": target, "message_id": message_id, "success": True}

    return await asyncio.to_thread(_send)


async def read_reward_history(client, entity, *, cursor: int, limit: int):
    """Forward cursor catches long gaps; a recent overlap catches edited replies."""
    if cursor > 0 and hasattr(client,'iter_messages'):
        forward=[message async for message in client.iter_messages(entity,min_id=cursor,reverse=True,limit=limit)]
        next_cursor=max([cursor,*[int(m.id) for m in forward]])
        backlog=len(forward)>=limit
        recent=[] if backlog else list(await client.get_messages(entity,limit=min(50,limit)) or [])
        merged={int(m.id):m for m in [*forward,*recent]}
        return [merged[k] for k in sorted(merged)],next_cursor,backlog
    recent=list(await client.get_messages(entity,limit=limit) or [])
    return sorted(recent,key=lambda m:int(m.id)),max([cursor,*[int(m.id) for m in recent]]),False


async def report_rare_activation(state_path,deliver,*,now=None):
    now=time.time() if now is None else now
    state=load_json_dict(state_path)
    if state.get('rare_activation_version')==FILTER_VERSION or now<float(state.get('rare_activation_retry_at') or 0):
        return False
    state.update(rare_activation_retry_at=now+300);persist_json_atomic(state_path,state)
    text=('✅ 稀有物品报告已启用\n账号：'+ '、'.join('@'+x for x in ACCOUNT_USERNAMES.values())+
          '\n物品：庚金、青元剑诀（上）、青元剑诀（中）、青元剑诀（下）'+
          '\n核对官方获得回执后通过 Hermes 报告，包含账号、物品数量和来源。'+
          '\n此条是监控启用确认，不代表实际获得物品。')
    try:
        receipt=await deliver(text)
        if (not isinstance(receipt,dict) or receipt.get('success') is not True or receipt.get('target')!='telegram:9100000001'
                or type(receipt.get('message_id')) is not int or receipt['message_id']<=0):
            raise ValueError('activation_delivery_unconfirmed')
    except Exception as exc:
        state.update(rare_activation_error=type(exc).__name__);persist_json_atomic(state_path,state);return False
    state.update(rare_activation_version=FILTER_VERSION,rare_activation_receipt=receipt,rare_activation_error=None)
    persist_json_atomic(state_path,state);return True


async def _run_persistent_locked() -> None:
    from telethon import events

    from transport import disconnect_telegram_client, ensure_connected, telegram_client, with_telegram_retry

    load_dotenv()
    target = os.environ.get("TG_TARGET", "@example_game_group").strip()
    chat_username = target.lstrip("@")
    topic_id = int(os.environ.get("TG_TOPIC_ID", "7310786"))
    session_name = os.environ["TG_SESSION_NAME"]
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    state_dir = Path(os.environ.get("TG_STATE_DIR", "state/accounts/tg9100000001/recipe_notice"))
    state_path = Path(os.environ.get("TG_RECIPE_STATE_PATH", str(state_dir / "notification_state.json")))
    reconcile_seconds = max(300.0, float(os.environ.get("TG_RECIPE_RECONCILE_SECONDS", "3600")))
    history_limit = max(50, int(os.environ.get("TG_RECIPE_HISTORY_LIMIT", "500")))
    delivery_target = os.environ.get("TG_RECIPE_DELIVERY_TARGET", "telegram:9100000001").strip()
    hermes_bin = os.environ.get(
        "HERMES_SEND_BIN",
        "/Users/operator/.hermes/hermes-agent/venv/bin/hermes",
    ).strip()
    trusted_sender_ids = {
        int(item.strip())
        for item in os.environ.get("TG_RECIPE_TRUSTED_SENDER_IDS", ",".join(
            str(value) for value in sorted(OFFICIAL_REPLY_SENDER_IDS))).split(",")
        if item.strip().isdigit()
    }
    target_usernames = frozenset(item.strip().lstrip('@').casefold() for item in os.environ.get(
        "TG_RECIPE_TARGET_USERNAMES", ','.join(sorted(DEFAULT_TARGET_USERNAMES))).split(',') if item.strip())
    if not target_usernames or any(re.fullmatch(r'[a-z0-9_]{5,32}', item) is None for item in target_usernames):
        raise SystemExit("TG_RECIPE_TARGET_USERNAMES must name the intended recipient accounts")
    if not trusted_sender_ids:
        raise SystemExit("TG_RECIPE_TRUSTED_SENDER_IDS must contain at least one verified bot sender ID")
    if delivery_target != "telegram:9100000001":
        raise SystemExit("TG_RECIPE_DELIVERY_TARGET must be telegram:9100000001")
    if target != '@example_game_group' or topic_id != 7310786:
        raise SystemExit("Recipe source must be the canonical topic")

    state = load_json_dict(state_path)
    started_at_ts = state.get("started_at_ts")
    if not isinstance(started_at_ts, (int, float)) or started_at_ts <= 0:
        started_at_ts = time.time()
        state.update(
            {
                "status": "listening",
                "started_at_ts": started_at_ts,
                "updated_at_ts": started_at_ts,
                "delivered": [],
            }
        )
        persist_json_atomic(state_path, state)
    state.update({'filter_version': FILTER_VERSION, 'target_usernames': sorted(target_usernames),
                  'rare_item_accounts': sorted(ACCOUNT_USERNAMES),
                  'trusted_sender_ids': sorted(trusted_sender_ids),
                  'pid': os.getpid(), 'updated_at_ts': time.time()})
    persist_json_atomic(state_path, state)

    client = telegram_client(
        session_name,
        api_id,
        api_hash,
        auto_reconnect=True,
        connection_retries=3,
    )
    process_lock = asyncio.Lock()
    outbox = RewardOutbox(state_dir / 'rare_item_outbox.sqlite3')
    project_root = Path(__file__).resolve().parent
    health = EventMonitorHealth(state_dir, 'tg9100000001', 'recipe_notice', 'return_observer', [target], 60)
    next_history_ts = 0.0
    delivery_failure_generation = 0

    async def deliver(text: str) -> dict:
        return await deliver_with_hermes(text, delivery_target, hermes_bin)

    async def lookup_parent(message_id):
        return await asyncio.wait_for(with_telegram_retry(
            lambda: client.get_messages(entity, ids=message_id), label='rare reward recipient readback',
            reconnect=lambda: ensure_connected(client)), timeout=15)

    async def process_message(message):
        rewards = await telegram_rewards(message, trusted_sender_ids=trusted_sender_ids, lookup_parent=lookup_parent)
        message_id = int(getattr(message, 'id', 0) or 0)
        if rewards and message_id > 0:
            outbox.enqueue(rewards, source='telegram', source_id=f'{CHAT_ID}:{message_id}',
                           link=f'https://t.me/{chat_username}/{topic_id}/{message_id}')
        delivered=await process_recipe_message(message, state_path=state_path, trusted_sender_ids=trusted_sender_ids,
                                               topic_id=topic_id, chat_username=chat_username, deliver=deliver,
                                               target_usernames=target_usernames)
        if rewards:
            for row in load_json_dict(state_path).get('delivered',[]):
                if row.get('message_id')==message_id:
                    covered={account for account,name in ACCOUNT_USERNAMES.items() if name in row.get('actors',[])}
                    outbox.confirm_existing_notice(source='telegram',source_id=f'{CHAT_ID}:{message_id}',
                                                   accounts=covered,receipt=row.get('delivery_receipt'))
        return delivered

    async def handle_message(message: object) -> None:
        async with process_lock:
            delivered = await process_message(message)
            await outbox.drain(deliver)
            if delivered:
                print(f"Recipe notification delivered for message_id={getattr(message, 'id', None)}", flush=True)

    try:
        await health.verify_listener(client)
        entity = await with_telegram_retry(
            lambda: client.get_entity(target),
            label="resolve recipe push target",
            reconnect=lambda: ensure_connected(client),
        )

        async def on_message(event: object) -> None:
            nonlocal next_history_ts, delivery_failure_generation
            message = getattr(event, "message", event)
            try:
                await handle_message(message)
            except Exception as exc:
                delivery_failure_generation += 1
                next_history_ts = 0
                health.failed('recipe_delivery', exc)

        client.add_event_handler(on_message, events.NewMessage(chats=entity))
        client.add_event_handler(on_message, events.MessageEdited(chats=entity))

        async def verify_source(*, on_scan_complete) -> None:
            nonlocal next_history_ts
            scan_generation = delivery_failure_generation
            full_history = time.time() >= next_history_ts
            if full_history:
                cursor=int(load_json_dict(state_path).get('history_cursor') or 0)
                recent,next_cursor,backlog=await with_telegram_retry(
                    lambda:read_reward_history(client,entity,cursor=cursor,limit=history_limit),
                    label='recipe history reconciliation',reconnect=lambda:ensure_connected(client))
            else:
                recent=await with_telegram_retry(lambda:client.get_messages(entity,limit=1),
                    label='verify recipe source',reconnect=lambda:ensure_connected(client))
            if full_history:
                async with process_lock:
                    for message in recent or []:
                        message_date = getattr(message, "date", None)
                        if message_date is None or message_date.timestamp() < float(started_at_ts):
                            continue
                        await process_message(message)
                    state = load_json_dict(state_path)
                    state.update(status='listening', last_reconcile_ts=time.time(), updated_at_ts=time.time(),history_cursor=next_cursor)
                    persist_json_atomic(state_path, state)
                    next_history_ts = 0 if backlog else time.time() + reconcile_seconds
            async with process_lock:
                outbox.import_spool(project_root)
                await outbox.drain(deliver)
                state = load_json_dict(state_path)
                state.update(rare_item_outbox=outbox.summary(), rare_item_checked_at=time.time())
                persist_json_atomic(state_path,state)
                if os.environ.get('TG_RARE_ACTIVATION_REPORT')=='1':
                    await report_rare_activation(state_path,deliver)
            if delivery_failure_generation != scan_generation:
                # A push failed while this read was in flight. Its delivery was
                # not verified by this scan; retain the failure and retry history.
                next_history_ts = 0
                return
            on_scan_complete()

        async def reconcile_loop() -> None:
            while True:
                try:
                    await health.run_cycle(client, verify_source)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # The primary health writer retains the exact failure.
                await asyncio.sleep(60)

        reconcile_task = asyncio.create_task(reconcile_loop(), name="recipe_notice_reconcile")
        print(
            f"Recipe push listener active target={target} topic={topic_id}; "
            f"recipients={','.join(sorted(target_usernames))} filter={FILTER_VERSION}; "
            f"history recovery every {reconcile_seconds:g}s",
            flush=True,
        )
        try:
            await client.run_until_disconnected()
        finally:
            reconcile_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconcile_task
    except (Exception, SystemExit) as exc:
        health.failed('recipe_listener', exc)
        raise
    finally:
        health.close()
        with suppress(Exception):
            await disconnect_telegram_client(client, 15)


async def run_persistent() -> None:
    load_dotenv()
    state_dir = Path(os.environ.get("TG_STATE_DIR", "state/accounts/tg9100000001/recipe_notice"))
    with ExitStack() as lease:
        try:
            lease.enter_context(flow_lock(state_dir / "recipe_monitor.lock"))
        except FlowLockBusy:
            print("Another recipe monitor is running; skip this start.", flush=True)
            return  # a busy lease is a normal skip, not a failure
        await _run_persistent_locked()


def main() -> None:
    asyncio.run(run_persistent())


if __name__ == "__main__":
    main()
