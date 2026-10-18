# 指令回執、持續重試與恢復（2026-09-13）

指令送出不代表遊戲生效。使用者指定的 `12300733` 是 @demo_player_01 的
`.神迹 布道`；舊 `last_success.json` 只有 `sent`，後續七分鐘的唯讀查詢
亦沒有對應業務回執。本輪移除這種成功判定，並按使用者要求接續未確認指令。

## 共用契約

- 官方回覆必須對應帳號、指令、主群／topic、原命令或唯一事件。其他玩家、
  轉發、任意 Bot 文字、HTTP 200、Telegram message ID、心跳都不能證明成功。
- `last_attempt.json` 記錄嘗試，`last_result.json` 記錄已確認業務結果及下次期限；
  只有明確成功才寫 `last_success.json`。冷卻、拒絕與不利結局不冒充成功。
- 沒有相關回覆就持久保留 pending，按允許間隔持續重發，沒有總次數上限。
  重啟後先對帳再接續；原副本遲到成功可以結束整條重試鏈。
- 確認窗口從實際通過發送 gate／取得送達 ID 起算；在 gate 排隊不消耗新指令的
  回覆等待時間。成功記錄保存實際被回覆的副本 ID，而非最後一次重發的 ID。
- 尊重暫停、限流、官方冷卻、明確拒絕與原事件期限。過期題目不重發；
  精確關聯的遲到回執仍可對帳，不能因過了一天而丟棄。
- 所有主動指令只送 `-1009400000004` / topic `7310786`；
  `-1009400000005` 為唯讀來源。帳號憑證來源別名不改變業務帳號。

## 活躍流程的回執矩陣

| 流程 | 生效證據 | 無回覆的處理 |
| --- | --- | --- |
| 撫摸法寶、問道、探尋裂縫、血洗、召喚魔影、元嬰出竅 | 官方直接回覆原命令，並符合各流程業務文字 | `command_pending.json` 保存全部副本，至少約 120 秒後接續；具體確認窗口可能更長 |
| 深度閉關、閉關／元嬰狀態查詢 | 官方直接回覆 action 或 query；只有明確開始才是 action 成功 | action 與 probe 各有持久 journal；無狀態回覆不推定空閒或已開始 |
| 玄骨考校／駕校與窺鼎 | 相同帳號、題目分支與選項的官方結果 | 30 秒重試；最長 300 秒，題目被縮短不延長；錯答／逾時另存 |
| 極陰祖師 | 官方確認所選 `.献上魂魄` 或 `.收敛气息` 的結果 | 60 秒重試；原題最多 180 分鐘；公告不能當成可作答題目 |
| 南隴侯交易 | 帳號、侍妾、功法／法寶選擇及交易結果一致 | 60 秒重試；最多 600 秒；只有 @demo_player_01 選功法，其他五帳號選法寶 |
| 神機布道 | `.神迹 布道` 的官方業務成功、冷卻或拒絕 | 60 秒重試；公告發現窗口不是已送出指令的失效時間；公告刪除後仍接續同一指令 |
| 溫養雙修 | 正確兩個帳號、anchor、命令及官方結算 | 核對所有副本後，每 180 秒重試同一 anchor；一小時週期由官方結算／冷卻建立 |
| 斗法 | 正確出手帳號、`.斗法 demo_player_01` 及當日官方結算 | 無回覆不計配額；保留全域至少五分鐘輪轉、不得連續同帳號、每帳號每日五次；反重複只停該帳號 |
| 垂釣 | 同帳號、同 challenge 的 `result.ready` | 先查結果再重試同一竿；新認證 scope 不能只憑沒有 challenge ID 的 `ready=true` 結算舊竿 |
| 天機 | 同帳號、identity、日期的官方進度及結算 | pending finish 落盤；重啟先查進度再重試同一 challenge；缺欄位不推定 `(0,3)` 或完成 |
| 青元子 | 每個 hit 的 accepted/perfect 及明確 finish 結果 | 同認證 scope 內重試相同請求；charge/hit 受各自時間窗口限制；未知結算保留待核對 |

