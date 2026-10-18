# XIUXIAN 运维报告后续检修（2026-09-11）

本轮继续核对健康快照、告警输出、QYZ 官方入口变化，以及一个新出现的返回观察器故障。所有时间为 Asia/Shanghai。

后续极阴、玄骨、双修重试及配方通知筛选的修补和最终验收，见 [问答与通知修补记录](question-reply-repair-2026-09-11.md)。

## 已修复

### 1. QYZ 报告保留真实失败原因

两次连续健康快照均确认六个业务账号的 `qyz_miniapp` 为真实 `bad`。旧报告只显示通用的 `qyz_preflight_blocked`，没有暴露最新账号事件中的安全错误码。

健康检查现在读取最新事件的经过白名单格式校验的错误码，并报告：

`qyz_latest_event_error=official_webview_origin_invalid`

它不会把 QYZ 改绿，也不会压制告警；六账号仍严格分开计数，只在告警层按同一根因合并展示。

一次只读、已授权账号的 WebView RPC 探针仅输出 URL 结构，确认当前官方群入口 `12267212` 返回：

- 官方 bot：`sample_peer_06`
- scheme/host：`https://asc.aiopenai.app`
- 新路径：`/miniapp/xianxia-verify`
- 原生产预期路径：`/miniapp/xianxia-world-boss`

官方页面的实际路由代码已核对：`qyz_` 入口在拖动验证界面初始化前转到 `/miniapp/xianxia-world-boss`，并原样保留含 Telegram 签署资料的 fragment。19:53 再次使用生产 HTTP transport 匿名读取页面，HTTP 200，内容 SHA-256 与 14:03 的证据一致。

修补允许这两个明确的官方路径，继续绑定账号 ID、username、签署入口、所有外层入口参数与有效期；其他路径、异域、重复或冲突参数均拒绝。原有青元子 Turnstile 流程保持不变，不新增浏览器认证资料交接或拖动证明。六个失败事件仍保留，只有下一场的服务器战斗回执才能证明实战恢复。完整复盘见 [今日青元子战斗复盘](qyz-battle-review-2026-09-11.md)。

### 2. `@demo_player_06` 元婴返回观察器恢复

健康检查捕获：

`tg9100000006/yuanyingchuqiao [return_observer] = monitor_failed (history_scan: PermissionError)`

根因是观察器引用的业务账号发送 wrapper 丢失可执行位：

`scripts/send_yuanyingchuqiao_tg9100000006.sh` 为 `0644`，其他同类 wrapper 均为 `0755`。

已恢复为 `0755`，并增加回归测试：每个元婴返回观察器引用的 `TG_SEQUENCE_SEND_WRAPPER` 必须存在且可执行。测试也已改为使用当前业务账号 `tg9100000006/@demo_player_06`，同时保留其凭证来源 `tg9100000007`，不再把凭证来源当业务账号。

恢复可执行位后，后续自然排程能够调用该 wrapper。测试覆盖所有元婴观察器引用的 wrapper，而非只核对一个文件。

### 3. 抚摸法宝话题根回执

话题根回执需要可验证的账号归属。新增回归测试确认：只有消息 ID 相邻、同话题及成功文案的回执保持未确认，不能据此写入账号成功记录。本次没有加入依靠相邻 ID 猜测归属的 fallback。

### 4. session 探测保留有效凭证

`ensure_session_authorized.py` 原先按错误文本识别网络异常，漏掉了 `IncompleteReadError` 的 4 字节变体。Telethon 的 `is_user_authorized()` 也会把限流和服务器 RPC 错误转成 False。

修补使用带共享并发限制的连接及只读 `GetStateRequest`：只有 Telegram 明确的未授权错误允许进入 fallback/reseed；其他失败保留原 session。没有现存 session 或 fallback 状态未知时，退出 75 等待自然重试。统一断开连接并消费终止 future，避免重复的未处理异常日志。

## 告警策略核对

- 六个 QYZ 失败账号继续独立计数，错误原因经过安全格式校验后进入报告。
- 正常等待、cooldown 和短暂发送状态保留既有分级；没有停用健康检查或修改通知去重状态。
- 没有发送 Telegram 测试消息，没有手工触发 Hermes 通知。

## 当前结论

青元子 93 项、相关运维 133 项离线测试通过。今日六账号均在 WebView 阶段失败，开战、攻击及结算回执为零；入口修补与真实实战成功分开验收。此轮没有全量重跑其他独立项目，也没有以离线结果代替服务器结算证明。
