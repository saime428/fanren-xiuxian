# MiniApp daily flows: 星宫灵圃 · 琉璃问心塔 · 游历 (2026-09-19)

Three new script-only flows share one audited dwelling chain
(`miniapp_shared/dwelling.py`): dedicated Telethon session copy → the newest
pinned/announced 洞府公共入口 button in the game groups (`@example_game_group`
or `-1009400000005`; the entry rotates ~monthly, discovery follows it, see
`fishing/miniapp_fishing_integration.py::_dwelling_entry_candidates`) →
`RequestMainWebView` initData →
`POST /api/miniapp/xianxia-dwelling/start` → per-`playerId`
`POST /api/miniapp/xianxia-dwelling/external` child token. No `.观星台`,
`.闯塔` or `.野外历练` group commands are sent; the upstream client retired them.

| Flow | Package | LaunchAgent | Accounts | Endpoints |
| --- | --- | --- | --- | --- |
| 星宫灵圃 (牵引天雷星 / 一键安抚 / 一键收集) | `star_observatory/` | `ai.hermes.xiuxian.star-observatory.send` | tg9100000004 @demo_player_04, tg9100000005 @demo_player_05 | `xianxia-sect-farm/start`, `xianxia-sect-farm/action` |
| 琉璃问心塔 | `tower_miniapp/` | `ai.hermes.xiuxian.tower-miniapp.daily` | all six business accounts, every dwelling identity | `xianxia-pagoda/start`, `xianxia-pagoda/challenge` |
| 游历·野外历练 谨慎 ×8 | `journey_miniapp/` | `ai.hermes.xiuxian.journey-miniapp.daily` | all six accounts; every identity, except @demo_player_01 where only the bound character 示例绑定角色 (playerId 9200000001) runs | `xianxia-dwelling/details`, `xianxia-dwelling/journey` |

Account identity is verified twice per run: `LiveAccountBridge` compares the
Telegram user id, and `verify_username` compares the `@username`. State lives
under `state/accounts/<account>/<flow>/`; fleet summaries under
`state/<flow>/summary.json`. initData, entry tokens, child tokens and launch
URLs are never persisted or logged.

## 星宫灵圃 (`star_observatory`)

* Decision order per cycle mirrors the official client: `soothe` (一键安抚,
  empty `plotKey`) when any plot is 星光黯淡/元磁紊乱 → `collect` (一键收集,
  empty `plotKey`) when any plot is 可收集/精华已成 → `pull` with
  `starName: 天雷星` into each empty plot → sleep until the earliest server
  `remainingSeconds` (+15 s grace). `nothing_to_soothe` / `nothing_ready` are
  benign no-ops; any other `actionResult.ok == false` stops the cycle with the
  server code (for example `cultivation_insufficient`).
* Only 天雷星 may be pulled (`star_not_allowed` otherwise) and only the
  configured accounts may run (`account_not_allowed`). Since 2026-09-20
  @demo_player_01 (tg9100000001) is included but scoped to the bound character 示例绑定角色
  (playerId 9200000001) via `star_observatory.state.IDENTITY_SCOPE`; the main
  soul 示例修士一 never runs. Other accounts use their own personal identity.
* A transport failure after a mutation persists `unknown_mutation_outcome`
  with the pre-action board. The next run re-reads the board; if it is
  unchanged inside the 30-minute hold window the flow waits, otherwise (or
  after the window) it decides again from the live board. Every action is
  validated server-side against that board, so nothing is replayed blindly.
* `next_due_at` is persisted, so the 5-minute tick opens Telegram only when a
  server timer has elapsed (typically once per ~36 h batch).

## 琉璃问心塔 (`tower_miniapp`)

* Admission gate 02:00 Asia/Shanghai with 0/2/4/6/8/10-minute account offsets
  (`tower_miniapp/runner.py`, `START_HOUR`; 21:00 → 00:00 → 00:20 → 00:35 on 2026-09-21 → 02:00 on 2026-09-23 at the operator's request; the 00:35 move was to
  follow the staggered timetable in `docs/automation-profiles/README.md`); retries
  every 15 minutes until it settles; a completed day is cached per identity and per account.
* Per identity: `start` → if `todayHighest`/`failedFloor`/`resetsToday` is
  non-zero the server already recorded today (`already_recorded`); else if
  `canChallenge` fire exactly one `challenge`. Success requires the reply's
  `replay` plus a terminal `state`; otherwise `start` is re-read up to six
  times (10 s apart) and only a server-recorded attempt is accepted
  (`reconciled_terminal`). An uncertain challenge is never fired twice on the
  same day (`mutation_uncertain` → read-only reconcile next tick).
* `xianxia-pagoda/reset` is not in the allowlist and cannot be called.