問答監聽的 push 與近期歷史會比對所有副本。舊回覆精確查詢每輪最多兩個命令
ID，輪流處理並保存進度；舊 thread 查詢超時仍保留 pending，不會拖垮来源
健康掃描或永久阻止下次重試。發送鎖每次取得時更新持鎖 PID 與開始時間。
每五分鐘最多選一個未確認事件，按帳號及該事件的時間範圍查兩個來源群，
恢復已離開近期訊息頁的官方廣播；歸屬與唯一候選限制照常套用。副群
`1105013` 是 @demo_player_03 對 `12302541` 主動收斂成功的實際回執，已納入回歸。

普通指令、閉關 action/probe、元嬰 probe 及雙修也限制每輪最多兩個舊副本的
網路查詢，持久保存輪巡位置；近期回覆仍比對全部副本。舊 thread 最多等待
10 秒，不能因累积數百個未確認副本而耗盡下一次重試窗口。100 副本的離線
回歸已驗證能接續發送，並由任一舊副本的相關回覆結束整鏈。

神機正式自動重試 `12304866` 的官方成功回覆為 `12304868`：
「【神音浩荡】……信仰提升至 100，稳定提升至 100」。已由正式監聽器對帳並
停止該鏈重試。此前「凡间方才承受神谕，需再等待 20分钟15秒」等冷卻文字
已補入分類器；較舊冷卻不能縮短已知的服務端等待期限。

唯讀診斷確認，未節流的 1,000 條 GetHistory 查詢會收到 4～8 秒 `FloodWait`。
Telethon 在 `request_retries=0` 搭配預設內部 sleep 時會把此錯誤折疊為
`ValueError`。共用 client 現設 `flood_sleep_threshold=0`、
`raise_last_call_error=true`，由外層按真實秒數退避；服務端限流／暫時錯誤不觸發
重連。歷史頁間隔一秒、同帳號的兩來源依次掃描，仍保留 1,000 條恢復範圍。
新唯讀查詢已完成兩群各 1,000 條；正式監聽亦可在短限流後恢復完整來源掃描。

## 狀態所有權與排程

| 狀態 | 主要寫入者／互斥 |
| --- | --- |
| 各帳號／流程的 `command_pending.json`、`last_attempt.json`、`last_result.json`、`last_success.json`、`cooldown_retry.json` | 該流程 sender，持有既有 `send.lock` |
| 深度閉關 `status_probe_pending.json`、`status_probe.json` | `send_shendubiguan.py`，同一 `send.lock` |
| 元嬰 `status_probe_pending.json`、`status_probe.json` | `probe_yuanyingchuqiao_status.py`，`status_probe.lock` |
| 問答 `question_state.json`、`monitor_health.json`、學習題庫及嘗試／成功記錄 | 該帳號 `QuestionReplyMonitor`，`question_monitor.lock`；發送另取 `send.lock` |
| 返回觀察器 `observer_health.json` 及事件游標 | 既有 `observe_return.py`，不寫 sender journal |
| 雙修狀態及 anchor | 既有 `shuangxiu_wenyang.py` coordinator，在其流程鎖內 |
| 垂釣 `fishing/state/miniapp_fishing_locks/commands/<account>/pending.json` | 該帳號垂釣 runtime，在原帳號鎖內 |
| 天機 pending、identity 結果及 coordinator 摘要 | 天機 runtime／coordinator 的各自狀態；摘要不能替代 identity 回執 |
| 青元子 `world_boss_state.json` | 該帳號 host，在 `protocol.lock` 生命週期內 |
| 工作區 `state/xiuxian-healthcheck.json` | `scripts/xiuxian_healthcheck.py` |
| 工作區 `state/xiuxian-healthcheck-notification.json` | `scripts/xiuxian_health_alert.py` |
| 工作區 `state/xiuxian-staging/healthcheck.json` | 既有 staging 維護 label 呼叫共用 `hermes_shadow_check.py` |

