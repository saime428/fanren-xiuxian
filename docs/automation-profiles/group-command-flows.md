# 档案：群指令冷却类流程（send_topic_message.py / send_shendubiguan.py 家族）

2026-10-09：深度闭关、元婴出窍及其状态查询、问道、血洗山林、召唤魔影已接入
[天机阁迁移](miniapp-command-migration.md)。这些流程的服务器结果与到期时间由
`miniapp_commands/` 管理，不再发送群命令。下述群回执格式适用于其余流程及迁移前历史。

共同机制：向 `@example_game_group` 话题 7310786 发送一条文本指令，等待官方机器人直接回复；
成功 / 冷却 / 失败由回复标记（`TG_SUCCESS_MARKER_TEXT`、`TG_COOLDOWN_MARKER_TEXT`）判定，冷却时长从回复解析并写入
`cooldown_retry.json`，下一次在冷却结束后发送。未确认的发送进入 `command_pending.json`，由
`ai.hermes.xiuxian.return-deadline-dispatch`（30 秒）在到期时踢一次同一指令。
所有发送共用全局 1 秒发送闸；每流程独立 Telethon 会话副本与锁（`state/accounts/<账号>/<flow>/`）。

**周期到期检查（2026-10-05）**：三组抚摸法宝、五组探寻裂缝，以及问道、血洗山林、召唤魔影，
共 11 组账号/流程的官方回执 `next_due_ts` 都接入上述 30 秒调度器。
冷却到期或成功后的下一周期到期时，非强制叫起原账号发送器；既有固定间隔仅作兜底，
服务重启不再从头等待两小时/四小时。账号、指令、话题和官方回复身份必须匹配，
暂停/禁用、待确认回复窗口及原发送锁仍有效；普通 job 两分钟内不重复 kick。
玄天斩灵剑的冷却仍由已有专属 helper 负责，成功后的两小时期限由公共检查补齐；
interval 注册的发送器在自身锁内接收回执期限，避免两个调度入口同时管理它。
事件作答、手动斗法、深度闭关和元婴状态探测不使用此周期回执规则。

**监听归属（2026-10-01）**：深度闭关和元婴出窍分别由各账号的 `.shendubiguan.return_observer` / `.yuanyingchuqiao.return_observer` LaunchAgent 启动，各自独立程序、session、锁与 cursor；都执行 `observe_return.py` 只是共用代码，不是寄宿同一程序。[第二元神](demo_player_01-yuanshen.md) 同样独立，由 `tg9100000001.yuanshen.return_observer` 负责其归位推送，状态、专属 session 和锁放在 `state/yuanshen/tg9100000001/`；不能为了少一条连接而挂到深度闭关中。

| 流程 | 指令 | 账号 | 节奏 | 入口 |
| --- | --- | --- | --- | --- |
| shendubiguan 深度闭关 | `.查看闭关` 探测 → `.深度闭关` | 6 账号 | 8 小时冷却；send 60 秒 tick，watchdog 10 分钟，return_observer 常驻 | `send_shendubiguan.py`, `scripts/lib/run_shendubiguan_send.sh` |
| yuanyingchuqiao 元婴出窍 | `.元婴出窍` | 5 账号（demo_player_06 已移除） | 8 小时；观察归来后立即再出窍 | `scripts/send_yuanyingchuqiao_<账号>.sh`, `observe_return.py` |
| tanxunliefeng 探寻裂缝 | `.探寻裂缝` | 5 账号 | 约 12 小时（43050 秒），5 分钟 tick | `scripts/send_tanxunliefeng_<账号>.sh` |
| wendao 问道 | `.问道` | demo_player_06 | 12 小时 | `scripts/send_wendao_tg9100000006.sh` |
| xuexishanlin 血洗山林 | `.血洗山林` | demo_player_02 | 4 小时 | `scripts/send_xuexishanlin_tg9100000002.sh` |
| zhaohuanmoying 召唤魔影 | `.召唤魔影` | demo_player_02 | 8 小时 | `scripts/send_zhaohuanmoying_tg9100000002.sh` |
| fumofabao 抚摸法宝 | `.抚摸法宝 玄天斩灵剑` / `…青竹蜂云剑（神雷版）` | demo_player_01 / demo_player_06 / demo_player_02 | 2 小时；demo_player_01 另有 cooldown_dispatch 60 秒 | `scripts/send_fumofabao_*.sh` |
| shuangxiu_wenyang 双修温养 | demo_player_01 发锚点 `1`，demo_player_03 回 `.双修 温养` | 两账号 | 每小时，60 秒轮询 | `shuangxiu_wenyang.py` |
| doufa 斗法 | `.斗法 demo_player_01` | 5 账号 | 手动激活，每账号每日 5 次配额；“24 小时内已交锋过多”暂停 | `scripts/lib/run_doufa_daily_quota.sh` |

