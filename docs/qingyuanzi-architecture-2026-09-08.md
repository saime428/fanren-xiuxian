# 青元子 MINI APP 架构与适配审计（更新于 2026-09-09）

> 以下保留 v6/v7 阶段的历史审计。三个 Telegram 参考脚本和真实 CF callback 证据见 [2026-09-09 适配记录](qingyuanzi-reference-adaptation-2026-09-09.md)；当前 `qyz-reference-v9` 的来源轮换、重启恢复及战斗适配见 [2026-09-10 复盘记录](qingyuanzi-battle-review-2026-09-10.md)。旧的“禁止参考浏览器、CF 必然阻断”描述不代表当前实现。

## 证据和范围

- 当前官方公开页面：`https://asc.aiopenai.app/miniapp/xianxia-world-boss`，2026-09-09 本轮再取，仍为 120663 bytes / 3135 lines，SHA256 `3d1c01e987edfe45ea67e696435ad53bf60290fca91e61016953dfd2f2091d37`。以下行号对应这一版 HTML。
- 页面为 HTML/CSS + 单个内联 JS IIFE，依赖 Telegram WebApp SDK、Cloudflare Turnstile SDK、Canvas 2D；图像为 `/staticweb/qyzboss.webp`、`/staticweb/qyz.png`。
- 本地生产：`qingyuanzi-new-source/qingyuanzi_host.py`，由原有 `ai.hermes.xiuxian.qingyuanzi-six.monitor` 启动。最新实机 PID 33709，运行 `qyz-protocol-v6`，执行者为 `official_protocol`；下面保留各次适配的历史记录。
- 仓库原有大量未提交改动。本目标在已有代码上逐步修复；此前 v6 增加漏窗/倒地结算及相应证据记录。最新接续仅完成旧执行空壳、无用 Dashboard 设置及相关字节码/测试索引的物理清理，分三批从生产树移走 51 个路径（含一个缓存目录），使用系统废纸篓可回退，不重写 Git 历史、不删除账号凭据和真实历史证据。两项清理约束先 RED 后 GREEN；清理后本目录 73 项、健康/注册表相关 99 项回归通过，均非服务器实战证据。未重启现有主控，未改动其它玩法。
- 六账号最新独立检查均通过身份、群成员及新消息 `1075814` 的来源校验。该场次已过期且官方验证未接通。没有申请签名 WebView，没有调用任何游戏 POST。
- 此审计能完整说明**公开前端协议与本地调用链**；游戏后端源码、认证态响应和本次服务器结算未取得，不能据此宣称已还原后端实现或实战通过。

## 运行面和数据流

```text
已授权官方群/固定入口消息/实际 bot 发件人
  → 六个独立 Telegram 身份（只读 SQLite → MemorySession）
  → 账号进程锁 + 账号事件互斥 + mutation journal
  → 重新读取入口/身份/群成员/官方安全策略
  → Telegram RequestMainWebView → 内存 initData
  → /start → 等待 joining→battle → /begin
  → 动态 /window → /charge-start → /hit （N 次）
  → 等所有回执及结束宽限 → /finish
  → 账号状态、逐窗服务器回执、健康汇总
```

生产只使用 `official_protocol` 作为请求执行者。旧 `qyz_browser_execution.py`（包括拒绝执行的兼容空壳）、旧测试及旧模块字节码现已从生产工作树移除，不再保留第二个执行入口。无生产引用的 `automation_settings.py/json` 也已移除，六账号映射只取自 `qingyuanzi_host.ACCOUNTS`。不开 Chrome/CDP、sidecar、proof 文件或人工 token 注入通道。

## 前端组成

