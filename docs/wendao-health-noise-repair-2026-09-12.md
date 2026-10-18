# 問道超時與健康檢查通知修復（2026-09-12）

08:33 的健康告警包含 `tg9100000006/wendao` 超過 180 秒執行上限。
本輪修正發送器的時間預算、結算回執及健康檢查的冷卻時間歸屬，並按使用者
要求，將 `Xiuxian Active Healthcheck` 的一般結果與執行失敗通知都只存本機。

## 通知與排程

已透過 Hermes CLI 更新原有工作 `7bf4e7f517b4`：

```sh
hermes cron edit 7bf4e7f517b4 --deliver local --failure-deliver local
```

工作維持啟用、`no_agent=true`、每 30 分鐘執行
`xiuxian_active_healthcheck.sh`。讀回正式設定後，呼叫 Hermes 實際的
`cron.scheduler_delivery._resolve_delivery_targets()`，一般與失敗路徑均回傳
空目標清單；相同入口只有這一個 Cron。兩次確認與去重邏輯仍保留，但結果
不再自動送往 Telegram。工作區 `PREFERENCES.md` 已記錄這項持續偏好。

這是既有健康檢查與發送器的修復，沒有新增排程或狀態寫入者。Cron 設定仍由
Hermes 管理；發送器寫入本帳號的業務狀態；collector 與 notifier 分別寫入
工作區 `state/xiuxian-healthcheck.json` 及
`state/xiuxian-healthcheck-notification.json`。完整 BAD/WARN 證據繼續保存。

## 原因與修補

- 發送器曾等待 154 秒冷卻，沒有替發送後的完整回執窗口留時間，且未計入
  Telegram 連線耗時。現在從連線前開始計時，先保留初始等待、完整輪詢
  窗口、一個輪詢間隔及安全餘量，再決定能否原地等待。以 5 秒準備時間的
  回放為例，可等待時間為 127 秒；154 秒會寫入既有待重試狀態交由後續排程
  處理。短冷卻仍可原地等待，180 秒上限保持不變。
- 問道先回覆「你消耗了 1000 點修為…」，再編輯為 `【问道得宝】`。
  現在必須取得成功或冷卻回執；初步訊息不算結算。新增簡繁體得寶標記，
  保留舊成功與重置標記，並核驗本帳號指令的直接回覆。遲到的成功回執可
  恢復成功狀態，避免再次發送。
- 08:38 的真實回覆顯示尚餘 `11小时54分钟30秒` 冷卻；問道的成功去重
  間隔改為 43200 秒，提前探測設為 0。
- 同一 `cooldown_retry.json` 留有舊群組記錄，健康檢查曾直接取第一筆，
  把今日問道冷卻誤讀成 8 月的期限。現在按 `last_attempt` 的
  `target|topic_id|message` 選取，並核驗該筆內容相符；其他群組或指令不能
  提供本輪冷卻時間。

## 驗證

- **164 項相關離線測試通過**，其中本輪新增 12 項。覆蓋輪換 Bot、錯誤帳號、
  初步訊息編輯成結算、遲到回執恢復、時間預算及跨群組冷卻資料。
- 原始生產日誌的指令 `12283620`、回覆 `12283621` 直接回放：08:32:54
  初步訊息未確認；08:33:00 編輯後的得寶訊息被辨認為成功。完整發送流程的
  隔離回放在第 23 秒結算，僅發送一次。
- **10:03:57 自然 Cron 執行成功**，`last_status=ok`、`failure_streak=0`，
  執行及交付錯誤均為空，輸出為 `silent (empty output)`。
  **11:04:06 再次自然執行成功**，失敗次數仍為 0；重新核驗一般及失敗通知
  均無外送目標。當次快照保留 86 OK / 2 WARN / 6 BAD，問道兩項仍為 OK。
- 自然啟動的問道發送器 exit 0；正式冷卻記錄中的可等待預算為
  `125.979` 秒，確認新邏輯已載入，沿用原有 300 秒 LaunchAgent 間隔。
- **10:16:31 正式健康檢查入口**在 `PATH=/usr/bin:/bin` 下 exit 0，
  stdout/stderr 均為空。問道發送器與 watchdog 均為 OK，冷卻期限正確為
  **2026-09-12 20:32:58（北京時間）**。
- 完整快照為 **88 OK / 0 WARN / 6 BAD**。六項是青元子舊場次 `12267212`
  的未成功紀錄，仍保留真實狀態；新場次的入場驗收見
  [青元子 CF 與入場實測](qyz-cf-admission-verification-2026-09-12.md)。
- 較廣的 `test_refactor_units.py` 無法匯入 `battle_page_state`；以本輪修改前
  `9f75305` 的隔離 Git 快照重跑亦有相同錯誤。這是既有測試失配，未宣稱
  全專案測試通過，亦未為本輪修復加入相容性替身。

證據位於 `state/reports/2026-09-12-wendao-health-noise/`：
`regression-tests.log`、`baseline-refactor-test.log`、
`cron-routing-validation.json`、`natural-runtime-check.json`、
`final-health-verification.json`、`final-cron-audit.json` 及
`original-receipt-replay.json`。
診斷未人工發送 Telegram 訊息或修改生產戰鬥紀錄。
專案及工作區均沒有 `scripts/ops-alignment-check.sh`；已直接核驗正式 Cron、
LaunchAgent 與健康檢查結果。
