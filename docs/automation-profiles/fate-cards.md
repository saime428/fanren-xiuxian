# 档案：天机命脉 + 噬金虫进化（fate_cards/）

**目的**：六个账号、每个洞府身份，每天完成一次天机命脉并验命领取天机残痕（用户 2026-09-26 要求“解析天机命脉，推理最佳逻辑实现所有账号自动化”），
并把每天 5 局噬金虫进化全部打完（用户 2026-09-26 要求“噬金虫要打满”）。

**入口**：洞府 → 外府 → 游历杂务 → 天机命脉（`dwelling/external action=fate_cards`，子页面 `/miniapp/xianxia-fate-cards`，token 前缀 `fate_`）。
代码 `fate_cards/`：`protocol.py`（接口、噬金虫规则与贪心策略）、`adapter.py`（按身份铸 token 的客户端）、`runtime.py`（单身份流程与命择策略）、
`run_cycle.py`（LaunchAgent 入口）。LaunchAgent `ai.hermes.xiuxian.fate-cards.daily`（`scripts/ai.hermes.xiuxian.fate-cards.daily.plist`）。

## 协议（2026-09-26 解析，证据 `state/reports/2026-09-26-fate-cards/`）

`POST /api/miniapp/xianxia-fate-cards/<端点>`，body `{token, initData, ...}`：

| 端点 | 作用 |
| --- | --- |
| `start` | 只读：`challengeDate`、`traceBalance`、`questions[]`（修行/机缘/财运/因缘/宗门/劫数）、`choices[]`（含 `rewardTrace`、`risk`）、`hasDrawn`、`record` |
| `draw{questionKey}` | 启三牌阵（前因/今时/后果），天机残痕 +1；重复调用返回 `alreadyDrawn` |
| `interpret` | 生成解读（AI 或固定文案），页面在命择前调用 |
| `choose{choiceKey}` | **不可更改**；生成 `record.quest{metric,target,progress,canSettle,status,expiresAt}` |
| `settle` | 任务完成后领奖；重复调用返回 `alreadySettled`；未完成为 `quest_incomplete` |

| 命择 | key | 奖励 | 任务 |
| --- | --- | ---: | --- |
| 逆势改命 | `defy` | 4 | 一次噬金虫挑战 |
| 顺势承命 | `accept` | 2 | `cultivation_gain` 今日获得 30 点修为（09-26 六个号启牌后立即 30/30） |
| 藏锋避劫 | `hide` | 1 | `wait_seconds` 180 |

噬金虫进化（`beast_merge`，`/miniapp/xianxia-beast-merge`，token 前缀 `beastmerge_`）：`start`（只读，`attempts{used,limit=5}`）→ `run/start` →
`move{runToken,column,seq}`（服务器推演，5×7 棋盘，同阶相邻合成，最多 160 步）→ `submit{runToken}`（服务器复核，`run_too_fast` 拒绝过快的局）。
每局交局本身也发天机残痕（09-26 实测一局 160 步、11525 分、8 阶虚空虫母、+3）。

## 最佳逻辑

1. 未启牌 → `draw`（问天主题固定"修行"，主题不影响奖励）→ `interpret`（失败不阻断）。
2. 命择（`runtime.decide_choice`）：奖励最高的逆势改命可以由脚本完成，但任务的确切指标只有命择后才可见、且不可更改，
   所以策略存于 `state/fate_cards/policy.json`：
   - `canary`（初始）：只有试点号 @demo_player_01（tg9100000001）选逆势改命，其余选顺势承命；
   - 试点号的逆势任务一旦验命 → `verified`，此后所有号都选逆势改命；
   - 当天剩余的噬金虫局数全部打完逆势任务仍未达成 → `failed`，全体保持顺势承命，证据（任务指标、局分）写入 policy；
   - 当天噬金虫局数已用完时不选逆势改命。
3. 任务：逆势 → 按固定 0.8 秒/步下完一局噬金虫（贪心：先得分，再留空格，再低列；无随机），交局后重读任务，
   直到任务可验命或当天局数用完（反正都要打满，逆势任务可以用上每一局）；藏锋 → 等满 180 秒；顺势 → 修为达标即可。
   `canSettle` 为真才 `settle`。