| 层 | 入口/状态 | 作用 |
| --- | --- | --- |
| Telegram 壳 | 770–791、902–907 | `ready/expand`、颜色、关闭竖滑；从 query 或 Telegram `start_param` 获取入口；`tg.initData` 提供认证 |
| 会话和角色 | 1295–1325、1380–1564 | entryToken/sessionToken 分离；`needsIdentitySelection` 时显示 playerId 选择，不等于自动允许代选任意角色 |
| 房间 | 1565–1764 | joining、battle、closed、人数不足；boss HP/phase/人数、每角色参战额度；WebSocket 与轮询同步 |
| 开战验证 | 908–1042、1765–1837 | 官方 widget callback 生成一次性 token 和配对幂等键，只提交 `/begin`；成功、失败、过期时清理 |
| 战斗模型 | 836–885、1838–2288 | startedAt/elapsed、challenge/windows、processedWindows、蓄力票据、actions、pendingHitReports、生命和统计 |
| 帧循环 | 2289–2345 | requestAnimationFrame 更新时间；每 360ms 拉窗口；蓄力 UI；漏窗反击；正常结束/超时 |
| 画面 | 2346–3030 | 背景缓存、阵法、boss、窗口、角色、剑光、反击、死亡、飘字；为展示，不是服务器结算证据 |
| 输入 | 3031–3130 | pointerdown 开始蓄力，up/cancel/leave 释放；按钮入阵/重连；online/visibilitychange 恢复房间同步 |

## 接口映射

所有 HTTPS POST 前缀均为 `/api/miniapp/xianxia-world-boss/`，请求共享账号私有 `token` 和 `initData`。

| 端点 | 特有请求字段 | 公开客户端使用的返回字段 | 语义及参考位置 |
| --- | --- | --- | --- |
| start | playerId | needsIdentitySelection/identityChoices、sessionToken、boss、player、room、challenge | 入场和角色确认；等待开战后刷新 challenge。1404 |
| state | 无 | boss | 房间状态读取，约 2800ms 最小间隔。1616 |
| ws-ticket | 无 | ticket | 获取短期 WS 凭据，连接 `/ws/miniapp/xianxia-world-boss/state?ticket=…`。1670 |
| begin | challengeId、turnstileToken、turnstileIdempotencyKey | startsInMs | 正式开战；用 RTT/2 校准等待时间。1765 |
| window | challengeId、afterWindowId | window、done、windowCount | 流式披露窗口；正数 windowCount 可以更新，不能硬编码 16。1838 |
| charge-start | challengeId、windowId | chargeTicket | 每窗一次性蓄力票据。3031 |
| hit | challengeId、windowId、chargeTicket、elapsedMs、holdMs | boss、hit.damageYi | 实时伤害回传。前端不读取 hit.perfect，是否实际有此字段只能由认证响应证明。2113 |
| finish | bossProof | result.grade、score、player_hp | 本轮结算。2231 |

`bossProof` 字段为 mode=`qyz_focus_burst_v2`、challengeId、stance、durationMs、playerHp、dead、actions、clientStats、realtimeDamageApplied；actions 每项为 `{t,holdMs,stance}`。本地统计只是提交材料，绝不等于服务器确认的完美次数。

## 时序、窗口和结算

1. `actionLimit=1` 是角色参与额度，不是窗口总数。前端自动入阵要求 roomStatus=battle、actionsUsed=0、actionsRemaining>0。
2. begin 返回后等待 `max(0, startsInMs - RTT/2)`，再以 monotonic/performance 时钟开始本地计时。
3. 每窗使用 id、centerMs、hitMs、perfectMs；命中条件为 `abs(t-centerMs) <= hitMs`。本地完美还要求偏差不超过 perfectMs 且 holdMs 在 520–1250ms。
4. 浏览器异步请求蓄力票据，在释放后等待票据再报 hit；网络响应不改变已发生的释放时间。官方 hit timeout=7000ms，charge-start/window timeout=5000ms。
5. 动态窗口不得预生成。重复 id 不重复蓄力/报 hit；`boss_window_not_ready`、`boss_battle_not_started` 是可再次读取的等待态。
6. 正常结束为所有已声明窗口处理完、`elapsed >= lastWindowEnd + 2200ms`、pendingHitReports 清空。`lastWindowEnd + 9000ms` 用于超时上限，不是正常结束宽限。
7. 正常窗口目标由服务器动态修订。早到的 done/空 window 不能直接视为 N/N 完成。
8. 漏窗在 `centerMs + hitMs + 80ms` 后按 phase 扣 16/22/30 生命，未知阶段按 22；未命中的释放若 holdMs>1800，再扣 8。v6 记录这些实际本地事件并继续后续窗口；生命归零进入 dead，1250ms 后提交真实生命/动作统计。未确认或不确定的 HTTP mutation 仍不能据此冒充完成。
9. 没有 token 的公开页进入 demo（18 个本地窗口）；demo、飘字、按钮“本轮完成”、客户端 perfects 都不能证明服务器成功。
10. “战果回传中/战果同步中”仍在进行；“战果未被天机阵承认/结算失败”是失败。只接受本账号本挑战的实际 `/finish` 回执，完美目标还需要逐窗服务器证据。

