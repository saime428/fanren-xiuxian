# 南宫阙 MiniApp：协议、回放与投产门禁

## 当前安全状态

- 旧 LaunchAgent、轮询 shell 与真仙战场 Playwright 路径已停用并移至废纸篓。
- 新模块仅提供协议构建、响应 reducer、账号规划和脱敏离线回放。
- `nangongque_config.json` 固定为 `enabled=false`、`mode=shadow`、`network_enabled=false`。
- 未知机制不生成动作；在真实脱敏 capture 补齐之前，不安装生产 LaunchAgent。

## 独立协议

仅允许 `nqb_` 入口，API 固定为：

1. `POST /api/miniapp/xianxia-nangongque-boss/start`
2. `POST /api/miniapp/xianxia-nangongque-boss/state`
3. `POST /api/miniapp/xianxia-nangongque-boss/input`
4. `POST /api/miniapp/xianxia-nangongque-boss/claim`

所有请求预算均为单次 transport attempt。`start`、`input`、`claim` 出现不确定传输结果后不得重发，只能通过只读 `state` 对账。

## 状态机

```text
new
  -> start
needs_state
  -> authoritative state
needs_decision | blocked_unknown_mechanic | needs_claim
  -> explicit, fixture-backed decision
input_uncertain
  -> authoritative state reconciliation
needs_state | needs_claim
  -> claim
claim_uncertain
  -> server claimed=true
complete
```

任一终态错误、身份不一致、room 不一致、缺少 sessionToken、未知机制均 fail-closed。

## 严格账号归因

业务账号固定为六个：

- `tg9100000001`
- `tg9100000002`
- `tg9100000003`
- `tg9100000004`
- `tg9100000005`
- `tg9100000006`（业务身份 `@demo_player_06`，凭据来源 `tg9100000007`）

`tg9100000007` 只能作为 credential source，不能成为业务账号、状态目录或结算归属。状态路径固定为：

```text
qingyuanzi-new-source/state/accounts/<business-account>/nangongque_boss/
```

## 并发入场规划

`build_admission_batch()` 以同一 `entry_ts + join_window_seconds` 生成六账号共享 deadline。它只生成独立账号 plan，不串行执行完整战斗，也不创建网络连接。

## 离线验证

```bash
cd qingyuanzi-new-source
../.venv/bin/python3 nangongque_shadow.py --check
../.venv/bin/python3 -m unittest -v test_nangongque_protocol.py
```

离线 fixture 可用 `--replay fixture.json`；输出仅保存脱敏后的 reducer summary，权限为 `0600`。

## 解除 shadow 门禁的必要条件

以下条件必须全部完成，才能另行开发/启用 live runner：

1. 收集每种真实机制的脱敏 `/state` 与 `/input` response fixture。
2. 为躲剑丝、破宝镜、镇血煞、护南宫婉逐个建立 reducer/action-policy 测试。
3. 每个非幂等请求超时后只允许 `/state` 对账，测试证明不会重发。
4. 单账号 canary 验证同 actor、entry、room 的 settlement 与 `claimed=true`。
5. 六账号 session 逐个 `get_me()` 校验业务 Telegram ID；demo_player_06 映射不得串归因。
6. 加入 active flow registry、healthcheck、stale/hang、锁和日志保留测试。
7. 完成专项、全量、影子、单账号 canary 后，才允许注册 LaunchAgent。

在这些证据出现前，生产发送保持关闭。
