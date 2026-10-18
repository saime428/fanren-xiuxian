# 档案：事件应答类流程（auto_reply_topic_event.py 家族）

2026-10-09：神迹布道由原事件监听调用 MiniApp `small-world / miracle_sermon`，不再发群命令。
同一事件的 API 结果不明时保留待确认，不盲目重复布道。其余事件回复仍需群上下文。
见[天机阁迁移说明](miniapp-command-migration.md)。

共同机制：常驻 Telethon 监听（`KeepAlive`），在官方消息命中触发标记且明确归属本账号时回复一条固定指令；
10–30 秒历史对账补漏，重启后延续同一事件；只认归属本账号的官方结果。

| 流程 | 触发 | 回复 | 账号 |
| --- | --- | --- | --- |
| xuangu_zuoda 玄骨作答 | `神念直入脑海 / 天机异象 / 乾蓝冰焰…` | `.作答 X`（题库映射，`xuangu_quiz.py`） | 6 账号 |
| jiyin_choice 极阴祖师 | `回复本消息` 的极阴祖师选择 | demo_player_01 `.献上魂魄`，其余 `.收敛气息` | 6 账号 |
| jiaohuan_gongfa / jiaohuan_fabao 南陇侯交换 | 南陇侯 `回复本消息` | demo_player_01 `.交换 功法`，其余 `.交换 法宝` | 6 账号 |
| shenji_budao 神迹布道 | `小世界·天降浩劫` | `.神迹 布道` | demo_player_01 |
| recipe_notice 配方通知 | 额外奖励公告（只读） | 不回复；通过 Hermes 通知 | demo_player_01 |

**与 0 点重置的关系**：无日配额，事件何时来何时答；不占用凌晨槽位。

第二元神心魔使用独立的现有 `yuanshen.return_observer`，不属于上表通用监听器家族。
2026-10-15 已按用户要求为 @demo_player_01 改为回复原警示 `.抉择 稳固道心`，并核验线上加载，
见 [心魔归属、去重和结果衔接](demo_player_01-heart-demon.md)。

**迭代提示**：改题库只改 `xuangu_quiz.py`；新事件先用真实公告文本写 `TG_TRIGGER_MARKER_TEXT`，并在
`test_*_reply_contract.py` 里加回包契约测试。
