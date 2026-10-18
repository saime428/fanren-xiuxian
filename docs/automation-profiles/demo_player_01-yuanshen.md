# @demo_player_01 元神镇魔 / 元神修炼

## 状态与证据边界

**已按用户明确授权完成真实发送验证，两个 launchd 任务已安装并启用（2026-09-29）。**
启用时官方回执：魔染 0/100，按阈值跳过镇魔、不消耗修为；元神已在修炼，剩余 15小时18分钟40秒。
修炼忙碌拒绝消息 `12552755`，剩余时间面板 `12552757`，五魔面板 `12552722`。
2026-09-30 14:09:04（Asia/Shanghai）已收到修炼启动回执 `12562802`：「你的第二元神已开始闭关修炼，将在24小时后为你带来修为增长。」修炼期限为 10-01 14:10:04（含 60 秒缓冲）。
2026-10-01 10:29 检查 ledger：37 次 `.五子同心魔` 查询（36 次确认），魔染只有 0 或 8；没有镇魔扣费记录。例行每小时长面板不必要，按用户要求改为下述 6 小时简洁查询。
现有参考仓库都是客户端，而非游戏服务器源码。不能把客户端的预检查策略当成服务器限制。

- **镇魔冷却 6 小时**：用户在 2026-09-29 本次会话明确确认。成功官方回执的消息时间 + 6 小时持久化为 `purge_ready_at`；服务器报出更晚的冷却可延后，不能提前。
- **修炼 24 小时**：参考客户端识别的成功回复为「你的第二元神已开始闭关修炼，本次修炼将持续24小时」。出处 `references/upstream-xiuxian-mian/model/features/second_soul.py:640-649`，测试例 `tests/test_second_soul.py:592`。2026-09-30 本账号真实启动回执已确认另一种文案（见上），两种均覆盖测试。
- **不是已证实“只能窍中温养才能发送”**：参考客户端的 idle-first 是客户端策略。初次启动以及已确认当前周期的归位推送直接发修炼；2026-10-01 起，只有 24 小时本地计时到点、未收到归位推送时先查状态，不能将估计时间当作已结束。这是漏推送兜底策略，不新增服务器限制。
- 真实历史样本 `upstream-xiuxian-mian/tests/fixtures/real_message_samples.json:576-581`（样本来源时间 2026-06-15）记录：「你的第二元神正在(修炼中)，无法分心修炼。」它只证明当时该状态被拒绝，**不能证明所有其他状态不可修炼，也不保证今日规则没有改变**。
- 同文件 `387-406` 记录状态面板、镇魔消耗 **5000 修为**、魔染 `91→39`。不把减少 52 当成固定公式；实际变化以每次回执为准，未知/拒绝文本不计成功。
- **魔染 ≥60 才镇魔、每上海自然日最多预留 2 次支出**是本地保守策略，参考上游阈值但不是服务器规则。可配置 `--threshold 1..100`、`--daily-purge-limit 1|2`。按历史单次价格，最多约 10000 修为/日；实际游戏价格仍可能变化。
- 没有可靠证据表明修炼中不能镇魔，因此两个流程相互独立：修炼的 24 小时排期不阻止镇魔的 6 小时排期；镇魔达到预算也不阻止修炼。

## 两个发送入口与独立观察器

```sh
.venv/bin/python3 scripts/run_yuanshen_zhenmo.py --check
.venv/bin/python3 scripts/run_yuanshen_xiulian.py --check
.venv/bin/python3 scripts/run_yuanshen_return_observer.py --check
```

不带参数也是纯离线 dry-run。两种模式均不联网、不写运行状态。
只有显式 `--send` 才连接 Telegram 并发送游戏指令；本次已获授权执行，已安装的两个 launchd 任务均使用该参数。

### 元神镇魔

到期先在本地检查 6 小时冷却、当日预算；不满足时不打开 Telegram、不发送镇魔状态查询。
满足后只查 `.第二元神` → 解析 `五子同心魔: … | 魔染 N` → 低于阈值则 **6 小时后复查** → 魔染 ≥60 且预算允许则发一次 `.元神镇魔`。
`.第二元神` 的真实官方回执 `12552757` 同时包含状态、剩余修炼时间、同心和魔染；无需再发 `.五子同心魔` 长面板。后者只保留历史日志解析，不在自动发送白名单中。
无论魔染是否降到阈值以下，都不会立即连发第二次；持久化 6 小时冷却。
预算用完显示 `purge_budget_exhausted`，等下一上海午夜 + 60 秒与冷却到期两者中更晚者，不静默卡住。