## 重试边界

官方通用 postJson 基于 timeout、网络错误、retryable、429/5xx 作有界重试。start 为 9s/2 次；begin 为 7s/1 次，调用内部使用同一 token+幂等键，最终返回后 reset；ws-ticket 为 6.5s/1 次；window 和 charge-start 为 5s/零次；hit 为 7s/一次且仅限指定 HTTP 状态；finish 默认 8s/零次。

本地请求层明确禁用 transport 隐式重放，所有有副作用的请求暂时零重试；HTTP 5xx、超时、无效响应均保存不确定结果，不自动重复消费入口。这是没有认证态服务端幂等证据时的保守差异。window 的“未就绪”后续轮询是读取新状态，不是重复消费票据。

## 账号、入口与健康

账号顺序唯一来自 host.ACCOUNTS：tg9100000001、tg9100000002、tg9100000003、tg9100000004、tg9100000005、tg9100000006；最后一个使用 tg9100000007 凭据来源，但必须验出真实 ID=9100000006 和 username=demo_player_06。

当前固定源为用户提供的 `https://t.me/c/9400000005/1075814` / chat=-1009400000005 / 无 topic / message=1075814，入口发送者为已核实的 `hantianzun31_bot`。后续公告的跟进范围仍未得到答复，不自动改用其他消息。入口重读、bot 发件人与按钮 bot 一致、token 前缀和 60 秒新鲜度都需验证。共享状态只传消息定位和 fingerprint，不保存 token。

域为 live automation / fast path；继续使用现有一个 LaunchAgent，不新增调度。账号 `world_boss_state.json` 由 host 唯一写入，内含业务状态及健康心跳，健康 collector 只读同一来源。账号进程锁防多进程重复执行；asyncio 互斥防同账号不同事件重叠。开始任何 mutation 前先持久化 submitted 状态，重启不得重发已提交/不确定事件。

健康必须区别进程连接、入口可读、身份正确、验证可用、正在战斗、服务器结算。monitor_active 不能覆盖入口缺失或验证不可用，也不能覆盖旧运行代码。

## 当前不可绕过的现场条件

- 2026-09-09 六账号均能读取用户的新入口，但公告发布于前一日 14:37，60 秒入场期限已过。新来源已适配，后续有效公告的跟进范围仍待答复。
- 当前官方 `__QYZ_TURNSTILE_CONFIG__.enabled=true`，action=`qyz_world_boss_begin`。纯 HTTP 代码没有官方 widget callback 的执行能力；Telegram 签发的 initData 不能替代 Turnstile token。现有禁止浏览器/注入/人工 proof 的约束下，无人值守开战尚无可验证的实现路径。
- 因此当前可完成的是协议及健康适配、离线故障回归、真实进程加载验证；不能把这些报告成六账号自动战斗已恢复。后续实战验收必须有新鲜入口、服务器接受 begin、动态 N 个 hit 和 finish。

## 2026-09-08 已实施与验收（历史）

