# 档案：星宫灵圃（star_observatory）

**目的**：`xianxia-sect-farm` 子应用：一键安抚 → 一键收集 → 向每个空盘牵引 **天雷星**。
账号：@demo_player_04、@demo_player_05（本人身份），@demo_player_01 只用绑定角色 示例绑定角色（`state.IDENTITY_SCOPE`）。

**入口**：`star_observatory/run_cycle.py`；LaunchAgent `ai.hermes.xiuxian.star-observatory.send`（5 分钟 tick，`--force` 可立即一轮）。

**调度**：无日重置；按服务器盘位计时器（约 36 小时一批）`next_due_at` 休眠，最长 2 小时复查一次以便及时安抚 星光黯淡/元磁紊乱。
tick 与凌晨批次错开：只有计时器到期才登录。

**状态**：`state/accounts/<账号>/star_observatory/state.json`，`state/star_observatory/summary.json`。
只有服务器 `actionResult/domain` 确认动作；传输不确定时记 `unknown_mutation_outcome`，30 分钟内盘面未变则等待，不重放。

**通知**：`star_notification` → `【星宫灵圃】`，每批一条（含身份名）。

**迭代提示**：只允许牵引 `protocol.STAR_NAME`；换星只改常量并加测试。
