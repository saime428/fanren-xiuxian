# XIUXIAN 監聽連線與運維告警修復（2026-09-11）

Hermes 排程 `7bf4e7f517b4` 的凌晨告警包含真正的 Telegram
`ConnectionResetError`／`IncompleteReadError`。監聽程序仍在執行，並不足以證明
推送與歷史補查已恢復；停用閒置日誌亦令舊 traceback 缺少新的成功證據。
本輪修復現有 20 個事件回覆監聽及 12 個返回觀察器的恢復與健康判定。

## 行為與所有權

- 現有監聽程序以 `get_me` RPC 核實業務帳號，監察 Telethon 的終止連線
  future，失敗後有界重試。只有完成兩個來源群組的補查，且推送連線仍然可用，
  才記錄恢復；暫停、鎖忙、部分讀取、取消或新心跳均不能清除未恢復錯誤。
- 常駐程序使用自己既有的 push client 執行補查，減少反覆建連。
  返回觀察器先核對 sender、帳號、事件標記、話題、自己發出的狀態探測及
  事件時效，再喚醒補查；定時補查仍會執行。
- MTProto 建連預設最多四個並行握手，可由
  `TG_TELEGRAM_MAX_CONCURRENT_CONNECTS` 設為 1–8。鎖等待可取消，並計入
  建連時限；取消或建連失敗會釋放鎖，關閉連線亦會消費獨立的終止 future。
- 健康檢查驗證程序 PID、業務帳號、runtime、兩個來源、最近成功掃描與連線
  核實時間。32 個已部署註冊項啟用 `monitor_health_required: true`，缺少證據
  會報錯；同一註冊項的普通發送器和 watchdog 維持各自判定。
- 舊錯誤只會被較新的已驗證成功覆蓋，新錯誤仍然報錯。例外訊息優先顯示
  例外類型，避免告警只剩 `Traceback (most recent call last):`。

這是既有 live automation 的恢復修復：Telegram 推送為即時路徑，歷史補查為
恢復路徑；launchd 管理既有工作程序，Hermes cron 執行健康與通知策略。
沒有新增排程或第二套監聽程序。

| 狀態 | 唯一主要寫入者 |
| --- | --- |
| `state/accounts/<account>/<flow>/monitor_health.json` | 該事件回覆監聽程序 |
| `state/accounts/<account>/<flow>/observer_health.json` | 該返回觀察器程序 |
| 工作區 `state/xiuxian-healthcheck.json` | `scripts/xiuxian_healthcheck.py` |
| 工作區 `state/xiuxian-healthcheck-notification.json` | `scripts/xiuxian_health_alert.py` |
| `state/reports/2026-09-11-ops-alerts/` | 本輪一次性運維核對 |

共用連線閘門及其三個 slot 檔案是跨程序的 `flock` 協調鎖，刻意由多個程序
共用，並非業務結果狀態。原有業務回執、游標、session 與帳號歸屬保持原寫入者；
`tg9100000006`／`@demo_player_06` 的憑證來源仍為 `tg9100000007`。
來源仍包括 `@example_game_group` 與 `-1009400000005`，發送目標仍受主群話題
`7310786` 限制。

## 部署與核對

所有時間為 Asia/Shanghai。最終部署紀錄以 `fleet-scale-reload.json` 為準。

- 12:01:01：32/32 原有監聽程序完成替換與成功掃描核對。每次只在取得該程序
  實際業務鎖後替換閒置 worker，保留既有 LaunchAgent。
- 12:11:07：32/32 PID、父程序、程式雜湊及載入時間符合最終部署，且每個程序
  的成功掃描均已超過部署時的第一輪；12:29:53 再次核對仍為 32/32 正常。
- 12:17:13：94 個註冊檢查項對應 89 個去重標籤，其中 88 個 LaunchAgent 已載入。
  剩餘 `recipe_notice` 由既有 Hermes 程序執行且健康，保留這個已記錄的跨 runtime
  所有權，沒有另行載入其 LaunchAgent。兩處 `ops-alignment-check.sh` 均不存在。
- 12:24:51：正式 `bash scripts/xiuxian_active_healthcheck.sh` 兩次核對後為
  **93 ok / 1 warn / 0 bad**，exit 0、輸出為空，通知指紋清單已清空。
  保留的 warn 是 `tg9100000001/shuangxiu_wenyang` 的
  `sent_unconfirmed_assume_consumed`，不是已確認成功回執。
- 12:29:49：部署後的自然 cron 再次執行成功，狀態 `silent (empty output)`；
  對應快照仍為 **93 ok / 1 warn / 0 bad**，32 個監聽正常，通知指紋清單為空。

複製 session 的唯讀連線驗證亦確認告警中的 `tg9100000005/jiaohuan_fabao`
與 `tg9100000005/jiyin_choice` 可完成授權、身分、兩群歷史讀取及重連。
沒有 Telegram 測試發言、人工 Hermes 通知、修改全域 Surge 路由，或將真實
發送器當作測試執行。

## 測試與範圍

以原提交 `410d90aed6c5606e48b981ecf8d8a5ccf5b4770c` 加上本輪檔案建立隔離
候選目錄，排除同時進行的其他修改。23 個相關模組共執行 194 項測試：
192 通過；以下兩項在原提交單獨重跑亦同樣失敗：

- `test_loaded_live_launchagents_have_repo_plist_snapshot_when_available`：
  15 個既有載入標籤不在該測試掃描的 plist 清單；啟用註冊表的清單核對通過。
- `test_each_account_has_exact_attributed_command`：舊斷言要求 `TG_LOG_IDLE='0'`，
  原提交的共用 wrapper 已設定為 `1`。

候選目錄 140 個 Python 檔案語法解析通過，排除 `references/`、`archive/` 與
運行狀態。回歸涵蓋斷線恢復、取消與鎖釋放、四槽並行、帳號核實、完整來源
掃描、過期證據、錯誤保留、推送篩選及註冊表缺失證據。

工作區另有未提交的 `ExpectedFlowExit`／預期 `SystemExit: 75` 告警處理及
抚摸法寶回執測試，屬同時進行的其他修改，這次保留並排除於候選提交。
上述正式整體 0 bad 是工作區合併狀態；只使用本輪候選健康檢查的 12:22:39
唯讀核對中，32 個監聽仍全數正常，但抚摸法寶的舊 `SystemExit: 75` 另報
1 bad。本輪沒有把那項獨立修復算作自己的成果。

監聽恢復證據不等於遊戲伺服器已確認業務成功；本輪沒有重播過期青元子入口、
取得新場次戰鬥回執，或修復獨立的發送回執／舊失敗重試選擇問題。
完整原始證據保存在本機 `state/reports/2026-09-11-ops-alerts/`，不納入 Git。

13:23 的後續回執修復已整合預期退出碼處理，並由獨立候選版本重現整體
0 bad；詳見 [發送回執修復紀錄](sender-reply-repair-2026-09-11.md)。