- 在已有协议模块上修复：服务端窗口目标可上调/下调、窗口数必须是整数、未就绪窗口有界继续读取、身份选择及参与额度检查、请求 timeout 与现行官方页面对齐。HTTP 请求不确定时不重放，任意服务端错误文本不会直接进入日志。
- host 增加每账号只读健康心跳、精确固定消息轮询、共享入口定位校验；保留原有内存会话和账号互斥。修复实际 resolver 返回正数 Channel.id 与 marked peer ID 的契约差异，空/异群解析结果禁止装配监听器。
- 六账号 `qyz_monitor` 接入现有 active_flows 和 health collector。collector 读取 host 的原状态文件，无额外状态写入者或独立 cron；校验身份、源码版本、实际 PID、心跳、来源和安全策略时效；失败/部分结算不会被进程在线覆盖。
- launchd 明确使用项目 `.venv/bin/python3 -B`，依赖中声明 httpx。未新增服务；在确认旧进程六账号 event_count 均为 0 后切换。首次 bootstrap 在旧进程退出时返回 error 5，确认旧进程已退出后重试成功；实际启动暴露的正负频道 ID 差异已修复并补测。
- 最终青元子目录 `unittest discover`：**53 tests OK**；健康 collector/效率/告警回归：**97 tests OK**。无网络 `--check` 明确返回 offline_contract_only / business_ready=false；plist lint 和 git diff --check 通过。
- 新进程 PID **77577**，2026-09-08 20:41 启动，六账号均产生 `qyz-protocol-v2` 新 ready 日志，execution_owner 均为 official_protocol，monitor_active=true。
- 20:42:11 第一轮健康快照中六账号均为 BAD，原因 source_unavailable；各账号原状态同时记录 official_verification_required。身份与群成员资格均已验证。全体 83 active checks 中，6 BAD 正是这六项青元子；其他业务的 1 WARN 未在本任务修改。
- 此后第二轮自动心跳已全部刷新（时间戳从约 1788871292 更新至 1788871355），六账号再次确认相同两项阻断；PID 77577 和 launchd runs=2 均保持不变，没有重启循环。
- 本轮没有发送群消息、没有申请签名 WebView、没有调用 start/begin/window/charge-start/hit/finish；零现场战斗证据。所有测试战绩都是 fixture。

后续动作仍是取得当前官方入口，并明确在既有执行限制下可行的官方验证方式。不能只替换消息 ID 就宣称全自动开战已恢复。

## 继续审计：异步回执与战斗时序

后续按原目标继续检查发现：前一版虽然在理想快速响应 fixture 中能处理所有窗口，但 `/charge-start` 和 `/hit` 的同步等待与官方页面的异步输入时序不同。晚到的票据会拖长本地蓄力，慢命中回执会延后下一窗处理。这是实际战斗路径的缺口，不能以原有绿测忽略。

`qyz-protocol-v3` 将票据请求、按 monotonic 时钟释放和命中回执分离；释放时间/holdMs 在释放时固定，获得相应票据后仅提交一次 hit；下一窗不等待前一窗网络回执。所有命中回执完成并确认后才允许 finish，明确拒绝已失败或不确定的回执；异常及取消会取消并回收本账号事件的全部待处理请求，不让请求越过 HTTP client 生命周期。一个 runtime 实例只能执行一次。账号连接池上限为 6，仍然没有 transport 隐式重试。

新增 5 项回归先取得失败再通过：慢 hit 等下一次 charge 的依赖测试、票据等待释放的依赖测试、丢失 hit 不重放/不结算、取消期间清理 pending charge、六账号并发认证及回执隔离。当前青元子 **58 tests OK**，健康相关 **97 tests OK**。

这些测试是离线的请求顺序/生命周期证据；仍不替代当前官方验证、真实窗口时序或服务器结算。已确认六账号均无进行中事件，再重载 v3；现场入口和验证阻断不变。

## 2026-09-09 继续适配、清理与完成审计

本次重新 GET 官方公开页面得到 HTTP 200 / 120663 bytes，SHA256 仍为本文开头的 `3d1c01…e1d37`。没有用历史副本冒充当前线上版本。

`qyz-protocol-v4` 修正了仍会影响实战的输入时序：

