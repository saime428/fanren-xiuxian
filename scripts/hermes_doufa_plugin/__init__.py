"""No-model Hermes command for the existing, account-scoped duel campaign."""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path
from contextvars import copy_context

LABEL = "ai.hermes.xiuxian.doufa_demo_player_02.rotation"


def register(ctx) -> None:
    from hermes_constants import get_hermes_home
    root = Path(get_hermes_home()) / "workspace/projects/xiuxian"

    def handler(raw_args: str) -> str:
        try:
            return handle_slash(raw_args, root, str(ctx.get_config("owner_user_id", "")))
        except Exception:
            # Do not raise into gateway's plugin->agent fallback, or expose logs.
            return "斗法快捷命令未確認執行；請檢查本地插件／服務，不會轉交 AI 或自動重試。"

    ctx.register_command(
        "doufa", handler=handler,
        description="斗法：五帳號輪替（零 AI）；status 查看狀態",
        args_hint="[start|status]",
    )


def handle_slash(raw_args: str, root: Path, owner: str) -> str:
    # Only task-local gateway identity is authoritative. Never use process env
    # fallback: another chat/cron task can leave unrelated identity values there.
    source = {var.name: value for var, value in copy_context().items()}
    if not owner or not owner.isascii() or not owner.isdigit() or any([
        source.get("HERMES_SESSION_PLATFORM") != "telegram",
        source.get("HERMES_SESSION_CHAT_TYPE") != "dm",
        source.get("HERMES_SESSION_USER_ID") != owner,
        source.get("HERMES_SESSION_CHAT_ID") != owner,
    ]):
        return "斗法快捷命令只接受已設定擁有者的 Telegram 私聊；未執行任何操作。"
    return handle_local(raw_args, root)


def campaign_running() -> bool:
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("無法確認斗法服務狀態")
    if re.search(r"(?m)^\s*pid = [1-9][0-9]*\s*$", result.stdout):
        return True
    if "state = not running" in result.stdout:
        return False
    raise RuntimeError("斗法服務狀態不明")


def handle_local(raw_args: str, root: Path) -> str:
    try:
        mode = raw_args.strip().lower() or "start"
        if mode != "start":
            return _handle_local(raw_args, root)
        fd = os.open(root / "state/doufa_shortcut.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "斗法啟動處理中；不重複啟動，請用 /doufa status 查看。"
            return _handle_local(raw_args, root)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return "斗法狀態／啟動未確認，未作自動重試；請用 /doufa status 查核。"


def _handle_local(raw_args: str, root: Path) -> str:
    mode = raw_args.strip().lower() or "start"
    if mode not in {"start", "status"}:
        return "用法：/doufa 啟動；/doufa status 只查看狀態。兩者均不調用 AI。"
    running = campaign_running()
    if mode == "status":
        return "斗法執行中。" if running else "斗法未執行。"
    if running:
        return "斗法執行中；不重啟、不重複發送。"
    wrapper = root / "scripts/activate_doufa_demo_player_02.sh"
    result = subprocess.run(
        ["/bin/bash", str(wrapper)], cwd=root,
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode == 0 and campaign_running():
        return "斗法已啟動：五帳號輪替；全局至少 5 分鐘，每號每日最多 5 次官方結算。"
    return "斗法啟動未確認；請用 /doufa status 查核，不會盲目重試。"
