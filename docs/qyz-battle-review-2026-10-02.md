# 青元子复盘与可靠性修正：2026-10-02

时间基准：Asia/Shanghai。入口 `12586536`，服务端 event **143**，官方榜单消息 [12586587](https://t.me/example_game_group/7310786/12586587)。本地榜单快照时间 13:44:56，不当作 Telegram 原始发布时间。

## 结论

本场 **6/6 正式结算、96/96 有效命中、74 PERFECT**，全员存活、终局 HP100；不是入场失败或漏打。昨日为 95/96 命中、84 PERFECT：命中补齐了，质量下降了。
官方结果为伐仙功成、50 人参战；本方只有 @demo_player_06 第六、@demo_player_01 第七进入公告前十。

| 账号 | 官方排名 | 精确结算伤害（亿亿） | 命中 | PERFECT | 最低逐击倍率 |
| --- | --- | ---: | ---: | ---: | ---: |
| @demo_player_06 | 6 | 6.85685484 | 16 | 14 | 1.0 |
| @demo_player_01 | 7 | 6.82543222 | 16 | 13 | 1.0 |
| @demo_player_04 | 前十外 | 6.50931926 | 16 | 10 | 1.0 |
| @demo_player_05 | 前十外 | 6.29462474 | 16 | 12 | 1.0 |
| @demo_player_02 | 前十外 | 6.14227926 | 16 | 12 | 1.0 |
| @demo_player_03 | 前十外 | 5.85293444 | 16 | 13 | 0.18 |

冠军公布伤害 7.36 亿亿；第十 @sample_peer_17 为 3681 贡献分、6.73 亿亿。未上榜账号的准确总排名/贡献分未知，不把个人结算的甲等100分混作贡献榜分数。

## 两类独立损失

### 1. 六账号末窗同时等到慢 charge 回执

第16窗 charge 发出时间集中在 **13:42:44.874–13:42:45.008**（相差约134ms），全部在13:42:46附近返回。

| 账号 | window RTT ms | charge RTT ms | server hold ms |
| --- | ---: | ---: | ---: |
| demo_player_01 | 970 | 1165 | 566.4 |
| 001 | 847 | 1213 | 528.8 |
| 002 | 1055 | 1084 | 515.1 |
| demo_player_04 | 912 | 1243 | 518.8 |
| 000 | 1031 | 1147 | 528.2 |
| demo_player_06 | 928 | 1106 | 445.7 |

这些慢请求都复用已建立的 HTTP 连接，没有冷连接/TLS重握手。末窗从 body 发完到 response headers 的等待为977–1051ms；共同异常在发出后等待回覆的区段。客户端证据不能再细分代理链路、传输与服务端排队。

001末窗的 release target=86041ms、charge reply=86333ms、实际release=86339ms：表面的298ms迟发中，**292ms是在等charge回覆，等到回覆后只有6ms延迟**。不能把原 `wake_lateness_ms` 全部当 CPU/线程睡眠问题。各账号全场 wake 中位数仍为2–3ms。

其他损失：001窗6/9/11慢charge、000窗5/9慢charge压短实际蓄力；demo_player_01窗4/11、002窗10、demo_player_04窗7、000窗3/14慢hit导致普通命中。即使本地hold约1150ms，也不代表服务端实际hold足额。

读连接分组统计不支持固定弃用某个socket：5/6账号socket0的p90较差，但000相反；两条连接中位数仅差约1–14ms，>850ms尖峰两条均有。保留两条暖读连接与当前轮换，不凭一次样本改成固定偏好。

### 2. 002 中途真实降伤，终局恢复不能抹掉

逐击官方回执：
- 窗6：倍率 **0.18**、review=true、risk72，伤害8679686亿。
- 窗7–9：倍率 **0.55**、risk52。
- 窗10起恢复1.0。

终局 `automation_review=false` / multiplier1.0 并不代表整场没被限制。继续保留逐击原始结算与最低倍率，不伪造成功，也不实现规避该检查的参数或策略。

## 本轮代码修正

1. `qyz_http.HttpTransport`：原本收到HTTP200标头后，即使正文读取超时/断流，仍按 `http_status != 0` 将该读连接标为暖连接。HTTPX已关闭未读完的流，下一次可能错误选中失效连接。现在必须完整读完正文才设置 `response_complete=true`；半截正文失败标冷，沿用现有另一暖连接及后台冷连接重建。完整的官方409拒绝仍可保持传输连接为暖，不把业务拒绝混作连接损坏。
2. 新增纯数字诊断 `response_wait_ms` / `response_body_ms`，区分等待响应头与读正文；不保存headers、tokens或URL。
3. `qyz_protocol` 新增 `charge_reply_block_ms` / `release_after_ready_ms`，区分等蓄力回执与条件就绪后的本地延迟。不改出手目标、蓄力参数、HTTP请求数量或重发规则。

**证据边界：本日唯一read超时发生在响应头前，未证明今日损失由“半截正文仍标暖”这个缺陷造成。该修正是防止网络故障被客户端恢复逻辑放大，不将它包装成已解决今日共同1秒长尾。** 新增诊断也不会自行降低远端延迟。

## 路由只读对照

当前 `asc.aiopenai.app` 命中 `ai.conf` → AI → Proxy → 美国smart组/AnyTLS。没有改变当前选择。
对公开MiniApp页面作每组两次HEAD probe（不含认证、不发战斗请求）：

| 策略 | 第一次 ms | 第二次 ms |
| --- | ---: | ---: |
| 美国 | 1113.43 | 340.13 |
| 香港 | 1502.98 | 279.30 |
| 日本 | 800.50 | 418.84 |
| 新加坡 | 1465.25 | 389.24 |

全部HTTP200。样本很少，且静态页面与战斗API不同，不能证明改香港或日本能稳定解决尾延迟，因此不盲目切换路由。

## 验证与部署边界

- 传输半截正文超时/断流、读连接故障切换、完整拒绝、时延归因、战斗/时钟/host/健康/受保护参考脚本回归：145 passed、58 subtests passed。
- 战报测试使用注入的fake sender单独运行：8 passed。合计 **153 passed、58 subtests passed**。
- 首次合跑因设置了 `MINIAPP_NOTIFY_DISABLED=1`，战报的模拟投递断言有6项失败；核实全部使用fake sender后取消该环境开关单独重跑通过，没有调用真实通知发送器。
- 用户已明确批准空闲重载。重查入口仍为12586536、六账号均有正式finish后，向旧宿主PID72881发送正常SIGTERM，由原launchd KeepAlive接手；版本标记为 **qyz-reference-v29**。上线核对：新PID **84342**、launchd running、六账号监控心跳均为v29，原战斗结果（含002降伤）保持不变。
- 重载前另一维护会话已新增 account_lifecycle gate；已保留该并行变更并用临时测试状态路径重跑153项测试。生产 `@demo_player_02/tg9100000002` 仍因碎裂处于paused，重载不会解封或绕过该gate。
- 下一场实际成绩改善尚未验证，不承诺这些客户端修正能消除远端长尾或服务器主动降伤。

## 证据位置

`qingyuanzi-new-source/state/accounts/<account>/battle_diagnostics/12586536.json`：`record.protocol_result.server_result`、`reference_outcome.diagnostics.hits`、`window_reveal.log`、`window_states`、`transport_diagnostics`。官方榜单快照：`qingyuanzi-new-source/state/world_boss_shared_board.json`。