- `WindowStream` 在当前窗口蓄力期间预取下一个动态窗口，维持单个读取者和最多一个窗口的预取深度；未就绪/重复窗口仍有界轮询，动态 `windowCount`、去重、账号隔离和结束时取消请求沿用原约束。
- 窗口较迟披露时，若剩余完美区间允许，会把释放时间后移以取得至少 550ms 的实际蓄力。不会修改测得的 holdMs；已经过期的窗口在申请 chargeTicket 前拒绝，避免徒增无效操作。
- 下一窗读取、蓄力票据、命中回执均不会互相串行阻塞释放计时；只有全部已接受回执齐备才进入既有 finish 流程。
- 事件 journal 和回执在每次 trace 中合并为一次原子状态保存，仍保证 mutation submitted 记录先于对应 HTTP 提交，去掉每条 trace 的重复写盘。

清理结果：删除无生产调用者的旧 `log_utils.py` 共 **5954 行**。青元子原来只使用其中的群组解析；已在 host 中替换为十余行严格官方群解析，并验证实际 Telegram Channel 的 marked ID、megagroup 与本账号客户端。删除无引用的扫描数量常量、未被使用的 RTT 收集和旧 actor 配置。历史浏览器执行器仍为明确拒绝运行的短兼容桩，其原执行逻辑已移除。未清理独立垂钓或其他业务状态。

另修正 `miniapp_registry.py` 的陈旧登记：适配器由不存在的 `qyz_original_adapter` 改为 `qyz_protocol`，flow 对齐 `qyz_miniapp`；删除静态、无证据的 `production_enabled=true/live_verified=true` 声明。运行中的监控由既有 LaunchAgent 和 active_flows 管理，不能把监控安装状态当作战斗验证。

验证：青元子目录 63 项回归通过，随后新增 1 项实际 monotonic 时钟测试通过（1.4s 窗口延迟、0.4s 票据延迟、0.7s 命中回执延迟，5.301s 完成）；合计 **64 项**。健康、注册表、动作策略回归 **104 项**通过。三个新增时序问题与注册表错误均先观察到失败再修复。所有战绩响应来自明确标记的 fixture；实际时钟测试也没有访问游戏服务器。`git diff --check`、无网络 `--check` 通过。工作区不存在 `scripts/ops-alignment-check.sh`，没有声称运行过它。

加载前重新确认六账号均无事件/无在途 mutation、心跳新鲜且绑定 PID 82089，向现有 LaunchAgent 发送 SIGTERM，让既有 KeepAlive 正常拉起更新；没有新增调度。新 PID **4452** 的六账号已加载 `qyz-protocol-v4`，身份、成员资格和连接状态均真实确认。2026-09-09 **01:26:19 +0800** 再次调用只读健康分类：六项 QYZ 均 BAD / `blocked_source_unavailable`，心跳年龄 22–25 秒，实际进程存在；账号原状态同时保持 `official_verification_required`。未申请签名 WebView、未调用游戏 POST，事件数仍全部为 0。

| 原目标要求 | 当前直接证据 | 审计结论 |
| --- | --- | --- |
| 完整解析 Mini App 青元子 | 当前公开 HTML、接口/状态/时序/验证/结算映射 | 公开前端解析已落文档；后端及认证响应未取得，不声称还原后端 |
| 按解析适配 XIUXIAN 青元子模块 | host/auth/http/protocol 代码、64 项本模块测试、真实 v4 进程 | 适配已实现并加载，但当前验证门槛阻止实际开战 |
| 删除相关冗余代码 | 5954 行旧工具删除、旧浏览器逻辑禁用、陈旧注册表修复、无残留引用检查 | 本轮确认无生产用途的旧代码已清理 |
| 正常战斗 | 必须是六账号本挑战的 begin、全部动态窗口 hit 与 finish 回执 | **缺失，未完成**；fixture 不是实战证据 |
| 实现自动化 | 必须可持续取得授权入口、完成官方验证，并自动取得全部服务器结算 | **被阻断，未完成**；进程在线不能证明业务自动化 |

相同现场阻断已跨至少三个连续目标轮次复核：唯一允许的消息 `12190787` 无法读取，官方 Turnstile 开启，而既有约束禁止 Chrome/CDP/worker/sidecar/人工 proof 等替代执行路径。之前提出的新官方入口请求仍未获得答复。继续实战需要有效、被授权的官方入口和可执行的官方验证路径；不能靠重复重启、增加 fixture、放松来源约束或隐藏 BAD 达成目标。目标应标记 blocked，不能标记 complete。

