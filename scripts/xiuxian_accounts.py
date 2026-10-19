#!/usr/bin/env python3
"""Add your own accounts, choose each account's features, generate and install the jobs.

    python3 scripts/xiuxian_accounts.py features                 # what can be enabled
    python3 scripts/xiuxian_accounts.py add tg<id> --username my_name
    python3 scripts/xiuxian_accounts.py enable tg<id> shendubiguan fishing \\
        'jiyin_choice={"reply": ".献上魂魄"}'
    python3 scripts/xiuxian_accounts.py game --group <group> --chat-id -100… --topic-id … --second-chat-id -100…
    python3 scripts/xiuxian_accounts.py login tg<id>        # once per account (Telegram code prompt)
    python3 scripts/xiuxian_accounts.py render                   # regenerate after every change
    python3 scripts/xiuxian_accounts.py install --launchd        # macOS; --systemd on Linux

Everything personal lives in the gitignored config/accounts/: accounts.json (this
tool's config), <account>.env (API id/hash), and what render derives from it —
active_flows.json, game.env, launchd/<label>.plist — plus the per-account
wrappers scripts/*_<account>.sh. Each feature's wrappers, jobs and registry
entries are copied from the tracked demo account that runs it, with only the
account, username, credential source and the feature's options replaced.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = "/Users/operator/.hermes/workspace/projects/xiuxian"
LABEL_PREFIX = "ai.hermes.xiuxian."
PLIST_DIRS = ("scripts", "qingyuanzi-new-source", "fishing")


def demo_ids() -> set[str]:
    """The tracked demo accounts and credential sources: their files are already in the repo."""
    rows = demo_rows().values()
    return {row["id"] for row in rows} | {row["credential_source"] for row in rows}

# The demo account whose tracked files and registry entries are each feature's template.
TEMPLATE = {
    "shendubiguan": "tg9100000002", "yuanyingchuqiao": "tg9100000002", "tanxunliefeng": "tg9100000002",
    "wendao": "tg9100000006", "xuexishanlin": "tg9100000002", "zhaohuanmoying": "tg9100000002",
    "shenji_budao": "tg9100000001", "fumofabao_xuantianzhanlingjian": "tg9100000001",
    "fumofabao_qingzhufengyunjian_shenlei": "tg9100000006", "jiaohuan_fabao": "tg9100000002",
    "jiaohuan_gongfa": "tg9100000001", "jiyin_choice": "tg9100000002", "xuangu_zuoda": "tg9100000002",
    "shuangxiu_wenyang": "tg9100000001", "treasure_hunt": "tg9100000002",
    "tianxing_exploration": "tg9100000001", "yuanshen": "tg9100000001", "qingyuanzi": "tg9100000002",
}
# A feature's flows[] name when it differs from the feature key.
REGISTRY_FLOW = {"qingyuanzi": "qyz_miniapp"}
# Account-independent jobs: on when any account enables the feature (None: always on).
SHARED_HELPERS = {
    "account-lifecycle.monitor": "account_lifecycle",
    "miniapp-fishing.send": "fishing",
    "miniapp-fishing.daily-reset": "fishing",
    "qyz-battle-report": "qingyuanzi",
    "star-observatory.send": "star_observatory",
    "tower-miniapp.daily": "tower",
    "journey-miniapp.daily": "journey",
    "fate-cards.daily": "fate_cards",
    "sect-daily": "sect_daily",
    "pavilion-loop.send": "pavilion",
    "return-deadline-dispatch": None,
    "log-retention": None,
}
JIYIN_REPLIES = (".收敛气息", ".献上魂魄")
NOTES = {
    "jiyin_choice": '选项 reply：".收敛气息"（默认）或 ".献上魂魄"',
    "shuangxiu_wenyang": "必填 partner：另一个已添加账号的 id；只能一个账号开",
    "fumofabao_qingzhufengyunjian_shenlei": "可选 interval_dispatch_seconds（≥60，改由截止调度器拉起）",
    "fumofabao_xuantianzhanlingjian": "只能一个账号开",
    "yuanshen": "只能一个账号开",
    "tianxing_exploration": "只能一个账号开",
    "jiaohuan_fabao": "与 jiaohuan_gongfa 二选一",
    "jiaohuan_gongfa": "与 jiaohuan_fabao 二选一",
    "fishing": "可选 special_bait: true（允许在 fishing/state/miniapp_fishing.json 给它配特殊饵）",
    "pavilion": '可选 routes：{"侍妾名": "固定航线"}',
    "qingyuanzi": "可选 entry_discovery: true（由它扫战斗入口，默认第一个开的账号）",
    "fate_cards": "可选 canary: true（先试逆势改命，默认第一个开的账号）",
    "account_lifecycle": "可选 listener: true（用它的会话监听，默认第一个开的账号）",
}


# ---------------------------------------------------------------- config file

def accounts_module():
    from miniapp_shared import accounts
    return accounts


def config_file() -> Path:
    return Path(os.environ.get("XIUXIAN_ACCOUNTS_FILE") or accounts_module().USER_PATH)


def load_config() -> dict[str, Any]:
    path = config_file()
    if not path.is_file():
        return {"game": dict(accounts_module().GAME_DEFAULTS), "accounts": []}
    return json.loads(path.read_text(encoding="utf-8"))


def check_config(payload: dict[str, Any]) -> list[dict[str, Any]]:
    acc = accounts_module()
    rows = acc.validate(payload)
    acc.game_settings(payload)
    acc.notify_settings(payload)
    for row in rows:
        options = row["features"]
        reply = options.get("jiyin_choice", {})
        if isinstance(reply, dict) and reply.get("reply", JIYIN_REPLIES[0]) not in JIYIN_REPLIES:
            raise ValueError(f"{row['id']}: jiyin_choice.reply must be one of {JIYIN_REPLIES}")
        shuangxiu = options.get("shuangxiu_wenyang")
        if shuangxiu is not None and (not isinstance(shuangxiu, dict) or shuangxiu.get("partner") in (None, row["id"])):
            raise ValueError(f"{row['id']}: shuangxiu_wenyang needs a partner account")
        interval = options.get("fumofabao_qingzhufengyunjian_shenlei", {})
        if isinstance(interval, dict) and "interval_dispatch_seconds" in interval and (
                type(interval["interval_dispatch_seconds"]) is not int or interval["interval_dispatch_seconds"] < 60):
            raise ValueError(f"{row['id']}: interval_dispatch_seconds must be an integer >= 60")
    return rows


def save_config(payload: dict[str, Any]) -> None:
    check_config(payload)
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def find_account(payload: dict[str, Any], account: str) -> dict[str, Any]:
    for row in payload["accounts"]:
        if row["id"] == account:
            return row
    raise SystemExit(f"unknown account {account}; add it first")


# ---------------------------------------------------------------- rendering

def _token(rest: str) -> str:
    return re.split(r"[.-]", rest, maxsplit=1)[0]


def _file_token(name: str) -> str | None:
    """The flow a per-account file belongs to (``name`` has the account as ``{a}``)."""
    match = re.fullmatch(r"(?:send|watch|run)_(.+?)(?:_return_observer)?_\{a\}\.sh", name)
    if match:
        return match[1]
    match = re.fullmatch(re.escape(LABEL_PREFIX) + r"\{a\}\.(.+)\.plist", name)
    return _token(match[1]) if match else None


class Renamer:
    """Rewrites a template account's text for another account."""

    def __init__(self, template: dict[str, Any], target: dict[str, Any], options: dict[str, Any]):
        self.t, self.a = template, target
        self.options = options

    def __call__(self, text: str) -> str:
        t, tc, a, ac = self.t["id"], self.t["credential_source"], self.a["id"], self.a["credential_source"]
        if tc != t:  # back to the plain form: the account signs in with its own source
            text = text.replace(f"source ./config/accounts/{tc}.env\nexport XIU_ACCOUNT_SLUG='{t}'\n"
                                f"export TG_SESSION_FALLBACK_NAME='state/{tc}'\n",
                                f"source ./config/accounts/{t}.env\n")
            # Other mentions (comments) of the source follow the target's own source.
            text = text.replace(f"'{t}' '{tc}'", f"'{t}' '{t}'").replace(tc, "{cred}")
        if ac != a:
            text = text.replace(f"source ./config/accounts/{t}.env\n",
                                f"source ./config/accounts/{ac}.env\nexport XIU_ACCOUNT_SLUG='{a}'\n"
                                f"export TG_SESSION_FALLBACK_NAME='state/{ac}'\n")
            text = text.replace(f"'{t}' '{t}'", f"'{a}' '{ac}'")
        text = text.replace(t, a)
        text = re.sub(rf"(?<!\d){t[2:]}(?!\d)", a[2:], text).replace("{cred}", ac)
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(self.t['username'])}(?![A-Za-z0-9_])", self.a["username"], text)
        reply = self.options.get("reply")
        if reply:
            text = text.replace(f"'{JIYIN_REPLIES[0]}'\n", f"'{reply}'\n").replace(f"'{JIYIN_REPLIES[1]}'\n", f"'{reply}'\n")
        return text


