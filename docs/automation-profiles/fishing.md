# 档案：灵溪垂钓（fishing/）

**目的**：六个账号每天自动垂钓，只认官方结算；禁止鱼竿市场操作。
- **钓点**（2026-09-26 用户指定"钓鱼点按熟练度达标情况优先从高选择"）：所有账号 `pond = highest_unlocked`，每次下竿前
  由 `select_pond` 按 `shop.ponds[]` 选熟练度达标的最高钓点（青溪浅滩 0 / 灵眼寒潭 800 / 乱星海礁 2400）。
- **鱼饵**（2026-09-26 用户规则"除了demo_player_01其他保持凡饵"）：只有 @demo_player_01 用妖血饵；其余五个号**在任何钓点都用凡饵**，
  熟练度到 800 进入灵眼寒潭后也不换饵。`load_fishing_config` 强制执行：`auto.bait` 必须是 `plain`，`account_overrides`
  里只有 tg9100000001（`SPECIAL_BAIT_ACCOUNTS`）可以改 `bait`，违反时配置加载失败、不会下竿。
- 五个号（@demo_player_02 / @demo_player_03 / @demo_player_04 / @demo_player_05 / @demo_player_06）：凡饵、不打窝，每天 5 竿（服务器对它们的上限就是 5）；
  09-26 熟练度 516–655，仍在青溪浅滩。
- **@demo_player_01（2026-09-26 起，用户指定）**：熟练度 6156 → 乱星海礁、每竿都用**妖血饵**；每天打窝三次，顺序 **妖腥窝 → 灵草窝 → 灵草窝**；
  竿数不设本地上限，钓到服务器返回 `fishing_daily_limit_reached` 为止（三窝覆盖最多 16 竿，不等于服务器每日配额；2026-10-05 实际 15 竿后到上限）。
  这个号只有主号 示例修士一 一个身份（没有示例绑定角色），钓竿为金竹钓竿。

**入口**：`fishing/run_miniapp_fishing_cycle.py`（协调器，20 秒 tick，LaunchAgent `ai.hermes.xiuxian.miniapp-fishing.send`）；
`fishing/reset_miniapp_fishing_day.py`（0 点日重置，`ai.hermes.xiuxian.miniapp-fishing.daily-reset`）；
协议 `fishing/miniapp_fishing_integration.py`。

**配置**：`fishing/state/miniapp_fishing.json`。`auto` 是五个号共用的钓点/鱼饵；
`account_overrides.<账号>` 可设 `pond`、`bait`、`chum_plan`（按顺序的窝料 key 列表）、`daily_cast_limit`（整数，或 `"server"`）。
`account_options(config, account)` 合成单个账号实际使用的选项，协调器按账号传入。

## 协议结构（2026-09-25 只读解析，证据 `state/reports/2026-09-25-fishing-structure/demo_player_01-readonly.json`）

置顶洞府入口 → initData → `dwelling/start` → `dwelling/external action=fishing` 铸 `fish_` token，
之后都是 `POST /api/miniapp/xianxia-fishing/<端点>`，body 带 `token`、`initData`：

| 端点 | 作用 |
| --- | --- |
| `start` | 会话快照：`session.phase`（lobby / waiting / challenge / settled / missed / expired）、`pond`、`rod`、`bait`、`challenge` |
| `shop` | 鱼饵库存与价格（`baits[].count/cost`）、窝料（`chums[]`）、当前生效的窝 `activeChum {key,name,remaining}`、钓点解锁 |
| `buy-bait` | `{baitKey, quantity}` |
| `chum` | `{chumKey}` 打窝 |
| `next` | `{pondKey, baitItemId}` 下竿；当天用完返回 `fishing_daily_limit_reached` |
| `finish` / `result` | 提交 proof、读正式结算 |

窝料（`shop.chums[]`，字段 `key/name/cost/dailyLimit/usedToday/remainingToday/uses/affordable`）：

| key | 名称 | 消耗 | 管几竿 | 每日上限 |
| --- | --- | --- | ---: | ---: |
| `rice` | 米糠小窝 | 凡饵×2、灵石×30 | 4 | 2 |
| `grass` | 灵草窝 | **灵米饵×3**、凝血草×5 | 5 | 2 |
| `demon` | 妖腥窝 | 妖血饵×2、一阶妖丹×3、灵石×200 | 6 | 1 |

鱼饵 key：`plain` 凡饵、`spirit_rice` 灵米饵（灵石 35）、`spirit_worm` 灵虫饵、`demon_blood` 妖血饵（灵石 220 + 一阶妖丹 1）、`moon` 月华饵。
钓点 key：`qingxi` 青溪浅滩、`hantan` 灵眼寒潭（熟练度 800）、`luanxing` 乱星海礁（熟练度 2400）。

## 打窝逻辑（`UpstreamFishingProtocol._apply_chum_plan`）

每次在 lobby 下竿前读一次 `shop`：
1. `activeChum.remaining > 0` → 不打窝，直接下竿。
2. 否则按 `chum_plan` 顺序找第一个未完成的步骤：某 key 的第 n 步在服务器 `usedToday ≥ n` 时算完成。
   **只看服务器计数，不在本地记次数**，所以重启、回包丢失、当天手动打过窝，都不会多打。