**与 0 点重置的关系**：这些流程按官方冷却滚动，没有固定日槽位，因此不会与凌晨批次同时集中登录；
各自的 tick 只在冷却到期后才发一条指令。斗法配额按日键重置。

**双修温养冷却（2026-10-05）**：可信直接回执包含明确剩余时间时，使用回执时间加剩余秒数及
3 秒缓冲；较新的官方倒计时可覆盖旧成功记录的一小时估计。实时结果和历史恢复共用计算，
重复读取不会延长截止时间。没有具体时长的冷却拒绝仍按一小时兜底，成功仍是一小时周期。

**迭代提示**：新增指令复制一个 `scripts/send_<flow>_<账号>.sh` + plist，注册到 `config/active_flows.json`，
标记文本用真实回复补齐；不要放宽 `command_confirmation` 的官方发送者白名单。

## @demo_player_02 抚摸法宝（2026-10-05）

按 @demo_player_06 模板新增独立 `fumofabao_qingzhufengyunjian_shenlei` 流程：指令为
`.抚摸法宝 青竹蜂云剑（神雷版）`，每 7200 秒调度，`RunAtLoad=false`。
两账号共用发送器实现及完全相同的回复标记、等待、冷却与待确认恢复规则；新账号只使用
`config/accounts/tg9100000002.env` 和自己的 session seed。状态唯一主写者为该账号的新 send 任务，
锁、回执和冷却独立位于 `state/accounts/tg9100000002/fumofabao_qingzhufengyunjian_shenlei/`。
成功必须有匹配官方回执；冷却不算成功，缺回复保留 pending，禁止立即重复发送。
VPS 当前镜像把 supervisor 清单打包在镜像内，单改宿主 manifest 或重启不会加载新条目。
实际调度使用已有 `ai.hermes.xiuxian.return-deadline-dispatch` 的 30 秒 tick，通过注册项
`interval_dispatch_seconds=7200` 启动独立账号脚本；初次安装立即验收，随后每两小时一次。
子进程继承独立 `interval_dispatch.lock`，未结束时不能重复启动，缺回执恢复仍走同一账号脚本。
`interval_dispatch.json` 只保存调度时间和 PID，不能当作官方成功证据。宿主任务清单保持原 109 项，
不添加一个未来重建时会重复调度的独立 job。plist 保留模板配置参考，Mac 不加载发送器。
新增同类账号可通过显式注册与已运行 dispatcher 接入，无需用户手动重建容器。

**到期时间修正（2026-10-05）**：间隔调度器每 30 秒读取该账号发送器写入的
`last_result.json`，校验账号、流程、群话题和官方回执身份，再采用其 `next_due_ts`。
收到冷却回复后，按官方剩余时间加发送器的缓冲到期重试；成功后，从成功回执时间
重新计算两小时。每个回执只同步一次，避免旧的到期回执反复启动脚本。
调度器仅写自己的 `interval_dispatch.json`，业务状态仍由原发送器负责；
缺回执重试、账号锁、暂停和禁用规则继续生效。
