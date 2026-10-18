# 新副本架构完整解析

## 结论

当前仓库包含两套不同层级：

1. `qingyuanzi-upstream-exact`：完整副本域实现与测试参考。
2. `qingyuanzi-new-source`：当前生产 Qing Yuanzi 六账号 host，仅接入世界 Boss，**未接入副本域**。

因此现状是“架构和完整实现存在于上游镜像，但生产 host 尚未集成”，不能把上游文件存在等同于生产副本自动化已经运行。

## 一、支持的副本类型

| kind | 名称 | 门票/资格 | 开启 | 加入 | 进入 | 默认成功 CD |
|---|---|---|---|---|---|---|
| `virtual_hall` | 虚天殿 | 虚天残图 | `.开启虚天殿` | `.加入副本 <ID>` | `.进入虚天殿` | 125 分钟 |
| `zhuimo` | 坠魔谷 | 坠魔谷禁制令 | `.开启坠魔谷` | `.加入坠魔谷 <ID>` | `.进入坠魔谷` | 2 小时 |
| `huanglong` | 黄龙山 | 黄龙急援令/宗门版 | `.开启黄龙山` | `.加入黄龙山 <ID>` | `.进入黄龙山` | 125 分钟 |
| `cangkun` | 苍坤洞府 | 苍坤残图 | `.开启苍坤洞府` | `.加入苍坤洞府 <ID>` | `.进入苍坤洞府` | 2.5 小时，可配置 0.25–24 小时 |
| `kunwu` | 昆吾山 | 昆吾通行令 | `.开启昆吾山` | `.加入昆吾山 <ID>` | `.进入昆吾山` | 125 分钟 |
| `luoyun` | 落云秘圃 | 无票，但有宗门/境界/贡献/掌天瓶条件 | `.开启落云秘圃` | `.加入落云秘圃 <ID>` | `.进入落云秘圃` | 48 小时 |
| `xiaojigong` | 北冥小极宫 | 元婴初期成员门槛，职业组队 | 由对应入口管理 | `.加入小极宫 <ID>` | `.进入小极宫` | 72 小时 |

血色试炼明确排除在这套自动加入架构外。

## 二、控制面与数据面

### 控制面

- 日志/副本群接收：
  - `.查询副本`
  - `.副本cd`
  - `.查询虚/.查询坠/.查询黄/.查询苍/.查询昆/.查询落/.查询小`
  - `.开启副本 @用户名 <类型>`
  - `.加入副本 @用户名 ...`
  - `.解散副本`
- 按钮 callback 仅管理员可执行。
- query/open/join/dissolve 操作均转换为明确的状态机事件，而非直接无状态转发。

### 游戏数据面

- 自动副本只向游戏群发送真实游戏命令。
- 查询、CD、队伍建议、失败解释和结算摘要留在日志/UI 层。
- 生产对接时必须把所有 outbound 固定到 `@example_game_group` topic `7310786`。

## 三、核心状态机

### 开房 flow

`opening -> opened -> entered -> settled/dissolved`

关键字段：

- `flow_id`
- `replica_kind`
- `replica_chat_id`
- `leader_identity_id/leader_username`
- `open_command_msg_id`
- `room_id`
- `phase`
- `expires_at`
- `last_error`

开房发送结果未知时保持 `opening`，等待服务器开房广播，禁止盲目重发。

### 加入状态

每身份分别记录：

- `participating`
- `room_id`
- `joined_at/active_until`
- `cooldown_until`
- `pending_msg_id/pending_room_id/pending_until`
- `retry_count`
- `last_result/last_error`
- settlement context

### 房间状态

- `pending_open`
- `last_room_by_chat`
- `room_history`
- team usernames / identity IDs / profession map / capacity
- opened/entered/dissolve_requested/dissolved

房间历史有 TTL 和数量上限；旧的 opened 广播不得覆盖 entered/dissolved 状态。

## 四、可靠性与幂等

- event claim：同一事件只处理一次。
- `op_id`：单次操作唯一键。
- `chain_id`：同一房间/阶段操作链。
- 按钮 exclusive group：同一阶段多选一，防止重复点击不同选项。
- stage guard：旧阶段按钮过期后不再发送。
- 房间/身份 reserve：同一身份不可重复加入。
- 发送未知：等待服务器回包或广播，不盲重发。
- 快速重试默认只允许 join/dissolve；开房不是普通重试动作。
- 加入 throttle：5 分钟窗口，最多 3 次。
- 加入成功/冷却/失败均要求服务器文本或直接回复证据。

## 五、组队规划

### 苍坤洞府

- 最低境界：结丹初期。
- 必需职业：破军、御山、灵医、影刃、咒师。
- 化神初期以上可作为神识候选；队内要求可调神识约 1000。
- 优先太一门；灵根排序天/异/真/伪。
- 能构建多队计划、职业覆盖、神识持有者、备用队员。

阶段策略：

