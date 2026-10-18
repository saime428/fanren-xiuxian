# 档案：琉璃问心塔（tower_miniapp）

**目的**：六个业务账号的每个洞府身份（含绑定角色）每天登塔一次（`xianxia-pagoda` 子应用 `start` → `challenge`）。

**入口**：`tower_miniapp/run_cycle.py`；LaunchAgent `ai.hermes.xiuxian.tower-miniapp.daily`。
协议见 `tower_miniapp/protocol.py`（`todayHighest / failedFloor / resetsToday / canChallenge`，回放 `replay`）。

**调度与错峰**：槽位 02:00–02:10。`runner.START_HOUR/START_MINUTE = 2, 0`，账号偏移 0/2/4/6/8/10 分钟，
日历触发 02:00/02/04/06/08/10 + 5 分钟 tick，失败 15 分钟后重试。历史：21:00（09-19）→ 00:00 → 00:20（09-20 错峰）→ 00:35（09-21 垂钓优先）→ 02:00（09-23 用户指定）。

**状态**：`state/accounts/<账号>/tower_miniapp/summary.json` 与 `identities/<playerId>.json`；`state/tower_miniapp/summary.json`。
成功判据：服务器已记录当日登塔（`already_recorded`）或本次 `challenge` 返回 `replay` + 终态 `state`（`settled_confirmed`）。
不确定的挑战只做只读对账，同一天绝不发第二次 `challenge`。

**通知**：`tower_notification` → `【琉璃问心塔】`。

**迭代提示**：`xianxia-pagoda/reset` 不在白名单；如服务器新增楼层规则，只改 `protocol.py` 并用真实回包补测试。

**不确定挑战的补打（2026-09-23 检修）**：09-22 @demo_player_04 00:41 的 `challenge` 请求超时（结果未知），之后每一轮只读回 `start`，
服务器整天显示 `todayHighest 0 / canChallenge true`，旧规则“绝不盲目二次挑战”让它 23 小时没有再打，最后由用户手动完成。
现在：先只读核对；若首次不确定起 10 分钟后服务器仍无当日记录（`tower_miniapp.runtime.REPLAY_AFTER_SECONDS`），重新挑战，
每天最多 2 次（`MAX_REPLAYS_PER_DAY`）。塔的 `start` 计数就是服务器对当日尝试的记录，重打不会重复消耗，服务器日门槛也会拒绝多余的。
身份记录新增 `uncertain_since_ts` / `replay_count`。
