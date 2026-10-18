# @demo_player_06 元嬰閉關自動化移除（2026-09-15）

依使用者「完全删除 @demo_player_06 的元婴闭关脚本，不需要了」的要求，
已移除業務帳號 `tg9100000006` 的 `yuanyingchuqiao` 流程。
該流程此前使用憑證來源 `tg9100000007`，實際命令為 `.元婴闭关`。

已卸載並禁用四個 LaunchAgent：

- `ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.send`
- `ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.watchdog`
- `ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.return_observer`
- `ai.hermes.xiuxian.tg9100000006.yuanyingchuqiao.return_observer.watchdog`

原監聽 shell PID 5339 與 Python PID 5348 均已退出。
四個專用 shell wrapper、四份 repo plist、四份已安裝 plist，
以及專用 `yuanying_retreat.py` 均已刪除。其專用測試和過期 bytecode
亦已清理；新 Python 程序已無法 import 此模組。

主註冊表刪除一個 flow 及一個 observer watchdog helper；其他註冊項
經與操作前快照逐項比較，完全相同。舊帳號 bundle 的 pause/resume
清單也已移除該 flow。註冊表對已移除的帳號／流程及四個 label
設有拒絕恢復的檢查，避免舊設定將它重新啟用。

共用 observer、watchdog 和健康檢查中，僅為八小時元嬰閉關服務的
分支已刪除。其他流程需要的官方狀態辨認仍保留在元嬰狀態 probe，
以便遇到手動閉關時正確判定忙碌；它不會啟動本次已刪除的自動化。

專用狀態目錄 `state/accounts/tg9100000006/yuanyingchuqiao/`
的 20 個檔案已移至使用者垃圾桶，運行目錄已不存在。移動前確認
沒有程序開啟該目錄的檔案。精確恢復位置只記錄於本機
`state-removal.json`；帳號共用憑證及其他流程的 session 保留。
Git 歷史和既有離線遷移備份是歷史資料，不是啟用來源。

Hermes 的 8 個 cron、舊系統的 7 個 cron 及使用者 crontab 均已檢查，
沒有額外元嬰閉關排程。更新後的全隊恢復清單有 91 個 label，
不含上述四個；即時健康檢查的 active 和 legacy 分類也都沒有退役流程。

**驗證：606 項根目錄回歸測試全部通過，shell 語法及
`git diff --check` 通過。** 測試涵蓋刪除後的檔案／排程清單、
舊設定拒絕恢復、共用 observer、狀態 probe、健康檢查和帳號隔離。
其餘五個帳號的元嬰出竅流程、@demo_player_06 的深度閉關／裂縫等其他流程
均保留原註冊設定。本次沒有執行遊戲測試命令，也沒有發送 Telegram 訊息。

穩定偏好 `PREFERENCES.md` 已更新，兩份舊閉關說明已標記為歷史紀錄。
完整本機證據位於 `state/reports/2026-09-15-demo_player_06-retreat-removal/`：
`before.json`、`stop-actions.json`、`installed-plists-removed.json`、
`state-removal.json`、`tests-root-final.log`、`health-summary.json`、
`acceptance.json`。