## 2026-09-09 用户提供新入口后的适配

用户随后提供 `https://t.me/c/9400000005/1075814`，已取代旧的固定入口。六账号分别使用自己的内存 Telegram 会话进行只读读取，均确认：chat=-1009400000005、message=1075814、megagroup=true、forum=false、无 topic、实际发件人为 `hantianzun31_bot` 且 bot=true、非转发、按钮为「进入真仙战场」且指向同一官方 Bot 的 qyz startapp。未输出或落盘 token。

该公告发布于 **2026-09-08 14:37:00 +0800**，原文明确规定 **60 秒入场**、至少 10 人、个人一次完整参战、总时限约 7 分钟、单次参战窗口约 300 秒。读取时已过期；「按钮仍存在」不能证明还能加入该场。公开页面再读取仍为同一 SHA256，Turnstile enabled=true。

v5 变更：

- 来源绑定更新为用户的新私有群和消息，无论坛 topic；额外固定已核实的 Bot username，拒绝其他 Bot 自行提供同样形式的按钮。
- 私有群不依赖公开用户名：内存实体缓存为空时，从该账号自己的 Telegram dialogs 有界查找精确群 ID；只保留内存实体，不复制或写回会话数据库。群解析和成员校验复用该路径。
- 入场新鲜度按本条官方公告从 120 秒改为 60 秒。健康区分 `source_verified=true` 与 `entry_fresh=false`，过期入口明确为 `blocked_entry_expired`，不会因页面可读就声明业务可用。
- 来源本轮仍限定所提供的消息。已异步询问是否允许跟进同群、同一官方 Bot 的后续新开战公告；在答复前没有扩大到其他消息或群，也没有改变其他 XIUXIAN 流程的 Telegram 发送范围。

验证：来源/期限回归先出现 4 项失败，修改后青元子目录 **68 tests OK**（包含真实时钟延迟测试）；健康相关 **97 tests OK**；diff whitespace check 通过。六账号再次通过**新生产代码的真实只读检查**：2026-09-09 02:29:10 +0800 均连接、身份、成员资格和来源校验通过，状态均为 `blocked_entry_expired`，blockers 为 `entry_expired` 和 `official_verification_required`。这次检查使用内存 actor，不写生产状态、不安装会触发战斗的临时监听器。

确认旧 PID 4452 六账号事件数均为零且心跳新鲜后，已让既有 KeepAlive LaunchAgent 优雅加载 v5。新来源解决了「消息无法读取」问题；本场已过期和官方验证方式仍未解决。没有申请签名 WebView、没有游戏 POST、没有服务器战斗结算，完整目标仍未完成。

## 2026-09-09 @demo_player_01 人工成功参考复核

用户提示该账号有人工参战及成功过盾记录。已从 Hermes 只读历史数据库 `/Users/operator/.hermes/state.db` 的 `messages.id=36103` 恢复 **2026-09-06 当时直接读取 Telegram 的 terminal 输出**，不是仅引用旧助手结论。对应调用 `messages.id=36101` 明确读取群 `-1009400000004`；留存时间为 2026-09-06 13:57:43 +0800。复核证据和原文摘要 SHA256 已补入 [当日报告](../qingyuanzi-new-source/state/reports/qyz-live-feasibility-2026-09-06.json) 的 `manual_control.reference_audit`。

| 记录 | 消息 ID | 发布时间（+0800） | 实际发送者 |
| --- | --- | --- | --- |
| 当日开战公告 | 12174960 | 2026-09-06 13:40:14 | @sample_peer_06 |
| 当日最终结算 | 12175018 | 2026-09-06 13:47:21 | @sample_peer_05 |

结算原文确认：**@demo_player_01 排名第 2，3760 分，强攻 16，伤害 7.32 亿亿**；保底奖励为伐仙功 +7、修为 +5760、灵石 +105、贡献 +57。整场结果为「天道败退」，人工参与和个人结算成功不能写成击退 Boss。公告的 60 秒入场、约 300 秒个人战斗、约 7 分钟总时限也与新来源一致。旧报告把公告发布时间写成 13:40:17，已按原消息日期更正为 13:40:14。