3. 窝料要消耗的鱼饵不够时（灵草窝的灵米饵），先按差额 `buy-bait`，再刷新 `shop`。
4. `affordable=false` → 记为 `unaffordable`，这竿照常不带窝下竿，并在汇总里写"窝料不足"。
5. 发送 `chum` 后必定重读 `shop`：`usedToday` 增加或 `activeChum` 为该窝，才算 `applied`；
   否则记为 `chum_unconfirmed`，**本次不下竿**，60 秒后重试（重试时计数会说明窝到底有没有打上）。
6. 打完窝后再检查下竿用的鱼饵（妖腥窝本身会用掉 2 个妖血饵），妖血饵用完时按 `buy_bait` 每次补 10 个。

每次运行的结果带 `chum` 字段（`status / chum / bought / used_today / active`）；只收线、不读 `shop` 的运行，沿用当天上一次的 `chum` 记录。

## 调度与配额

0 点重置只改本地文件（归档昨日证据、清配额，保留跨午夜的退避）。首竿在 **00:00 首槽**：`admitted()` 按账号 0/2/4/6/8/10 分钟放行
（@demo_player_01 为 0），之后每竿按服务器 `retry_not_before_ts`。服务器返回 `external_action_rate_limited` 时按 5/15/60 分钟退避。
13:36–13:52 让位青元子。

配额判断用 `daily_quota_done()`：整数上限看本地结算计数；`"server"` 上限只在当天收到 `fishing_daily_limit_reached` 后算完成
（`server_daily_limit_reached`），本地计数最多到 40 作为保护上限。

**状态**：`fishing/state/miniapp_fishing/accounts/<账号>/fishing.json`（含 `daily_cast_count`、`daily_cast_limit`、`server_daily_limit_reached`、`chum`），
`fishing/state/miniapp_fishing/fishing_coordinator.json`（`running / rate_limited_wait / daily_complete`），`history/<日期>/` 归档，
`fishing/state/miniapp_fishing_locks/commands/<账号>/runs.jsonl` 每次运行一条脱敏记录。

**通知**：`miniapp_shared/summaries.fishing_notification` → `【灵溪垂钓】`，按账号显示 `5/5 竿` 或 `N 竿（服务器上限已到）`，
以及 `打窝 灵草窝×2、妖腥窝×1`。全部账号（含 @demo_player_01 钓到服务器上限）完成后才发，通常 01:00 前后。
当天汇总发出之后才完成的账号（例如当天中途加入）另发一次性的 `【灵溪垂钓】<日期> 补报`
（`fishing_late_notification`，键 `<日期>:late:<账号>`）；每天汇总与补报覆盖了哪些账号记在
`fishing/state/miniapp_fishing/reported_accounts.json`，没有记录的旧日期视为已全部汇报。
2026-09-26：@demo_player_01 在 00:27 五账号汇总之后才加入垂钓，它的 15 竿一直没有汇报（每个 tick 都是
`already_delivered key=2026-09-26:complete`）；11:55 以补报发出（message 34196）。

## 逐竿鱼获汇报（2026-09-28 用户要求“所有账号都要汇报每一竿具体情况，钓到了什么鱼”）

每一竿结算时（`_record_settlement`，三条结算路径都经过它）从官方 `/result` 回包提取鱼获（`fishing/fishing_catch.extract_catch`）：
鱼名（`result.fish.name`，兼容 `fish` 字符串 / `fishName` / 结算文案“竟是【…】”）、品阶（`rarityLabel`，或 `rarity` 键按
`session.rarityLabels` 换成 凡鱼 / 灵鱼 / 珍鱼 / 妖鱼 / 异种 / 空竿）、重量（`fish.weight`，两位小数，斤）、伴生奖励
（`rewards` / `bonusLoot`）、钓术经验（`expGain`）；服务器 `caught=false` 即空竿。字段写法参照同游戏的上游客户端
`references/upstream-fanrenxiuxian-auto/miniapp_fishing.py`，官方没有文档。

- 每竿鱼获（含结算时刻）按结算编号去重，累积在 `accounts/<账号>/fishing.json` 的 `catches`，0 点清空；
- `runs.jsonl` 每竿另存一份脱敏的原始回包 `result_sample`（≤3000 字符），供核对字段；
- 每日汇总与补报里，每个账号下逐竿列出 `1. 00:02 灵鱼 银须灵鲢 2.88斤 +幸运符×1` / `2. 00:05 空竿`，再加一行
  `鱼获：灵鱼×1、空竿×1，钓术经验 +12`；消息超过 3400 字时先去掉时刻与重量奖励，再退到每号一行合计。
- 09-28 当天在改动前已钓完（结算 token 用过即失效，补查不到），逐竿汇报从 09-29 0 点第一竿开始。
  首日需核对 `result_sample`：若出现“鱼名未识别”，按真实字段补充提取规则。