6 小时低魔染复查是**本地降频策略**，不同于镇魔的游戏冷却；正常持续低魔染时约从 24 次/日减至 4 次/日（修炼流程的必要查询另计）。没有可靠的魔染增长速率，不能线性预测到 60 的时刻；缺少魔染或未知回复仍按 30 分钟异常重试，不误判为 0。
修炼中/受伤/心魔等阶段不被额外当作镇魔禁令；镇魔只看实际魔染与自身限制，修炼流程仍独立处理这些状态。

### 元神修炼：归位推送优先（2026-10-01 晚更新）

真实延迟：官方归位推送 [12575557](https://t.me/example_game_group/7310786/12575557) 于 **14:15:45** 发出，但到 **14:45:28** 的 [12575770](https://t.me/example_game_group/7310786/12575770) 才续发 `.元神修炼`，空等 **29 分 43 秒**。14:10 状态回执仍写「修炼中」却无剩余字段，旧程序退回 30 分钟等待且没有归位推送入口，是本次原因。

1. **各自独立、只复用底层实现**：深度闭关和元婴出窍本来就是各自的 LaunchAgent / 程序 / session / 锁，只共用 `observe_return.py`。按用户 2026-10-01 的明确要求，第二元神也独立为 `ai.hermes.xiuxian.tg9100000001.yuanshen.return_observer`，入口 `scripts/run_yuanshen_return_observer_tg9100000001.sh` → `scripts/run_yuanshen_return_observer.py` → `yuanshen_return.run_persistent`。其专属 Telethon 连接监听 `NewMessage` / `MessageEdited`，同一专属连接每 60 秒补查历史；不再寄宿深度闭关，不依赖深度闭关或元婴 observer 的运行状态。只复用通用循环、传输和会话授权工具，不共用进程。
2. **严格确认归位**：`yuanshen_return.py` 只接受官方 sender、指定群和 topic、精确 `@demo_player_01`，正文必须为「【第二元神归位】…已结束修炼，回归窍中温养」。拒绝转发、其他账号/主题、未来消息和超过 6 小时的历史事件。元婴的「元神归窍总结」不能混用。
3. **独立收件箱、立即叫起**：专属 observer 的 push 和历史均先保存 `return_events.json`，按 message ID 去重，然后异步、非强制 `launchctl kickstart` 原 `yuanshen-xiulian`。不等待 300 秒普通 tick，也不在 push handler 内发送游戏命令；深度闭关、元婴出窍的运行/暂停/重启均不控制这个独立进程。
4. **当前周期有效才续发**：runner 在自身 flock 内复核归位事件，解除该轮 train 计时，直接发 `.元神修炼`，不用再查一次状态。比较最新训练尝试的 message ID / watermark / 时间，以及更新的官方忙碌状态回执；迟到/编辑/重播的旧完成事件不能清除已开启新周期的计时。Telegram 同秒消息按 ID 判断先后。
5. **完成旧轮不等于开启新轮**：归位事件只确认旧轮结束。发新指令前写 intent，发送后写 ID，只有直接官方「已开始闭关修炼…24小时」回执才确认下一轮；保存归位 ID 与下一轮指令/回执 ID 的关联，再设置回执时间 +24 小时 +60 秒兜底计时。
6. **漏推送兜底**：启动/断线恢复与每 60 秒历史扫描使用第二元神自己的连接。如果仍没收到归位，24 小时计时到点先 `.第二元神`：有剩余时间按官方倒数 +60 秒；仅写「修炼中」无倒数时 **60 秒后再查**；确认为窍中温养才续发，不因本地时间到了就盲目重发。现有 30 秒 `return-deadline-dispatch` 负责及时叫起到期/待重试 sender，300 秒 tick 保留为最后兜底。
7. **发送不确定**：约 **120 秒**后先恢复旧消息的直接官方回执；仍未确认则查 `.第二元神` 判断，而非重复盲发。锁忙、重启、kick 失败保留收件箱/日志。普通重试按流程隔离，镇魔查询超时不会挡住已归位的修炼；显式服务器冷却、Telegram FloodWait 和青元子优先仍生效。

已保留归属于 @demo_player_01 的官方「你的第二元神已从【受伤】状态中恢复」推送识别、恢复后续练
和受伤倒计时修复。2026-10-15 用户调整：收到归属于 @demo_player_01
的官方心魔原警示时，由独立 observer 回复 `.抉择 稳固道心`，禁止自动强行突破。
本次维护版本已于00:44部署并核验，见 [心魔事件契约与衔接](demo_player_01-heart-demon.md)。
只有面板状态而缺少原警示时仍显示 `manual_heart_demon`，不猜测回复目标。分化、调度或购买
未自动化；镇魔的 6 小时查魔染/冷却与每日预算保持独立。

### 发送不确定、重启和归属

- 发送前先写尝试与预算，发送后立即写 message id，之后才等回复。
- 指令成功只接受官方机器人对本账户指令的直接回复；旧轮完成另接受上述严格归属的官方归位推送。两者都拒绝转发和别人的消息。
- 连接后双重校验 `id=9100000001` 与 `username=demo_player_01`；不能用 CLI 切换账号。
- 未确认的修炼先恢复历史回执，再查新面板。**只对不确定的重发**保守要求已归位，防止重复启动；这不是对初次发送的服务器资格断言。
- 未确认镇魔保守预留 6 小时和一次预算，之后先查当前魔染；未收到回执的旧尝试仍标作未确认，不伪造成功。
- 普通未知回复/发送失败：修炼 120 秒后核对，镇魔仍为 30 分钟；Telegram FloodWait 可延长等待且跨流程生效。
- 每个发送在全局发送闸门之前和闸门等待之后检查青元子优先，13:36–13:52 及活动战斗入口避让，无强制绕过选项。
- 唯一出站路由 `@example_game_group` (`-1009400000004`) topic `7310786`。

## 运维归属

领域：`live automation`，本机 legacy Python/launchd；不是 Hermes cron。
唯一主写实现 `yuanshen_automation.py`；两个入口通过同一个 flock 串行写
`state/yuanshen/tg9100000001/state.json`，锁文件同目录 `runner.lock`。
`due.train` 与 `due.purge`、普通重试等待按流程分离；只有实际 FloodWait 的 `transport_not_before` 跨流程生效。每个尝试带 `flow` 归属，健康检查不会将镇魔的 `.第二元神` 当作修炼回执；单流程查询超时/失败不覆盖另一流程的 `flow_status`。
归位收件箱 `state/yuanshen/tg9100000001/return_events.json` 的唯一主写者是独立 `ReturnObserver`，使用 `return_events.lock`；runner 只读此收件箱、独立写 `state.json`。三种闭关/出窍流程不共用 cursor。
独立观察器专属文件（同目录）：`return_listener.session`、进程全生命周期锁 `return_listener.lock`、`return_listener.log` / `return_listener.err.log`。进程锁在授权探测和打开 session 前取得；session 只从本账号权威 `state/accounts/tg9100000001/session` 初始化，不借用深度闭关或元婴的活动 session。`KeepAlive` 加 30 秒重启节流负责进程恢复。
健康检查要求独立 label、`mode=standalone`、本账号身份、连接存活及近期（≤300 秒）历史扫描证据；旧寄宿进程的心跳不能当独立观察器健康。漏接、kick 失败、归位后超过两分钟仍未接续会报 BAD，不能被旧训练回执/未来 timer 洗绿。

模板在 `scripts/ai.hermes.xiuxian.tg9100000001.yuanshen-{zhenmo,xiulian}.plist`，每 300 秒轻量 tick；实际出站频率受状态排期约束。每次启动重新 import 共享模块，修改 Python 后自动采用新逻辑，不需要 reload/kickstart，也不为验证而额外发指令。
模板含 `--send`，已复制到 `~/Library/LaunchAgents/` 并通过 `launchctl bootstrap` 加载，两项均已运行且 exit 0。运行状态和日志在 `state/yuanshen/`。两个 label 已加入 `config/active_flows.json` 的 helpers，不假冒通用 sender state；活动健康检查同时验证排程与本账号官方回执、各流程持久化结果、待确认发送和到期未运行。失败不会被后续 `scheduled_wait` 的 exit 0 掩盖。

2026-09-29 启用前去重：现有项目生产代码、`~/Library/LaunchAgents`、Hermes cron、HEARTBEAT 未找到这两个元神任务。没有覆盖/暂停已有任务。workspace 的 `scripts/ops-alignment-check.sh` 不存在，未运行。

## 离线验证

```sh
.venv/bin/python3 -m pytest -q test_yuanshen_automation.py
plutil -lint scripts/ai.hermes.xiuxian.tg9100000001.yuanshen-zhenmo.plist
plutil -lint scripts/ai.hermes.xiuxian.tg9100000001.yuanshen-xiulian.plist
```

覆盖协议解析、6 小时边界/重启、冷却只延后、两个排期互不阻塞、忙碌后查询、预算、误账号、错误回复归属、未确认恢复、锁和青元子避让。所有测试使用假的发送器，不触发真实通知或发信。

2026-09-29 初次正式目录验证：本功能 41 项测试，与任务注册、官方回执、账号映射、传输和健康检查回归合跑共 **184 passed、73 subtests passed**。

2026-10-01 降频验证：本功能 **55 项测试**，连同注册/LaunchAgent/账号归属/官方回执/传输/健康检查/目录校验，共 **207 passed、80 subtests passed**。两个 plist 校验通过，两入口 `--check` 均返回 `network_used=false`、`status_command=.第二元神`、`purge_check_seconds=21600`。新增覆盖低魔染 1/2/6 小时边界与重启不联网、预算耗尽不连接、简洁面板所有元神阶段、缺失魔染不扣费、两个流程的查询/失败归属及旧五魔日志兼容。

已在共享锁内备份并通过同一主写实现调整当前低魔染排期：基于官方回执 `12573788` 的 10:29:15 + 6 小时，下一次镇魔状态查询为 **10-01 16:29:15**（实际由后续 5 分钟 tick 触发）。修炼期限仍为 **10-01 14:10:04**。旧版五魔查询超时污染的修炼状态根据已确认启动回执 `12562802` 恢复；没有修改任何尝试/消息 ID、预算或游戏冷却。备份 `state/yuanshen/tg9100000001/state.before-cadence-20261001.json`。
上午降频部署后，两流程业务健康检查均 `ok`，禁止联网的真实状态排期检查均返回 `scheduled_wait`；当时未额外发指令或重载任务。16:29 已自然执行 `.第二元神` 镇魔查询并收到官方回执 `12576532`，魔染 8，下一次按 6 小时排期。

### 晚间归位推送部署验证（2026-10-01，寄宿方案的历史记录，已由下节独立拆分取代）

- 深度闭关、元婴出窍、第二元神、dispatcher、注册、账号/回执及健康检查相关回归：**260 passed、97 subtests passed**。覆盖同秒 ID 顺序、旧/重复/编辑推送、新状态优先、发送未知及迟到回执、镇魔超时隔离、官方等待不可绕过、push/history 两个 hook、监听心跳失效及 dispatch 失败不能洗绿。
- wrapper shell 检查和 `git diff --check` 通过；`--check` 确认 `training_completion_mode=official_return_push_with_status_fallback`、状态复查 60 秒、未确认重试 120 秒、`network_used=false`。
- 已在 observer 与 sender 都未持锁的窗口，仅重载 `ai.hermes.xiuxian.tg9100000001.shendubiguan.return_observer`，激活寄宿 hook。新 PID `62196` 的 listener 通过本账号验证并持续完成历史补查，两个元神 helper 业务健康均 `ok`。没有增加监听连接、没有重启/中断实际修炼或其他账号。
- 此时最新真实修炼启动仍是 `12575771`（10-01 14:45:32），兜底计时仍为 **10-02 14:46:32**；期间归位推送可提前解除这一估计计时。没有为测试额外发送游戏指令或覆盖当前训练状态。代码/模拟事件路径和实际监听激活已验证，下一次真实归位→续发的线上耗时尚未发生，不能将测试当作真实接续成功。

### 独立观察器拆分与验证（2026-10-01）

用户明确要求与深度闭关、元婴出窍一样各自独立处理。已删除 `observe_return.py` 中全部第二元神寄宿 hooks 和深度闭关 wrapper 的启用开关；新增独立 wrapper / Python 入口 / plist，并注册到 helpers，健康检查独立列出。

切换先备份业务状态与收件箱（`state.before-independent-20261001.json`、`return_events.before-independent-20261001.json`），在深度闭关 observer/sender 都空闲的锁窗口重载其程序，确认旧寄宿 writer PID `62196` 已退出后才 bootstrap 新 observer，避免两个主写者同时处理收件箱。交接时 `state.json` 完全未变，训练轮次、消息 ID、计时、镇魔预算均保留。

验证快照：三者均独立 running，深度闭关实际 Python PID `3034`、元婴出窍 `673`、第二元神 `3038`。第二元神专属 session 已建立，`mode=standalone`、账户核验和历史补查正常；listener、train、purge 三项健康均 `ok`。原元婴程序未重启。拆分回归 **264 passed、97 subtests passed**，shell/plist 校验与离线 `--check` 通过。未额外发送游戏测试指令；推送驱动、60 秒补查、120 秒未确认核对与镇魔 6 小时规则不变。