证据边界与适配结论：

- 这是一条有官方结算原文留存的人工参考。用户在当日消息 41699 和本轮均说明人工参与；不能再因协议 runner 没有回执，就断言该账号整场没有参战。
- 榜单的「强攻 16」与 3760 分是汇总结果，不提供逐击 `perfect`、全部动态窗口 ID 或 `/finish` 原始响应。旧报告将 widget callback、proof 消费及全部请求路径写成直接观测，现已收窄为实际可证的结论。
- 当前官方源码 967–1039 行由 widget callback 在页面内生成本次验证材料并再次调用 `startBattle()`；1765–1800 行在 `/begin` 中消费，随后清除。人工结算与此路径一致，但历史记录没有说明该次 Turnstile 是自动放行还是经过人工交互，亦没有保存 callback/HTTP 抓包，无法从榜单重建该步骤。
- 当日协议 `/start` 记录与同场人工结算并存。不能仅凭先调用过 `/start` 就断言后续官方页面必然无法参战；这也不授权两个执行者并发或重放请求。
- 公告与结算由不同官方 Bot 发出。仅按一个 Bot 搜历史会漏掉真实结算，后续证据关联必须考虑这一点。当前生产仍只接受用户提供的群、消息和已核实的入口 Bot；历史检索发现不构成扩大生产来源范围的授权。

本轮对旧消息的 Telegram 回读返回无正文；公开群用户名仍解析为正确旧群 ID，因此上述原文明确标为**当日 API 输出的档案恢复**。新群 2026-09-08 14:36–14:46 的有界历史读取未找到该账号结算；官方 Bot 私聊也没有相关消息。Surge 当前只保留连接级近期请求，未得到当次验证或战斗 HTTP 回执。

生产只读复核：**2026-09-09 04:50:20 +0800，PID 49784、qyz-protocol-v5**，六账号连接、身份、成员和新来源均通过；心跳 26–60 秒，事件数均为零，状态仍为 `blocked_entry_expired`，blockers 为 `entry_expired` 和 `official_verification_required`。本轮只补证据与纠正报告，没有重启、修改执行路径、请求签名 WebView、发送 Telegram 消息或提交游戏 POST。六账号自动战斗目标仍未完成。

## 2026-09-09 v6 漏窗、反击与倒地结算适配

上一目标轮次恢复了人工结算的原始留存，属于新增有效证据。本轮重新检查现行代码与官方页面后确认另一个实际差异：v5 在窗口已过期或释放偏离时立即抛出 `local_window_missed`，因此无法像官方页面一样继续处理后续窗口，也没有 `/finish`。这会使已有服务器确认攻击缺少本轮最终结算。

本次按官方 HTML 的 `releaseSeal`、`processMissedWindows`、`bossCounterDamage`、`applyBossDamage` 和 `submitResult` 调整：

- 已过期窗口不提交 charge-start/hit，在官方 +80ms 宽限后记录本地漏窗、扣除阶段反击伤害并重置 combo；后续有效窗口继续执行。
- 已真实蓄力但释放过晚时，保留实际 `{t, holdMs, stance}` 动作；未命中且超过 1800ms 时记录额外 8 伤害。该 charge 请求须先得到确定响应，才允许后续结算，票据不重放。
- 倒地后停止攻击、取消尚未使用的下一窗读取；等已提交攻击回执及 1250ms 死亡动画，再提交实际 playerHp/dead/actions/clientStats。死亡流程依官方页面允许早于普通 minDurationMs 结算，不虚构未处理窗口。
- 本地漏窗与服务器接受回执分别归档；`missed_windows`、`local_player_hp`、`local_dead` 随最终结果写入健康状态。发生漏窗、倒地或缺少任意完美回执，最多是 `settled_partial`。
- 健康检查要求完美结算同时有零漏窗、明确未倒地、全部逐窗接受与 perfect 数及 finish 回执；测试也确认已有漏窗/倒地不能被一致的汇总数字掩盖。HTTP 不确定结果仍阻止结算与重放。

