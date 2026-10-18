# 健康檢查 Cron 崩潰與告警確認修復（2026-09-12）

後續已按使用者要求，將此 Cron 的一般結果與執行失敗通知都改為只存本機；
排程繼續運作。現行交付設定與問道超時修復見
[問道超時與健康檢查通知修復](wendao-health-noise-repair-2026-09-12.md)。

Hermes 的 `Xiuxian Active Healthcheck`（`7bf4e7f517b4`）連續八次失敗，
完整例外為 `ModuleNotFoundError: No module named 'telethon'`。Cron wrapper
使用 PATH 上的 `python3`；健康檢查核驗溫養雙修成功回執時，從發送器載入判讀
函式，連帶載入 `transport`／Telethon。只有讀到這種回執才觸發，因此單純啟動
或使用專案虛擬環境測試，未必能重現。

## 修補

- Cron wrapper 明確使用專案 `.venv/bin/python`。Hermes 原有薄入口仍轉入
  同一專案腳本，維持 `no_agent`、每 30 分鐘執行及既有交付設定。
- `shuangxiu_wenyang_reply.py` 集中純文字判讀及帳號規則。發送器與健康檢查
  共用它，健康檢查不再載入 Telegram 連線程式；帳號、回覆對象及可信回執
  發送者仍完整核驗。
- 保留間隔一秒的兩份檢查快照。只有兩份都出現、且尚未通知的相同 BAD
  指紋才通知。持續故障不重複推送；一次短暫恢復不清除通知記錄，兩次均恢復
  才讓日後同一故障重新通知。WARN 仍保留在快照，沒有 BAD 時不通知。
- 第一份快照是執行期間的臨時檔，由 wrapper 在退出時清理。第二份為完整的
  正式健康快照；沒有把失敗改成健康，亦沒有停用排程。

這是既有 healthcheck 的快速檢查路徑修復。工作區
`state/xiuxian-healthcheck.json` 仍由 collector 寫入，
`state/xiuxian-healthcheck-notification.json` 仍由 notifier 寫入。
沒有新增 Cron、LaunchAgent 或業務狀態寫入者。環境變數
`XIU_HEALTHCHECK_OUTPUT`、`XIU_HEALTHCHECK_NOTIFICATION_STATE` 可把離線驗證
導向獨立檔案，預設正式路徑保持不變。

## 驗證

- 回執、雙修、告警、健康檢查及 QYZ 健康判定共 **129 項測試通過**。
  涵蓋禁用所有 site packages、錯誤帳號回執、最小 Cron PATH、兩次確認、
  暫態 BAD、短暫恢復、持續失敗去重及真正恢復後再告警。
- 系統 `python3 -S` 對正式狀態完成 94 項檢查並以 exit 0 結束，證明 collector
  不再依賴 Telethon 或其他第三方套件。
- 02:18 經 `~/.hermes/scripts/xiuxian_active_healthcheck.sh` 正式入口，使用
  `PATH=/usr/bin:/bin` 連續執行兩輪，均為 exit 0，stdout／stderr 均為空。
- **02:32:52 自然 Cron 驗證成功**：`last_status=ok`、`failure_streak=0`、
  `last_error=null`、`last_delivery_error=null`，排程輸出為
  `silent (empty output)`。這次已使用最終的兩份快照確認邏輯；排程仍啟用，
  下次執行時間為 03:02:52。
- 當時完整快照為 **88 OK / 0 WARN / 6 BAD**。六項均是青元子 9 月 11 日
  舊場次 `12267212` 的未成功紀錄，沒有以新心跳或程式修補當成戰鬥成功。
  相同故障保留於正式快照及去重狀態。02:32:52 的自然排程仍得到相同數量。

兩處 `ops-alignment-check.sh` 均不存在；已核對實際 Cron 回執及啟用註冊項，
沒有聲稱執行不存在的檢查脚本。

執行證據位於 `state/reports/2026-09-12-healthcheck-repair/`。沒有人工發送
Telegram 測試訊息，亦沒有手動改寫 Cron 的成功／失敗計數。
