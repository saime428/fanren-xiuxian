# 青元子 16/16 PERFECT 優化：v13

本次修補針對 [12287199](https://t.me/example_game_group/7310786/12287199)
實戰暴露的連線重建、伺服器蓄力不足及同帳號手動／自動衝突。
原場次仍為 96 窗、83 次確認命中、69 次 server perfect；**沒有帳號達成
16/16 perfect**。本次離線測試與公開頁測速不會改寫該成績。

## 已確認的傳輸問題

11 次伺服器蓄力不足的慢 `/charge-start`，都發生在前一擊完成超過 5 秒後。
原本共用的 httpx HTTP/1.1 連線池，預設 `keepalive_expiry=5`，而 `/window`
輪詢也會借用戰鬥連線。

17:06 的公開頁 GET 對照，每次閒置 6.2 秒：

| 連線保留 | 第一次閒置後 | 第二次閒置後 | TCP/TLS trace |
| --- | ---: | ---: | --- |
| 預設 5 秒 | 1001 ms | 1166 ms | 每次重建；TLS 分別約 486、655 ms |
| 60 秒 | 401 ms | 398 ms | 兩次均重用連線 |

新 `HttpTransport` 的獨立 GET 核對同樣為冷連線 1211 ms、閒置後重用
400 ms。請求均為未登入的公開頁 GET，沒有呼叫遊戲 API。這證明現有路由
下重建連線的成本；並不能證明歷史上每一次延遲都只來自 TLS。
Surge 請求紀錄顯示該網域走 `ai.conf` 的美國節點，未更改路由或全域設定。

## 程式行為

- 每帳號的 HTTP 上限仍是六條連線，拆為四條行動連線、兩條讀取連線，
  保留 60 秒。`start`、`begin` 與蓄力／出手沿用行動池，窗口輪詢獨立。
  每窗仍可並行排程，前一擊慢回覆不會強制阻塞下一窗。
- 保留約 1000 ms 的既有蓄力目標。票據慢回來時，以回覆時間和已知傳輸
  提前量估算票據年齡；只在 PERFECT 到達期限與本機蓄力上限都容許時，
  延後出手補足蓄力。無法兼顧時保留原命中目標，真實 `holdMs` 不補值。
  刻意延後的部分不會當成網路漂移回饋到後續窗口。
- 上層參考程式的 mutation 重試也設為零，避免 HTTP 之外的重試迴圈重送
  結果不明的 `hit`／`finish`。既有原生 CF 流程仍各次取得新驗證材料；
  `_begin_with_turnstile`、原生瀏覽器驗證與 worker 的函式本文未變。
- 收到 `boss_action_limit` 時先鎖住該帳號的後續行動，再做一次有時限的
  `state` 核對。其他帳號繼續。該帳號不再送出 finish，以免替手動頁面的
  戰鬥提早結算；已送出的在途請求不能撤回。事件保留為待核對，並保存已
  確認命中及讀取結果。讀取失敗也不會重開行動閘門。
- 保留 `boss_hit_outside_window` 的具體原因；`wake_lateness_ms` 改為
  實際出手時間減排程時間。逐請求 TCP／TLS 耗時只記錄固定欄位，沒有
  headers、URL、驗證資料或 httpcore 例外本文。
- 健康檢查單獨給 QYZ 的完整 20 場事件日誌 8 MiB 讀取上限，避免逐窗
  證據累積超過通用 1 MiB 後被誤報成狀態損壞。其他任務上限不變，超限
  與真實未完成事件仍報 BAD。Cron `7bf4e7f517b4` 正常／失敗輸出均為 local。

`qyz_reference/ADAPTATIONS.patch` 可反向重建三份附件的原始 SHA-256。
來源清單只更新已列為適配的戰鬥方法；110 個保留函式指紋仍核對通過。

## 驗證與實戰界線

青元子完整 **111 項**測試通過；相關健康、Cron、效率與註冊表 **119 項**
通過。新增案例先重現舊失敗，再驗證真實 loopback 連線重用、輪詢隔離、
慢票據、窗口外拒絕、不可恢復的時序、同帳號衝突隔離、零 mutation 重播
及蓄力延後不污染漂移估算。測試的伺服器判斷使用自己的到達時間及票據
年齡，不能從本機宣稱的 `holdMs` 推導 perfect。

額外 LaunchAgent 清單測試仍有一項既有失配：15 個已載入的舊標籤缺少
repository plist 快照。清單、註冊表與對應測試未因本次修改而變更；所有
啟用項目的 plist／共用映射檢查通過。完整測試輸出保留在本機報告目錄。

17:44–17:46 已透過原有 LaunchAgent 正常載入 v13，唯一主程序 PID
**49825**。六帳號的連線、身分、群成員、來源及驗證服務均已重新核對，
六個帳號租約仍唯一；啟動後沒有 traceback、型別或匯入錯誤。六份事件日誌
的 SHA-256 與部署前相同。

新版本部署與自然場次的實際驗收，記錄於
`state/reports/2026-09-12-qyz-perfect-optimization/checkpoint.json`。
尚須下一個新鮮自然入口的逐帳號 16/16 server perfect 與 finish 回執，
才算完成目標。手動與自動請勿同時操作同帳號同場次。

證據：同目錄 `connection-probes.json`、`implemented-transport-probe.json`、
`official-miniapp.html`、`qyz-tests.log`、`health-focused-tests.log`、
`health-tests.log`。公開頁 SHA-256：
`3d1c01e987edfe45ea67e696435ad53bf60290fca91e61016953dfd2f2091d37`。