验证：5 项针对真实差异的场景先 RED（含 4 种 phase 子场景），修改后通过；另有健康矛盾状态先出现 4 个失败后通过。最终青元子 **73 tests OK**（包含现有真实 monotonic 时钟延迟测试）、健康 **97 tests OK**、无网络 `--check` 和 diff whitespace check 通过。这些为合成响应回归，不是现场战绩。

05:55:43 +0800 确认原 PID 49784 仍运行 v5、六账号均有新鲜心跳及零事件后，使用 SIGTERM 让原 KeepAlive 服务加载 v6，没有新建或停用其他服务。**05:55:53 +0800，实际新 PID 33709**，六账号均产生重载后的来源/策略检查：connected/identity/membership/source 全 true，仍为 `blocked_entry_expired`，blockers 为 `entry_expired` 和 `official_verification_required`，事件数均为零。未发送 Telegram 消息、未申请签名 WebView、未提交游戏 POST。

本次确实补齐了战斗行为差异；完整目标仍缺可执行的官方验证路径、当前有效且获授权的场次，以及六账号真实 begin、全部动态窗口 hit 和 finish 回执。不能将 v6 部署或离线测试计为全自动实战完成。

## 2026-09-09 06:18 完成度与阻断复核

上一目标轮次的 v6 部署属于实质进展，本轮不重复运行已通过且没有新增修改的测试。06:15:37 +0800 实际检查 launchd 与 ps：原 PID 33709 仍运行，六账号均为 v6，心跳 42–60 秒，独立来源/策略检查新鲜，事件数均为零。没有因观察超时或业务阻断重启服务。

06:18:23 +0800 再以 @demo_player_01 的内存 Telegram 会话直接读取用户指定消息，确认群 -1009400000005、消息 1075814、实际 bot 发件人 hantianzun31_bot、有效按钮及官方 60 秒入场说明。消息发布时间仍为 2026-09-08 14:37:00 +0800，已过去约 56484 秒，entry_fresh=false。随后官方页面 GET 再次返回同一 SHA256、Turnstile enabled=true。本轮无游戏 POST、无签名 WebView 请求、无 Telegram 消息发送。

| 原始要求 | 当前权威证据 | 完成度 |
| --- | --- | --- |
| 完整解析 Mini App 青元子 | 官方公开 HTML、端点/字段/输入/时序/伤害及死亡流程已映射；当前页面哈希未变 | 公开客户端已解析；认证态完整契约及后端行为仍缺现场证据 |
| 根据解析适配 XIUXIAN 青元子模块 | 现行 auth/http/protocol/host、六账号隔离、v6 实际进程、73 项 QYZ 与 97 项健康回归 | 已部署的协议适配有效；官方验证执行能力仍缺，整体未完成 |
| 删除相关冗余代码 | 旧 log_utils.py 实际不存在，git diff 记录删除 5954 行；生产 Python 文件中无旧 log_utils/browser/proof runner 导入 | 已完成确认无生产依赖的旧代码清理；未改动其他 Boss 模块 |
| 必须确保正常战斗 | 必须有六账号当前事件各自的 begin、所有动态窗口 hit 和 finish 回执 | 缺失；六账号事件数为零，离线测试及旧人工单账号战绩不能替代 |
| 实现自动化 | 持续授权入口、可执行的官方验证、无人值守完整结算 | 未完成；指定入口过期，后续公告范围未答复，当前允许的执行方式无法运行 widget callback |

这是用户恢复目标后连续第三个目标轮次复核到同一验收阻断：人工参考恢复轮次、v6 实现轮次、当前复核轮次均有过期入口与官方验证缺口。前两轮的实质进展已保留，当前没有可继续推进真实参战的已授权路径；继续追加 fixture、调整 timing、重启或重复状态说明都不能消除此阻断。按目标阻断审计规则标记 **blocked，绝不 complete**。恢复所需的是有效且获授权的场次与在现有约束下可运行的官方验证方式；不得使用旧 token、未授权浏览器/注入/worker/sidecar 或人工 proof 交接。
