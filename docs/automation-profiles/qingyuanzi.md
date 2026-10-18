# 档案：青元子世界 Boss（qingyuanzi-new-source/）

**目的**：官方约 13:40 发布入场公告后，六账号进入真仙战场 MiniApp，完成 `/start → /begin(Turnstile) → /window → charge-start/hit ×16 → /finish`，
目标 16/16 PERFECT。结构、逐环节分析与本周成绩见 `docs/qyz-battle-review-2026-09-20.md`；
09-21 整场未入场的根因与 v22 修复见 `docs/qyz-battle-review-2026-09-21.md`；
09-22 一周趋势、伤害机制回归与 v23 战斗线程重构见 `docs/qyz-battle-review-2026-09-22.md`。
10-02 的96/96命中、74 PERFECT、末窗六号共同慢charge及002中途降伤，见 [10-02复盘与可靠性修正](../qyz-battle-review-2026-10-02.md)。本轮只修复半截HTTP响应的暖连接误判并补充分段延迟诊断；不改变蓄力/出手参数、请求数量、路由或反自动化规避参数。代码测试已过，在线启用状态以该复盘的部署记录为准。

2026-10-04 已在 VPS VPS 完成 6/6 正式结算、93/96 命中、68 PERFECT。
常态请求约110–120ms；新的主要问题是动作请求长尾和历史漂移反馈的反向调整，
见 [VPS首场复盘与优化优先级](../qyz-battle-review-2026-10-04-vps.md)。该复盘没有改变战斗代码。

2026-10-07 已完成战报更正及监听恢复：实际4/6结算、64/96机会命中、48 PERFECT，另两号因监听失活缺席。
修复原消息发布时间、后台循环监督和有效期内共享入口读取重试；更正消息34895已投递且多周期无重复，六号监听恢复、130份战斗档案不变。
同四号较昨日少6 PERFECT、伤害−7.12%；16次普通命中逐窗归类及V32补偿验证见[10-07复盘与已发布可靠性修复](../qyz-battle-review-2026-10-07-vps.md)。战斗时间参数保持v32/v3，本次未触发新战斗。

**入口**：`qingyuanzi_host.py`（六账号常驻监听，LaunchAgent `ai.hermes.xiuxian.qingyuanzi-six.monitor`，KeepAlive）；
战报 `qyz_battle_report.py`（`ai.hermes.xiuxian.qyz-battle-report`，60 秒只读 tick）。运行版本 `qyz_state.RUNTIME_VERSION`（v32，2026-10-06 在v31到达预算基础上加入已确认慢蓄力票据的最多20ms等待余量；见 [本场复盘与v32验证](../qyz-battle-review-2026-10-06-vps.md)）。

**优先级**：最高。`miniapp_shared/priority.py` 让星宫 / 问心塔 / 游历 / 宗门日常 / 垂钓 / 天机在 13:36–13:52（或入口后 12 分钟内）全部让路；青元子自身不走全局发送闸、不受错峰时刻表约束。

**调度**：不属于凌晨批次。13:39:30–13:41 每 5 秒扫描入口，其余 15 秒，本账号入口处理中时暂停扫描；Chrome 启动限 2 并发、页面加载限 3 并发。

**战斗执行（v23）**：每场战斗在账号自己的线程与事件循环里跑（`_run_battle_isolated`），宿主循环只留 Telegram、健康与日志；
轨迹按顺序回传，`start/begin_submitted` 同步落盘。已归档事件在 journal 里瘦身（`qyz_state.slim_event_record`），完整证据只在
`battle_diagnostics/<入口ID>.json`。本地判死只停止打击不停表，`/finish` 按真实时长提交。

**状态**：`qingyuanzi-new-source/state/accounts/<账号>/world_boss_state.json` 与 `battle_diagnostics/<入口ID>.json`；
共享 `state/world_boss_shared_entry.json`、`state/world_boss_shared_board.json`（官方结算榜）。

**通知**：每场一条 `【青元子战报】`，含前三名、本方名次与分差；若官方结算榜比最近一次入场记录更新（宿主整场未入场），
以榜单消息 id 为键发一条“本方未参战 0/6”战报。健康检查同时报 `qyz_event_missed: board=<id>`。

**入口判定规则（09-21 教训）**：先提取 `qyz_` 深链再做任何文本判断；入口公告正文本身含“可查看贡献榜”，
任何按关键字短路的分支都必须在深链提取之后。失败记录超过 30 分钟后不再算“待核对”（`qyz_state.event_needs_reconciliation`），
进程重启恢复仍用无时限规则。

**网络**：生产已迁至 VPS VPS，游戏 API 使用 `HttpTransport` 直接建立连接（`trust_env=False`）。
旧 M2 的 Surge `[MTProto]` 127.0.0.1:5753 及 `ai.conf → AI → 🇺🇸 美国节点` 路径只属于迁移前历史；
当前优化应以 VPS 的正式请求诊断为准。

**成绩目标（09-22 回归）**：伤害 = PERFECT×1.41 × hold（+8.7%/100 ms）× 连击序号（+2.2%/窗）；|delta| < 100 比 < 210 多 9%。
前三 = 16/16 PERFECT、|delta| < 100、hold 1000–1100；本方 09-18 的 16/16 已打出 7.16 亿亿（封顶约 7.21）。hold 计划保持 1000。

**迭代提示**：只改本地模块（`qyz_http / qyz_protocol / qyz_timing / qyz_verification`），参考脚本受保护函数有哈希锁
（`test_qyz_reference_fidelity.py`）。判断修复成功只看服务器回执（`window_reveal.error_count`、`request_lead_reviewed.lead_ms`、PERFECT 数、榜单名次）。

**拟人化适配**：浏览器轨迹／按压、操作间隔／微休息、网络退避／请求头顺序和扫描／心跳抖动
已接入本地适配层，参数和验证方式见 [青元子拟人化适配](../qingyuanzi-humanization.md)。
服务器战斗蓄力与命中时间仍按原协议执行；v28 的运行心跳包含
`humanization_version=qyz-humanization-v1`，可核验监控进程已加载。