def demo_rows() -> dict[str, dict[str, Any]]:
    acc = accounts_module()
    return {row["id"]: row for row in acc.load_accounts(acc.EXAMPLE_PATH)}


def demo_registry(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / "config/active_flows.json").read_text(encoding="utf-8"))


def template_files(root: Path, feature: str) -> dict[str, Path]:
    """``{name with {a}: path}`` of the template account's tracked files for ``feature``."""
    template = TEMPLATE[feature]
    flow = REGISTRY_FLOW.get(feature, feature)
    files = {}
    for directory in ("scripts",):
        for path in sorted((root / directory).iterdir()):
            if template in path.name and _file_token(path.name.replace(template, "{a}")) in (feature, flow):
                files[path.name.replace(template, "{a}")] = path
    return files


def render_account(row: dict[str, Any], feature: str, demo: dict[str, Any], root: Path = ROOT):
    """(files, flow entries, helper entries) of one account's feature, from its template."""
    template = demo_rows()[TEMPLATE[feature]]
    options = row["features"].get(feature)
    rename = Renamer(template, row, options if isinstance(options, dict) else {})
    flow = REGISTRY_FLOW.get(feature, feature)
    files = {name.replace("{a}", row["id"]): rename(path.read_text(encoding="utf-8"))
             for name, path in template_files(root, feature).items()}

    def clone(entry: dict[str, Any]) -> dict[str, Any]:
        # ponytail: business_actor/credential_source are notes no code reads; kept only
        # where the template entry carries them, so the demo entries render back exactly.
        entry = deepcopy(entry)
        noted = entry.pop("business_actor", None) is not None
        entry.pop("credential_source", None)
        entry = json.loads(rename(json.dumps(entry, ensure_ascii=False)))
        if noted and row["credential_source"] != row["id"]:
            entry.update(business_actor=row["username"], credential_source=row["credential_source"])
        return entry

    flows = [clone(entry) for entry in demo["flows"]
             if entry["account"] == template["id"] and entry["flow"] == flow and entry.get("enabled", True)]
    if isinstance(options, dict) and "interval_dispatch_seconds" in options:
        flows = [{**entry, "interval_dispatch_seconds": options["interval_dispatch_seconds"]} for entry in flows]
    prefix = f"{LABEL_PREFIX}{template['id']}."
    helpers = [clone(entry) for entry in demo["helpers"]
               if entry["label"].startswith(prefix) and _token(entry["label"][len(prefix):]) in (feature, flow)
               and entry.get("enabled", True)]
    return files, flows, helpers


