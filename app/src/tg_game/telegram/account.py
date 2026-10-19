import asyncio
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from tg_game.config import get_settings, telegram_client_kwargs


def _session_name(session_name: str = "") -> str:
    settings = get_settings()
    return (session_name or settings.telegram_session_name).strip()


def _session_path(session_name: str) -> str:
    name = (session_name or "").strip()
    if not name:
        return name
    path = Path(name)
    if path.is_absolute() or path.parent != Path("."):
        return name
    return str(get_settings().database_path.parent / name)


def _session_candidates(
    session_name: str = "", allow_fallback: bool = True
) -> list[str]:
    settings = get_settings()
    primary = (session_name or "").strip()
    if primary and not allow_fallback:
        return [primary]
    candidates = [
        primary,
        (settings.telegram_login_session_name or "").strip(),
        (settings.telegram_session_name or "").strip(),
    ]
    ordered = []
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def build_client(session_name: str = "", save_entities: bool = True) -> TelegramClient:
    settings = get_settings()
    client = TelegramClient(
        _session_path(_session_name(session_name)),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
        **telegram_client_kwargs(),
    )
    if hasattr(client, "session") and hasattr(client.session, "save_entities"):
        client.session.save_entities = bool(save_entities)
    return client


async def _graceful_disconnect(client: TelegramClient) -> None:
    if client.is_connected():
        await asyncio.shield(client.disconnect())
    try:
        await asyncio.shield(asyncio.wait_for(client.disconnected, timeout=5))
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(0.05)


async def get_authorized_account_info(
    session_name: str = "", allow_fallback: bool = True
) -> dict:
    for candidate in _session_candidates(session_name, allow_fallback=allow_fallback):
        client = build_client(candidate, save_entities=False)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                continue
            me = await client.get_me()
            return {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", "") or "",
                "first_name": getattr(me, "first_name", "") or "",
                "last_name": getattr(me, "last_name", "") or "",
                "phone": getattr(me, "phone", "") or "",
                "session_name": candidate,
            }
        finally:
            await _graceful_disconnect(client)
    raise RuntimeError("Telegram session is not authorized")


async def has_authorized_session(
    session_name: str = "", allow_fallback: bool = True
) -> bool:
    for candidate in _session_candidates(session_name, allow_fallback=allow_fallback):
        client = build_client(candidate, save_entities=False)
        await client.connect()
        try:
            if await client.is_user_authorized():
                return True
        finally:
            await _graceful_disconnect(client)
    return False


async def send_login_code(phone: str, session_name: str = "") -> dict:
    client = build_client(session_name)
    await client.connect()
    try:
        sent = await client.send_code_request((phone or "").strip())
        return {
            "phone": (phone or "").strip(),
            "phone_code_hash": getattr(sent, "phone_code_hash", "") or "",
            "session_name": _session_name(session_name),
        }
    finally:
        await _graceful_disconnect(client)


async def verify_login_code(
    phone: str, code: str, phone_code_hash: str, session_name: str = ""
) -> dict:
    client = build_client(session_name)
    await client.connect()
    try:
        try:
            me = await client.sign_in(
                phone=(phone or "").strip(),
                code=(code or "").strip(),
                phone_code_hash=(phone_code_hash or "").strip(),
            )
        except SessionPasswordNeededError:
            return {
                "requires_password": True,
                "session_name": _session_name(session_name),
            }
        return {
            "requires_password": False,
            "account": {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", "") or "",
                "first_name": getattr(me, "first_name", "") or "",
                "last_name": getattr(me, "last_name", "") or "",
                "phone": getattr(me, "phone", "") or "",
                "session_name": _session_name(session_name),
            },
        }
    finally:
        await _graceful_disconnect(client)


async def run_qr_login(session_name: str, on_url, total_seconds: float = 180) -> dict:
    """扫码登录：每出一张新二维码就 on_url(链接, 过期时间戳)；手机 Telegram「设置 → 设备 →
    连接桌面设备」扫码后返回账号，开了两步验证则返回 requires_password（再走密码那一步）。
    二维码约 30 秒过期，过期自动换新，总共等 total_seconds。"""
    client = build_client(session_name)
    await client.connect()
    signed_in = False
    try:
        qr = await client.qr_login()
        deadline = time.monotonic() + total_seconds
        while True:
            on_url(qr.url, qr.expires.timestamp())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("二维码已过期，请重新开始扫码")
            try:
                me = await qr.wait(timeout=max(1.0, min(remaining, qr.expires.timestamp() - time.time())))
            except asyncio.TimeoutError:
                await qr.recreate()
                continue
            except SessionPasswordNeededError:
                signed_in = True  # 扫码已通过，会话留给两步验证那一步
                return {"requires_password": True, "session_name": _session_name(session_name)}
            signed_in = True
            return {
                "requires_password": False,
                "account": {
                    "id": getattr(me, "id", None),
                    "username": getattr(me, "username", "") or "",
                    "first_name": getattr(me, "first_name", "") or "",
                    "last_name": getattr(me, "last_name", "") or "",
                    "phone": getattr(me, "phone", "") or "",
                    "session_name": _session_name(session_name),
                },
            }
    finally:
        await _graceful_disconnect(client)
        if not signed_in:  # 放弃或过期：没授权的会话文件不留
            Path(_session_path(_session_name(session_name)) + ".session").unlink(missing_ok=True)


async def verify_login_password(password: str, session_name: str = "") -> dict:
    client = build_client(session_name)
    await client.connect()
    try:
        me = await client.sign_in(password=(password or "").strip())
        return {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", "") or "",
            "first_name": getattr(me, "first_name", "") or "",
            "last_name": getattr(me, "last_name", "") or "",
            "phone": getattr(me, "phone", "") or "",
            "session_name": _session_name(session_name),
        }
    finally:
        await _graceful_disconnect(client)


async def logout_account(session_name: str = "") -> None:
    client = build_client(session_name)
    await client.connect()
    try:
        if await client.is_user_authorized():
            await client.log_out()
    finally:
        await _graceful_disconnect(client)