## 游历 (`journey_miniapp`)

* Triggers: `StartCalendarInterval` 00:15/18/21/24/27/30 Asia/Shanghai (one per
  account; `journey_miniapp.runner.admitted` gates each account at its 3-minute
  offset) plus a 5-minute `StartInterval`. Every in-scope identity runs `wild_experience`
  with `mode: cautious` (谨慎) until the server `dailyCount` reaches 8 (or
  the server `dailyLimit` if lower). Scope is `journey_miniapp.state.IDENTITY_SCOPE`:
  all identities of five accounts, and on @demo_player_01 only 示例绑定角色 (matched by
  playerId 9200000001, dao-name fallback). If a scoped account no longer
  lists its identity the run fails loudly (`scope_identity_missing`).
* Between attempts the flow obeys the server: `available == false` or
  `remainingSeconds`/`readyAt` → persist `next_due_at` and stop;
  `wild_experience_daily_limit` → day complete; `wild_experience_unavailable`
  → cooldown. An attempt counts only when the refreshed server counter equals
  `before + 1` (one read-only re-check is allowed when the reply omits it).
* Just after midnight a full counter with an overdue or missing `resetAt` is
  treated as `awaiting_reset` (retry in 2 minutes), never as today's
  completion. When the server reports a future `resetAt`, a completed quota
  is held only until that reset (+60 s), so a non-midnight server boundary
  still yields the eight attempts of each server day.

## Operations

### Completion notifications

Each flow sends one operator summary through the same Hermes channel the
recipe-notice monitor uses (`hermes send --to telegram:9100000001`, override
with `MINIAPP_NOTIFY_TARGET` / `HERMES_SEND_BIN`):

* 琉璃问心塔 and 游历: one message per Asia/Shanghai day when every account is
  complete (`【琉璃问心塔】… 全部账号完成 6/6`, `【游历·谨慎】… 全部账号完成 6/6`),
  listing each identity's result; if some account never completes, one
  day-end partial summary after 23:45.
* 星宫灵圃: one message per maintenance batch once both accounts have settled
  on the server timer and at least one performed a confirmed action
  (安抚/收集/牵引 counts, plot state, next wake).
* 灵溪垂钓: one message per day when every configured account reaches its 5
  casts (`fishing/run_miniapp_fishing_cycle.py` → `miniapp_shared.summaries.fishing_notification`),
  plus a day-end partial after 23:45; keys live in `fishing/state/miniapp_fishing/notifications.json`.
* 青元子: one fleet battle report per event from `qingyuanzi-new-source/qyz_battle_report.py`
  (LaunchAgent `ai.hermes.xiuxian.qyz-battle-report`, 60 s tick), sent when all six accounts are
  terminal for the latest entry or 15 minutes after the entry; see
  `docs/qyz-battle-review-2026-09-19.md`.

Delivery keys are recorded in `state/<flow>/notifications.json`; a failed
delivery is retried on the next tick with the same key, never duplicated.
Since 2026-09-22 `deliver_with_hermes` also retries a non-zero `hermes send` exit up to 3 times (5 s apart) within the
same tick, because the gateway's Telegram path flaps for 10–20 s at a time; an exit-0 result without a confirmed
receipt is never retried. The recorded failure text is the CLI's JSON `error`, not its startup banner.

```bash
cd /Users/operator/.hermes/workspace/projects/xiuxian
./.venv/bin/python3 -m unittest miniapp_shared.test_miniapp_shared \
  star_observatory.test_protocol star_observatory.test_runtime star_observatory.test_adapter \
  star_observatory.test_runner star_observatory.test_coordinator star_observatory.test_entrypoint \
  star_observatory.test_state tower_miniapp.test_protocol tower_miniapp.test_adapter \
  tower_miniapp.test_runtime tower_miniapp.test_runner tower_miniapp.test_coordinator \
  tower_miniapp.test_state journey_miniapp.test_protocol journey_miniapp.test_runtime \
  journey_miniapp.test_runner
./.venv/bin/python3 -m star_observatory.run_cycle --check      # offline
./.venv/bin/python3 -m star_observatory.run_cycle --dry-run    # read-only live
./.venv/bin/python3 -m tower_miniapp.run_cycle --dry-run
./.venv/bin/python3 -m journey_miniapp.run_cycle --dry-run
./.venv/bin/python3 -m tower_miniapp.run_cycle --force --account tg9100000005   # one account now
scripts/install_miniapp_daily_launchagents.sh install|status|uninstall
```

`scripts/xiuxian_healthcheck.py` treats the three labels as helpers: not
loaded, disabled or a non-zero last exit is `bad`. The runners exit non-zero
only for account-level failures (authentication, identity mismatch, dwelling
entry), never for a server cooldown or an already-completed day.