def render(rows: list[dict[str, Any]], game: dict[str, Any], root: Path = ROOT) -> dict[str, str]:
    """Every generated file, ``{project-relative path: content}``."""
    demo = demo_registry(root)
    files: dict[str, str] = {}
    flows, account_helpers = [], []
    enabled = {feature for row in rows for feature in row["features"]}
    for row in rows:
        for feature in row["features"]:
            if feature not in TEMPLATE:
                continue
            account_files, account_flows, helpers = render_account(row, feature, demo, root)
            for name, text in account_files.items():
                files[("config/accounts/launchd/" if name.endswith(".plist") else "scripts/") + name] = text
            flows += account_flows
            account_helpers += helpers
    shared = [entry for entry in demo["helpers"] if entry["label"].removeprefix(LABEL_PREFIX) in SHARED_HELPERS
              and SHARED_HELPERS[entry["label"].removeprefix(LABEL_PREFIX)] in enabled | {None}]
    registry = {"helpers": shared + account_helpers, "flows": flows}
    files["config/accounts/active_flows.json"] = json.dumps(registry, ensure_ascii=False, indent=2) + "\n"
    files["config/accounts/game.env"] = (
        "# Generated by scripts/xiuxian_accounts.py render from accounts.json; edit that instead.\n"
        f"XIU_GAME_TARGET='@{game['group']}'\nXIU_GAME_TOPIC_ID='{game['topic_id']}'\n"
        f"XIU_GAME_SECOND_CHAT_ID='{game['second_chat_id']}'\n")
    return files