4. 回包不确定的 `choose` 只重读 `start` 的 `record.choiceKey` 判断，绝不重发；`draw/settle` 由服务器幂等。

## 噬金虫打满（2026-09-26 用户要求）

每个身份的天机命脉命择之后（`run_cycle.run_account` → `runtime.fill_beast_runs`），把服务器 `beast start`
的 `attempts.limit - attempts.used` 局全部打完：同一贪心策略、固定 0.8 秒/步（服务器 `run_too_fast` 的节奏规则，不随机），
一局约 160 步、5 分钟。剩余局数**只看服务器计数**，所以手动打过的局、重启、失败重跑都不会多打。

- 顺序：必须先命择，逆势改命的任务先用局数，剩下的再打满；命择一直失败时，21:00 以后也照样打满（`BEAST_WITHOUT_CHOICE_FROM`）。
- 青元子：若一局可能延续到 13:36–13:52 窗口内（`BEAST_RUN_BUDGET_SECONDS` 6 分钟），就不开新局，状态 `yielding_to_qyz`，窗口后续打。
- `move` / `submit` 回包丢失（传输失败）时按 2 / 5 / 15 秒重发**同一请求**（同一 seq / runToken，`SAME_REQUEST_RETRY_SECONDS`），`run/start` 绝不重发（重发会新开一局）；服务器明确拒绝（如 `run_too_fast`）不重发。09-26 @demo_player_04 一局下到约 120 步时一次 `move` 超时、紧接着 `submit` 也失败，这局的分数和残痕没有记上，才加了这条。
- 仍然失败记为 `beast.status = failed`，账号保持待办，15 分钟后重读计数续打；`attempts` 字段缺失记为 `unavailable`，不算打满。
- 账号汇总多一个 `beast_status`（`full` / `pending`）；只有 `status = daily_complete` 且 `beast_status = full` 才算当天做完。
- 每局结果写入 `state/accounts/<账号>/fate_cards/beast/<playerId>.json`（`day`、`runs[]{score,max_tier,trace,moves,purpose(defy/fill),at}`）。
- 通知 `miniapp_shared/summaries.beast_notification` → `【噬金虫进化】`，全部账号打满后发一次（键 `<日期>:beast:complete`），
  23:45 仍未打满发日终统计；每行 `N/5 局，今日自动 k 局 <分数>，最高 X 阶，天机残痕 +Y`。

2026-09-26 首日：天机命脉早已验命，12:09 起按新逻辑补打当天剩余局数（@demo_player_01 研究时手动打过 1 局，补 4 局；其余每号 5 局），
每局实测 160 步、约 5.3 分钟、+3 天机残痕（前两局 11530 分 8 阶、8995 分 7 阶）。

## 调度与状态

03:00 起 Asia/Shanghai（问心塔 02:00、天机试炼补跑 02:30–02:55 之后），账号 0/3/6/9/12/15 分钟放行，串行；加上噬金虫每号约 27 分钟，
一个 tick 约到 05:45 结束；5 分钟 tick（上一个 tick 还在跑时 launchd 不会再起）；任务或噬金虫未完成 15 分钟后重试；13:36–13:52 让位青元子。
状态：`state/accounts/<账号>/fate_cards/summary.json`、噬金虫每局记录 `.../fate_cards/beast/<playerId>.json`、全队 `state/fate_cards/summary.json`、
策略 `state/fate_cards/policy.json`。通知 `miniapp_shared/summaries.fate_notification` → `【天机命脉】`，每天一次（全部完成或 23:45 日终），
`beast_notification` → `【噬金虫进化】`，同样每天一次。

**首日（2026-09-26）**：六个号当天已手动选了顺势承命，自动化于 04:35 前后为六个号验命，各 +2；LaunchAgent 同日安装。
逆势改命的试点从 09-27 03:00 @demo_player_01 开始。

**测试**：`.venv/bin/python -m pytest -q fate_cards`；命令 `python3 -m fate_cards.run_cycle --check / --dry-run / --force / --account tgXXXX`。