## 丢竿保护（2026-09-23 检修）

当天 @demo_player_04 服务器计到 5 竿，本地只有 4 次结算，`.result` 已被新作用域拒绝（`fishing_token_scope`），丢失的一竿无法追回。
两处修改：`waiting`（已下竿等咬钩）快照不再只返回等待、120 秒后再来——在同一次运行里等到 `biteAt`、重读 `start`、当场收线结算
（`_bite_challenge`）；`next` 回包丢失（超时/断连）时立刻在同一作用域重读 `start`（`_recover_unconfirmed_cast`）：
有 challenge 就收线，`lobby` 说明没消耗，`missed/expired/settled` 记入 `unconfirmed_casts` 作为证据。
汇总里会写"N 竿被服务器计数但未确认结算"。

## VPS 空竿复盘与优化（2026-10-05）

生产运行在 VPS 的 Docker `xiuxian`，宿主代码 `/opt/xiuxian/project`；Mac 只维护代码，不启动垂钓发送器。
当前入口是 `run_miniapp_fishing_cycle.py`，不是旧 `FishingCoordinator` 的鱼竿流转路径，鱼竿市场操作仍禁止。

当天官方确认 40 竿、5 次空竿（12.5%）：@demo_player_01 15 竿全中，@demo_player_04 5 竿全中；
@demo_player_02 与 @demo_player_06 各 1 次 `timeout`，@demo_player_03 两次 `timeout`，@demo_player_05 一次 `miss`。
四次 timeout 均先出现 `finish_pending_settlement`（finish 后查询 result 九次未就绪），2–3 分钟后在原作用域恢复结算。
`miss` 是同次运行正常结算。旧日志没有 finish 响应、评分和计时信息，不能断言每次 timeout 都由本地延迟造成，也不能把 miss 确认为随机概率。

发现明确架构问题：原 `run_all()` 在一个事件循环中 `asyncio.gather` 六账号，但协议请求及入口发现中包含同步 HTTP。
一个账号的慢请求会阻塞其他账号等待咬钩和收线的定时器；VPS 网络往返和偶发请求超时会放大这个问题。
本次没有拿 Mac → VPS 的 SSH 延迟代替 VPS → 游戏接口延迟，也没有为测速额外消耗竿数。

本次版本 `fishing-isolated-v1`：

- 每个账号拥有独立线程、事件循环、协议和 Telegram 客户端，最多六个工作线程；一个账号的同步 HTTP 不再阻塞另一个账号的收线定时器。
- 每账号每轮独立 `requests.Session` 复用连接，成功或异常都关闭，账号之间不共享连接池和 cookie。
- 公共入口缓存改为唯一临时文件后原子替换，避免并发写入共用 `.tmp`；连接失败也清理私有 session 副本。
- proof 的 fishSeed 按 JavaScript UTF-16 code units 求和，修正非 BMP 字符的跨语言差异；V2 20ms 控制步长和既有收线等待保持。
- `pending.json`、结算和 `runs.jsonl` 保存 `cast_diagnostics`：proof 时长、事件数、收线唤醒迟到毫秒数、finish 提交时间、
  HTTP 耗时/状态/明确拒绝标志、异常类型、可用时的评分及 result 查询次数。后续恢复结算继承原竿诊断，不写 token/initData。
- 空竿保留官方 `timeout` / `miss` 原因；新汇总将 timeout 显示为「空竿（超时）」。不回写旧日鱼获或修改配额。

保留 09-23 当次等咬钩、当次恢复 next 回包丢失，以及后续原 token 作用域恢复的保护：
未确认竿的 token 只存私有 `0600 pending_scope.json`；finish 不明时先读 result，只有匹配的原 challenge 且等待期结束才允许既有恢复流程。
只有 `/result.ready` 是正式结算依据；官方空竿同样消耗真实竿数，不能为提高成功率而重置或补造配额。

用户 2026-10-05 再次确认：**@demo_player_01 第一窝必须是妖腥窝（`demon`），然后灵草窝 → 灵草窝**。
生产配置已是 `["demon", "grass", "grass"]`；测试覆盖首窝优先、重复步骤、已有生效窝不覆盖及回包丢失不重复打窝。
窝料不足时保留原有「报告不足、该竿不带窝」规则，不跳过妖腥窝去先打灵草窝。其余账号凡饵、不打窝规则不变。

**验证边界**：离线测试可以确认账号定时器隔离、连接清理、结算恢复和计数规则；今天已完成配额，不强制额外下竿。
部署后核验协调器 `runtime_version=fishing-isolated-v1` 及日计数不变；真实 timeout 改善须看下一自然日的逐竿诊断，不能保证零空竿。

**测试**：按 `.maintenance/README.md`，只把脱敏源码与合成配置放入独立断网测试容器，复制到临时可写目录后，
在 `fishing/` 中执行 `python -m pytest -q --tb=short -p no:cacheprovider .`。禁止挂载真实账号、session 或状态。
测试包括 `test_fishing_runtime_isolation.py`、`test_fishing_chum_plan.py` 和既有协议/通知/配额回归。