這是既有 live automation、watchdog 與 maintenance 的修復。健康檢查及恢復
沿用共用 registry 解析與 LaunchAgent label 映射；青元子六帳號共用一個 label。
已核對並補登原本在運行的四個元嬰 observer watchdog、垂釣 sender 與每日 reset。
每 30 秒的到期 dispatcher 只讀 action／probe journal，非強制啟動已註冊 sender。
watchdog 不殺發送器、不刪 cooldown、不用 `kickstart -k`。
返回 observer 直接執行的十二個 sender wrapper 已核對執行權限；修正了
`send_shendubiguan_tg9100000006.sh` 的缺失執行位元。健康檢查會使用較新的、
帳號及 query 相符的官方倒計時解釋正在進行的元嬰任務，並獨立核驗事件 watchdog
的鎖檢查；這些證據不能清除另一條未確認指令。

`TG_RECONCILE_ONLY=1` 讓普通 sender 在既有鎖及帳號核驗下唯讀查原命令和回覆，
可補齊舊結果的歸屬 metadata；找不到官方證據便保留原狀，禁止新發送。
不是人工修改成功欄位。一般執行不設定此變數。

Hermes Cron `7bf4e7f517b4` 保持啟用，`deliver=local`、`failure_deliver=local`、
`no_agent=true`。WARN/BAD 保存在完整本機快照；未確認但按期重試為 WARN，
停擺或不可核對的業務失敗為 BAD。

## 已知邊界與驗證依據

青元子本輪版本為 `qyz-reference-v14`。官方網頁的 `/state` 用於房間狀態，
沒有足以恢復已中斷 charge/hit/finish 的已驗證結算契約。CF、charge ticket、
session token 不落盤；中途斷電後不能盲目重跑整場，仍保留
`unknown_mutation_outcome`。同 scope 重試及保留未知結果已實作，完整跨程序
戰鬥恢復仍未完成。

極陰逾時後「因长时间未做抉择……侥幸逃脱」不算主動選擇成功。
未知業務文字仍採保守判讀，合成離線測試不等於新的實戰回執。
青元子本日 `12303616` 六帳號各有 16 次 accepted hit 與 finish；一帳號
16/16 perfect、四帳號 15/16、一帳號 14/16。五筆 partial 保留為真實未達標，
不因部署或新心跳改綠。

最後離線驗證：主項目 584 項、青元子 112 項、垂釣 36 項、天機 17 項通過。
生產 inventory 的 160 個 Python、124 個 shell、97 個 plist 靜態檢查通過，
Git 沒有追蹤 runtime state／session／憑證檔。31 個問答與返回監聽程序在各自
空閒鎖邊界交接，新 PID 已取得完整來源掃描；青元子先前在六帳號結算完成後
交接為 `qyz-reference-v14`。排程驗收為 100 個健康項目、95 個唯一 label、
95 個已載入 label，無額外／缺失任務。

16:22:21 與 16:24:56（北京時間）的兩次新鮮健康快照均為
**94 OK / 0 WARN / 6 BAD**。六項 BAD 是上述五筆青元子 partial，以及
@demo_player_06 的舊極陰事件 `12117255`／命令 `12117259` 缺少可驗證回覆；該題已過期，
保留對帳而不重發。驗收中發現的 wrapper `PermissionError` 已修正，正式
observer 在 16:20:25 成功接續並重新取得兩來源的完整健康證據。

原始訊息、唯讀對帳、測試、部署前後快照及退休排程備份清單位於本機
`state/reports/2026-09-13-command-receipt-audit/`。退休程式測試備份位於
`archive/retired-tests-20260913/`，八個已無活躍入口的舊 LaunchAgent 位於
`archive/retired-launchagents-20260913/`。這些原始資料與 runtime state 不納入 Git。