def write_rendered(files: dict[str, str], root: Path = ROOT) -> list[str]:
    """Write ``files`` under ``root``, deleting what the previous render wrote and this one did not."""
    manifest_path = root / "config/accounts/rendered.json"
    old = set(json.loads(manifest_path.read_text())) if manifest_path.is_file() else set()
    for stale in sorted(old - set(files)):
        (root / stale).unlink(missing_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        os.chmod(path, 0o755 if name.endswith(".sh") else 0o600)
    manifest_path.write_text(json.dumps(sorted(files), indent=1) + "\n")
    return sorted(files)


# ---------------------------------------------------------------- installing

def job_plists(out: Path = ROOT) -> dict[str, dict[str, Any]]:
    """``{label: plist}`` for every scheduled label of the generated registry, paths rewritten.

    Account jobs come from the rendered config/accounts/launchd/, shared jobs from the tracked plists.
    """
    from flow_registry import active_entries, launchagent_labels
    registry = json.loads((out / "config/accounts/active_flows.json").read_text(encoding="utf-8"))
    plists = {}
    for label in launchagent_labels(active_entries(registry)):
        candidates = [out / "config/accounts/launchd" / f"{label}.plist",
                      *(ROOT / directory / f"{label}.plist" for directory in PLIST_DIRS)]
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise SystemExit(f"no plist for {label}")
        text = source.read_text(encoding="utf-8").replace(DEMO_ROOT, str(ROOT))
        plists[label] = plistlib.loads(text.encode())
    return plists


def _systemd_quote(arg: str) -> str:
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def systemd_units(label: str, plist: dict[str, Any]) -> dict[str, str]:
    """A .service (plus .timer unless it is KeepAlive) equivalent to one LaunchAgent."""
    log = plist.get("StandardOutPath") or plist.get("StandardErrorPath")
    service = ["[Unit]", f"Description={label}", "", "[Service]",
               f"WorkingDirectory={plist.get('WorkingDirectory', ROOT)}",
               "Environment=XIU_SCHEDULER=systemd",
               "ExecStart=" + " ".join(_systemd_quote(arg) for arg in plist["ProgramArguments"])]
    if log:
        service.append(f"StandardOutput=append:{log}")
    if plist.get("StandardErrorPath"):
        service.append(f"StandardError=append:{plist['StandardErrorPath']}")
    units = {}
    if plist.get("KeepAlive"):
        service += ["Restart=always", f"RestartSec={max(30, int(plist.get('ThrottleInterval', 30)))}",
                    "", "[Install]", "WantedBy=default.target"]
    else:
        service.append("Type=oneshot")
        timer = ["[Unit]", f"Description={label} schedule", "", "[Timer]", "AccuracySec=1s"]
        interval = plist.get("StartInterval")
        if interval:
            timer += [f"OnActiveSec={1 if plist.get('RunAtLoad') else int(interval)}",
                      f"OnUnitActiveSec={int(interval)}"]
        calendar = plist.get("StartCalendarInterval")
        for spec in ([calendar] if isinstance(calendar, dict) else calendar or []):
            weekday = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[spec["Weekday"]] + " " \
                if "Weekday" in spec else ""
            # The game day is Beijing time whatever the machine's timezone is.
            hour = f"{spec['Hour']:02}" if "Hour" in spec else "*"
            timer.append(f"OnCalendar={weekday}*-*-* {hour}:{spec.get('Minute', 0):02}:00 Asia/Shanghai")
        if not interval and not calendar:
            timer.append("OnActiveSec=1")
        timer += ["", "[Install]", "WantedBy=timers.target"]
        units[f"{label}.timer"] = "\n".join(timer) + "\n"
    units[f"{label}.service"] = "\n".join(service) + "\n"
    return units


def _log_dirs(plist: dict[str, Any]) -> None:
    for key in ("StandardOutPath", "StandardErrorPath"):
        if isinstance(plist.get(key), str):
            Path(plist[key]).parent.mkdir(parents=True, exist_ok=True)


def install(scheduler: str, dest: Path | None, dry_run: bool, out: Path = ROOT) -> None:
    plists = job_plists(out)
    installed_path = out / "config/accounts/installed.json"
    previous = set(json.loads(installed_path.read_text())) if installed_path.is_file() else set()
    run = (lambda argv: print("+", " ".join(argv))) if dry_run else (
        lambda argv: subprocess.run(argv, check=False))
    if scheduler == "launchd":
        dest = dest or Path.home() / "Library/LaunchAgents"
        domain = f"gui/{os.getuid()}"
        for label in sorted(previous - set(plists)):
            run(["launchctl", "bootout", f"{domain}/{label}"])
            (dest / f"{label}.plist").unlink(missing_ok=True)
        for label, plist in plists.items():
            _log_dirs(plist)
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{label}.plist").write_bytes(plistlib.dumps(plist))
            run(["launchctl", "bootout", f"{domain}/{label}"])
            run(["launchctl", "bootstrap", domain, str(dest / f"{label}.plist")])
            run(["launchctl", "enable", f"{domain}/{label}"])
    else:
        dest = dest or Path.home() / ".config/systemd/user"
        dest.mkdir(parents=True, exist_ok=True)
        for label in sorted(previous - set(plists)):
            run(["systemctl", "--user", "disable", "--now", f"{label}.timer", f"{label}.service"])
            for suffix in (".timer", ".service"):
                (dest / f"{label}{suffix}").unlink(missing_ok=True)
        units = []
        for label, plist in plists.items():
            _log_dirs(plist)
            for name, text in systemd_units(label, plist).items():
                (dest / name).write_text(text, encoding="utf-8")
            units.append(f"{label}.service" if plist.get("KeepAlive") else f"{label}.timer")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", *units])
        print("提示：执行 `loginctl enable-linger $USER`，退出登录后任务才会继续跑。")
    installed_path.write_text(json.dumps(sorted(plists), indent=1) + "\n")
    print(f"{scheduler}: {len(plists)} 个任务 → {dest}")


# ---------------------------------------------------------------- commands

def cmd_features(_args) -> None:
    acc = accounts_module()
    for key, name in acc.FEATURES.items():
        note = NOTES.get(key, "")
        print(f"{key:40} {name}" + (f"  —— {note}" if note else ""))


def cmd_list(_args) -> None:
    payload = load_config()
    if not payload["accounts"]:
        print("还没有账号；先 add。")
    for row in payload["accounts"]:
        source = row.get("credential_source")
        print(f"{row['id']} @{row['username']}" + (f"（凭据 {source}）" if source else ""))
        for feature, value in row.get("features", {}).items():
            print(f"    {feature}" + ("" if value is True else f" {json.dumps(value, ensure_ascii=False)}"))


def cmd_add(args) -> None:
    payload = load_config()
    if args.account in demo_ids():
        raise SystemExit(f"{args.account} 是仓库示例账号，请用你自己的 tg<Telegram 用户 id>")
    if any(row["id"] == args.account for row in payload["accounts"]):
        raise SystemExit(f"{args.account} already exists")
    row = {"id": args.account, "username": args.username.lstrip("@"), "features": {}}
    if args.credential_source:
        row["credential_source"] = args.credential_source
    payload["accounts"].append(row)
    save_config(payload)
    source = args.credential_source or args.account
    env_path = config_file().parent / f"{source}.env"
    if not env_path.exists():
        env_path.write_text(f"export XIU_ACCOUNT_SLUG='{source}'\n\n# From https://my.telegram.org\n"
                            "export TG_API_ID=''\nexport TG_API_HASH=''\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        print(f"已建 {env_path.relative_to(ROOT) if env_path.is_relative_to(ROOT) else env_path}：填入 TG_API_ID / TG_API_HASH 后执行 login")
    print(f"added {args.account}; 用 enable 选功能")


def cmd_remove(args) -> None:
    payload = load_config()
    find_account(payload, args.account)
    payload["accounts"] = [row for row in payload["accounts"] if row["id"] != args.account]
    save_config(payload)
    print(f"removed {args.account}; 执行 render 和 install 生效")


def _parse_feature(spec: str) -> tuple[str, Any]:
    name, sep, raw = spec.partition("=")
    if not sep:
        return name, True
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{name}: options must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{name}: options must be a JSON object")
    return name, value


def cmd_enable(args) -> None:
    payload = load_config()
    row = find_account(payload, args.account)
    for spec in args.features:
        name, value = _parse_feature(spec)
        row.setdefault("features", {})[name] = value
    save_config(payload)
    print(f"{args.account}: " + ", ".join(row["features"]))


def cmd_disable(args) -> None:
    payload = load_config()
    row = find_account(payload, args.account)
    for name in args.features:
        row.get("features", {}).pop(name, None)
    save_config(payload)
    print(f"{args.account}: " + (", ".join(row["features"]) or "（无功能）"))


def cmd_game(args) -> None:
    payload = load_config()
    game = payload.setdefault("game", dict(accounts_module().GAME_DEFAULTS))
    for key in ("group", "chat_id", "topic_id", "second_chat_id"):
        if getattr(args, key) is not None:
            game[key] = getattr(args, key).lstrip("@") if key == "group" else getattr(args, key)
    save_config(payload)
    print(json.dumps(game, ensure_ascii=False))


def cmd_notify(args) -> None:
    payload = load_config()
    payload["notify"] = {key: value for key, value in (("target", args.target), ("hermes_bin", args.hermes_bin)) if value}
    save_config(payload)
    print(json.dumps(payload["notify"], ensure_ascii=False))


def cmd_login(args) -> None:
    row = find_account(load_config(), args.account)
    source = row.get("credential_source", row["id"])
    from fishing.fishing_accounts import read_account_env
    env = read_account_env(source)
    if not env.get("TG_API_ID") or not env.get("TG_API_HASH"):
        raise SystemExit(f"先在 config/accounts/{source}.env 填 TG_API_ID / TG_API_HASH")
    try:
        os.environ.update({k: v for k, v in read_account_env("mtproto").items() if k not in os.environ})
    except FileNotFoundError:
        pass
    from transport import telegram_client

    async def login() -> None:
        client = telegram_client(str(ROOT / "state" / source), int(env["TG_API_ID"]), env["TG_API_HASH"])
        await client.start()
        me = await client.get_me()
        await client.disconnect()
        if me.id != int(row["id"][2:]) or (me.username or "").casefold() != row["username"].casefold():
            raise SystemExit(f"登录的是 id {me.id} @{me.username}，与配置的 {row['id']} @{row['username']} 不符")
        print(f"已登录 {row['id']} @{me.username}，会话 state/{source}.session")

    (ROOT / "state").mkdir(exist_ok=True)
    asyncio.run(login())


def cmd_render(args) -> None:
    payload = load_config()
    rows = check_config(payload)
    if not rows:
        raise SystemExit("还没有账号；先 add")
    if any(row["id"] in demo_ids() for row in rows):
        raise SystemExit("配置里有仓库示例账号，它们的文件已在仓库里，不能再生成")
    files = render(rows, accounts_module().game_settings(payload))
    # Validates the result against this deployment's accounts (miniapp scope, trade choice).
    from flow_registry import active_entries
    active_entries(json.loads(files["config/accounts/active_flows.json"]))
    for name in write_rendered(files, args.out):
        print(name)
    missing = [row["id"] for row in rows if not (args.out / accounts_module().DWELLING_SESSIONS[row["id"]]).is_file()]
    if missing:
        print("还没登录：" + " ".join(missing) + "（执行 login）")


def cmd_install(args) -> None:
    if not (args.out / "config/accounts/active_flows.json").is_file():
        raise SystemExit("先执行 render")
    install("launchd" if args.launchd else "systemd", args.dest, args.dry_run, args.out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("features", help="可选功能").set_defaults(func=cmd_features)
    sub.add_parser("list", help="已加的账号和功能").set_defaults(func=cmd_list)
    add = sub.add_parser("add", help="新增账号")
    add.add_argument("account", help="tg<Telegram 用户 id>")
    add.add_argument("--username", required=True, help="Telegram @用户名")
    add.add_argument("--credential-source", help="借用另一个 config/accounts/<id>.env 的 API 凭据")
    add.set_defaults(func=cmd_add)
    remove = sub.add_parser("remove", help="删除账号")
    remove.add_argument("account")
    remove.set_defaults(func=cmd_remove)
    for name, func in (("enable", cmd_enable), ("disable", cmd_disable)):
        command = sub.add_parser(name, help=f"{'开启' if name == 'enable' else '关闭'}功能")
        command.add_argument("account")
        command.add_argument("features", nargs="+", help="feature 或 feature='{\"选项\": 值}'")
        command.set_defaults(func=func)
    game = sub.add_parser("game", help="游戏群（命令只发到这个群的话题）")
    game.add_argument("--group", help="群 @用户名")
    game.add_argument("--chat-id", type=int)
    game.add_argument("--topic-id", type=int)
    game.add_argument("--second-chat-id", type=int, help="只读监听的第二个群")
    game.set_defaults(func=cmd_game)
    notify = sub.add_parser("notify", help="Hermes 通知（日报、战报）")
    notify.add_argument("--target", help="telegram:<你的用户 id>；不填即关闭")
    notify.add_argument("--hermes-bin")
    notify.set_defaults(func=cmd_notify)
    login = sub.add_parser("login", help="登录账号，生成 state/<id>.session")
    login.add_argument("account")
    login.set_defaults(func=cmd_login)
    render_cmd = sub.add_parser("render", help="按配置生成注册表、包装脚本和任务")
    render_cmd.set_defaults(func=cmd_render)
    install_cmd = sub.add_parser("install", help="安装定时任务")
    scheduler = install_cmd.add_mutually_exclusive_group(required=True)
    scheduler.add_argument("--launchd", action="store_true", help="macOS LaunchAgents")
    scheduler.add_argument("--systemd", action="store_true", help="Linux systemd 用户单元")
    install_cmd.add_argument("--dest", type=Path, help="写到这个目录（默认系统位置）")
    install_cmd.add_argument("--dry-run", action="store_true", help="只写文件、打印命令，不加载")
    install_cmd.set_defaults(func=cmd_install)
    for command in (render_cmd, install_cmd):
        command.add_argument("--out", type=Path, default=ROOT, help=argparse.SUPPRESS)  # tests only
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
