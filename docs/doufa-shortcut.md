# 斗法零模型快捷命令

## 使用

- `/doufa`（或 `/doufa start`）：手動啟動既有完整五發送帳號輪替。
- `/doufa status`：只讀 launchd 執行狀態，不開 Telegram session、不改配額。
- `/doufa help`：使用說明；其他參數拒絕，不接受任意命令、帳號、目標或路徑。

命令經 Hermes `ctx.register_command` 的直接處理器，不進 agent/模型迴圈。
普通文字「斗法」仍屬聊天，不是快捷命令。使用 `/doufa` 才保證命中零模型路由。

## 邊界與執行者

- 複用 `scripts/activate_doufa_demo_player_02.sh` → 既有
  `ai.hermes.xiuxian.doufa_demo_player_02.rotation` LaunchAgent。
- 不建立第二個排程、sender 或 campaign；既有 rotation 是唯一戰役主寫入者。
- 維持 `.斗法 demo_player_01`、五帳號、全局至少 300 秒、不得連續同發送者、
  每帳號每日最多 5 次官方確認結算、24 小時反覆交锋限制只暫停該帳號。
- 一次啟動只打啟動當天（Asia/Shanghai）的戰役：跨過北京零點即結束，
  不會在同一次啟動裡接着用次日的本地配額（伺服器是滾動 24 小時限制，
  零點後首發多半換來「交锋過多」）。次日要打須再 `/doufa` 一次。
- 某帳號的發送失敗不會中止整場，輪替繼續到其餘帳號。
- 已有 pid 時只回執行中，不清暫停、不重啟、不追加發送。
- 同時到達的啟動命令用 `state/doufa_shortcut.lock`（OS flock）互斥；
  這不是 rotation lock，也不改任何配額／回執檔。程序退出自動釋鎖。
- activation 的 `launchctl kickstart` 不使用 `-k`，避免與外部手動啟動競爭時殺死既有戰役。
- 啟動後讀回 launchd pid；這只證明戰役執行中，不等於遊戲結算成功。
- launchd 未載入／查詢失敗、超時或未確認啟動均明確返回錯誤，不盲目重試、
  不向聊天洩漏 subprocess 日誌，也不因 handler 異常落入模型路由。

## 擁有者與 profile

來源碼：`scripts/hermes_doufa_plugin/{__init__.py,plugin.yaml}`。
預設 profile 的 `plugins/xiuxian-duel-direct` 連到這個來源目錄，避免第二份实现。
只修改目前 profile，根目錄由 `get_hermes_home()` 取得。

設定使用 Hermes CLI（不要手改 YAML）：

```bash
hermes config set plugins.entries.xiuxian-duel-direct.settings.owner_user_id '<Telegram user ID>'
hermes plugins enable xiuxian-duel-direct
```

插件只接受該 user ID 在相同 chat ID 的 Telegram `dm`。
使用 gateway task-local ContextVars，不使用可能殘留他人身份的程序環境變數。
未設定擁有者、群組、別的使用者、其他平台或無 task-local 身份均拒絕。

插件啟用會經 gateway `reload-plugins` 控制 socket 熱載入並更新平台處理器；
讀回回應中的 `reloaded`、plugins、activations 和 `adapters_rewired` 後才認定啟用。
不重啟 gateway，不動其他 XIUXIAN 服務。

## 驗證

```bash
./.venv/bin/python3 -m unittest -v test_doufa_shortcut test_doufa_daily_quota
bash -n scripts/activate_doufa_demo_player_02.sh
```

離線測試以臨時目錄及 subprocess 邊界替身隔離，絕不真發遊戲命令。
上線驗證只在已確認既有 campaign 正在執行時測試 `/doufa` 重入，
對比前後 pid/runs/回執與官方已結算的帳號；不為測試另開 campaign。
