# 青元子：适配用户提供的三个 Telegram 脚本

> 本文記錄 9 月 9 日的 v8 部署。9 月 10 日的漏場復盤、Bot 輪換、重啟恢復及本地扣血適配見 [v9 復盤記錄](qingyuanzi-battle-review-2026-09-10.md)。

本次以用户于 2026-09-09 发给 Hermes 的 `world_boss_browser.py`、`world_boss_turnstile.py`、`world_boss_features.py` 为实现来源。再次发送的附件与此前附件逐文件 SHA256 相同。原件来源、原始哈希、适配后哈希及 110 个保留原文的函数见 `qingyuanzi-new-source/qyz_reference/SOURCE.json`，逐行差异见同目录 `ADAPTATIONS.patch`。

## 实际执行链

生产入口仍是 `qingyuanzi-new-source/qingyuanzi_host.py`，运行版本为 `qyz-reference-v8`。六账号原有身份校验、独立 Telegram 会话、成员校验、账号锁、HTTP 客户端和状态文件继续由 XIUXIAN 主控管理。`qyz_protocol.ProtocolRuntime` 调用参考 `WorldBossMonitor._participate`；入场、身份选择、等待挑战、CF 验证、时钟修正、动态窗口、蓄力票据、命中反馈与 finish 均经过该参考实现。

CF 流程按参考原文执行：先调用不带验证值的 `/begin`；服务器要求验证后创建绑定账号、场次和 challenge 的请求；原 `AutomaticTurnstileWorker` 串行调用原 `NativeTurnstileBrowser.verify`；浏览器加载真实官方 Mini App 页及其当前 Turnstile 配置；只取官方 widget callback；战斗协程一次性消费 token，用新 UUID 向 `/begin` 提交。token 请求不在 HTTP 层重放；被拒绝或结果不确定时按参考上限换新 token。浏览器等待及 Siteverify 耗时不混入战斗网络时钟。

浏览器仅访问公开页面，不接收 Telegram initData、账号 session、签名 WebView URL。生产使用 `MemoryTurnstileBroker` 实现原文件 broker 的同一交接契约，保留随机请求 ID、有效期、消费/接受区分和失败记录。六个账号分别使用绑定账号的 broker 视图；token 在消费、取消、过期时移除，不写临时文件。验证器属于现有主控进程，不增加后台服务或独立调度任务。

## 必要适配

- 浏览器发现增加 macOS Chrome 路径，Mac 使用原生窗口，不要求 Linux Xvfb；保留真实 widget 的验证代码和 worker 调度代码。
- 参考文件改为包内导入，通过 `compat.py` 接入 XIUXIAN 的身份和 HTTP 边界；生产不加载其他玩法的辅助模块。
- 动态窗口去重；过早的 `done` 不能替代服务器声明的窗口总数；处理服务器更新的窗口数。
- 缺少服务器 `hit.perfect` 时不再用本地 perfect 代替；只有本账号的真实命中回执与 finish 都满足声明窗口数，才记录 `settled_perfect`。
- 取消战斗时等待命中任务退出，避免请求跨越账号 HTTP 客户端的生命周期。
- 主控跟进现有官方群 `-1009400000004`、话题 `7310786`、Bot `hantianzun16_bot` 的新公告；每次执行前按实际消息 ID 重读并校验来源和 60 秒入场期限。旧公告过期时等待新场次，不再永远轮询固定旧消息。没有有效入口时不发送游戏请求。
- 健康检查识别新版本及已运行的参考验证器；仍将身份错误、来源缺失、失联、未确认的 mutation 或不完整战果列为异常。

## 验证证据及边界

本机原生 Chrome 运行真实官方 widget，第一次取得 callback 用时 **16.12 秒**。再通过完整 worker、内存 broker 与一次性消费者验证，用时 **9.08 秒**，第二次取用返回空，清理后敏感值数量为零。两次都未使用 Telegram 凭据，也未调用游戏 `/begin`。证据分别位于模块 `state/reports/reference-cf-callback-2026-09-09.json` 与 `reference-service-cf-2026-09-09.json`。

青元子目录 **74 项测试、36 个子用例**通过；相关健康、Mini App 注册表、效率检查 **94 项测试、52 个子用例**通过。覆盖六账号并发各 16 窗、不同动态窗口数、重复/空窗口、CF 换发与幂等键更新、一次性消费、账号越界拦截、取消清理、原脚本时钟修正、无票据不发送 hit，以及不把本地判定计作服务器完美回执。

现有 launchd 主控已重载到 **PID 68795**。六账号状态均来自该实际进程，版本均为 `qyz-reference-v8`，身份、群成员、官方来源和验证器可用性通过，当前状态 `idle_waiting_event`，六项生产健康分类均为 `ok`。只读回读证据保存在模块 `state/reports/reference-production-2026-09-09.json`。

这些离线战斗用例是合成数据，不能充当游戏服务器的实战记录。本次上线时没有未结束的参战任务，最新公告已过期；本次尚无新场次的六账号 begin/hit/finish 实战回执。生产启用后按新公告自动执行，并保留逐账号服务器证据。

另外运行的全仓调度清单检查暴露出已有的非青元子 plist 缺项，以及该清单未描述六账号共用一个青元子主控的映射。没有因此改动其他玩法；青元子的专用启动配置检查和当前 launchd 进程另行核验，不能把相关测试通过表述成全仓所有检查通过。

## 回退

适配前文件保存在 `/Users/operator/.hermes/backups/xiuxian-qyz-before-reference-20260909`，位于生产树之外。原 Telegram 附件保持不变。本次未发送 Telegram 消息。