- 第一幕：默认匿踪潜行，检查神识。
- 第三幕：根据警戒、神魂、贪念、线索决定卷轴或偏殿。
- 第四幕：全员各表态一次，有 stage guard 和 75 秒提醒。
- 第五幕：高警戒优先夺图撤离，避免继续贪。

### 坠魔谷

- 必需五职业。
- 心契/心劫和队长物资会影响选择。
- 队长物资：路线图、毒囊、阴环。
- 默认路线：第一幕路径2稳守；第二幕选1斩念。
- 每次抉择建立进度回执；35 秒无新进度只告警，不自动重复发送。

### 昆吾山

支持两套阶段：

- 封魔主线：山门、遗宫、八灵尺、化龙玺、黑风旗。
- 通用岔路/奇遇：奇遇 > 战斗 > 朱果 > 采集 > 捷径。

昆吾是当前代码中自动化最深的类型：可自动选路，3 秒后在严格条件下最多补一次。

### 黄龙山

- 每日 12:05 后查询轮值宗门征调。
- 按日去重，失败按 retry interval 重试。
- 决策变量：阵势、军心、敌警、战果、情报。
- 神师将至时只有阵势/军心高且敌警低才考虑追击。

### 落云秘圃

- 需要落云宗、结丹后期、贡献约 420、掌天瓶等条件。
- 决策仅由队长执行。
- 成功 CD 48 小时，并有独立 CD 提醒调度。

### 虚天殿

- 有独立匹配/踢人/自动开房状态机。
- 阶段包括冰火路、阵策、鼎前抉择、后殿余波、后殿试阵。
- 有显式 oracle 成功/失败历史库；未知卦象应提示，不应凭相似标题盲选。

### 北冥小极宫

- 元婴初期成员门槛。
- 使用职业组队模型。
- 成功 CD 72 小时。
- 当前 ticket meta 不像其他副本那样纳入通用开票优先级，入口/组队需要专门路径。

## 六、门票来源链

实时消息表明新副本资源不是单纯购买：

- `.入梦寻图` 在虚天/苍坤线路中获取残纹。
- 进度按 `0/4..4/4`。
- 进度越高，掉落率存在衰减。
- `.残图` 查询详情。
- 集齐后 `.拼图` 触发副本机缘/门票。

副本打开时扣票；房间解散时服务器文本可返还门票；所有增减由服务器文本写入储物袋 delta，不能本地臆测。

## 七、消息解析链

生产完整链应为：

1. 游戏群消息进入统一日志。
2. `record_game_group_message` 建立短时 inbox。
3. 开房广播解析 `leader + room_id + kind`。
4. join 回复按 `reply_to_msg_id`、sender identity、room ID 归属。
5. 队伍快照更新 usernames/职业/capacity。
6. 进入广播把房间转为 entered。
7. 阶段文本生成按钮或自动策略。
8. 结算文本解析参与身份，写成功 CD、物品 delta、审计摘要。
9. 解散/自动解散清理房间，按服务器文本返票。

## 八、安全边界

- 身份、账号、session source 三者分离；所有证据归业务身份。
- 未知发送不可直接重试。
- 旧按钮、旧房间、旧阶段均由 scope/TTL/stage guard 淘汰。
- 解散需要二次确认；房间已变化、已满或已进入时拒绝旧解散操作。
- 只能由管理员执行日志 bot 按钮。
- 外部 dispatch 默认关闭：`_REPLICA_EXTERNAL_DISPATCH_ENABLED = False`。
- dungeon quiet 可阻断副本命令，避免全局故障时继续突发发送。

## 九、当前生产差距

当前 LaunchAgent `ai.hermes.xiuxian.qingyuanzi-six.monitor` 的工作目录为 `qingyuanzi-new-source`。该目录的当前 host 只包含 World Boss 集成，搜索未发现副本模块导入、handler、状态或 active-flow 注册。

完整副本实现位于 `qingyuanzi-upstream-exact/model/app_replica.py`、`features/join_dungeon.py` 等，尚未成为生产调用图的一部分。

所以当前不能宣称：

- 六账号副本 listener 已运行；
- 自动开房/组队/进本/选路已生效；
- 副本 CD 或票据状态已同步到生产。

## 十、建议的集成边界

不要直接把 62 万字节的 `app_replica.py` 粗暴复制进生产 host。正确方式：

1. 先固定生产账号/身份映射和唯一 outbound topic。
2. 抽出状态、身份、发送、日志、按钮 callback 五个 port。
3. 先只读接入 query/panel/CD/房间广播解析。
4. 再接入开房和加入，发送未知一律 reconcile。
5. 再按副本逐个接入阶段策略：昆吾 -> 坠魔 -> 黄龙 -> 苍坤 -> 落云 -> 虚天 -> 小极宫。
6. 每个类型使用真实消息 fixture 和状态机回放测试。
7. 在 production 启用前先 shadow 模式，只记录“本来会发什么”。
8. 最后用一个明确账号、一个房间做在线验收，再扩六账号。

这是高风险、有状态、多身份的生产迁移；需要完整 call-graph、回放测试和服务器证据，不能用文件存在或 import 成功代替集成验收。
