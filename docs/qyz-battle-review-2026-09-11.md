# 2026-09-11 青元子自動戰鬥復盤

今日六個帳號完成了自動發現入口，但未進入自動戰鬥。開戰帳號 0/6、攻擊回執 0、伺服器確認結算 0/6，沒有任何 16/16 perfect 的實戰證據。所有時間為 Asia/Shanghai。

## 場次與逐帳號結果

當日實際入口是 [12267212](https://t.me/example_game_group/7310786/12267212)，由 `sample_peer_06` 在 13:40:04 發出。先前提供的 `12248312` 是舊場次參考。

| 業務帳號 | 使用者名稱 | 記錄失敗時間 | 結果 |
| --- | --- | --- | --- |
| tg9100000001 | demo_player_01 | 13:40:30 | WebView 階段失敗 |
| tg9100000002 | demo_player_02 | 13:40:22 | WebView 階段失敗 |
| tg9100000003 | demo_player_03 | 13:40:22 | WebView 階段失敗 |
| tg9100000004 | demo_player_04 | 13:40:24 | WebView 階段失敗 |
| tg9100000005 | demo_player_05 | 13:40:22 | WebView 階段失敗 |
| tg9100000006 | demo_player_06 | 13:40:23 | WebView 階段失敗 |

六份 `world_boss_state.json` 的同場次紀錄均為 `runtime_error`、`official_webview_origin_invalid`、`phase=webview`、`finish_confirmed=false`，沒有任何 mutation、攻擊回執或已確認階段。該錯誤在建立戰鬥 runtime 前拋出，因此這次自動流程沒有送出開戰、攻擊或結算操作。`tg9100000006` 的憑證來源仍為 `tg9100000007`。

## 原因與修補

Telegram 的官方 WebView RPC 回傳 `https://asc.aiopenai.app/miniapp/xianxia-verify`；原程式只接受 `/miniapp/xianxia-world-boss`，因此六帳號在入場窗口內被本地路徑檢查擋住。

官方頁面的前置路由會把 `qyz_` 入口直接導向青元子戰鬥頁，原樣保留 Telegram fragment。匿名讀取的頁面在 14:03 與 19:53 的 SHA-256 均為 `cbca78cde5a1055299f02f70b61e0a0684b092e9e39a48b9111e3e7a185b306c`。它不能被解讀為這場需要額外的拖動驗證。

修補接納這條明確的官方入口路徑，繼續檢查簽署帳號 ID、username、`qyz_` token、所有外層入口參數、重複參數及有效期。原有官方 Turnstile 和 16 次動態攻擊流程保持不變，驗證資料只留在既有記憶體流程。

已核對 9 月 9 日發給 Hermes 的三份來源清單：`world_boss_browser.py`、`world_boss_turnstile.py`、`world_boss_features.py`。110 個保留函式的雜湊符合 `qyz_reference/SOURCE.json`；`NativeTurnstileBrowser.verify`、`AutomaticTurnstileWorker.run_once` 和 `WorldBossMonitor._begin_with_turnstile` 均保留原函式。適配差异記在 `ADAPTATIONS.patch`，包括 macOS Chrome 啟動、套件匯入及已記錄的戰鬥回執修正；生产 broker 使用記憶體保存一次性 token。今日流程沒有走到 CF，因此不存在今日成功過盾的實戰證據。

本次比對依據是已記錄的來源清單及函式指紋；定向搜尋未找到當時三個附件的原始下載檔，因此不能宣稱已重新從原附件獨立比對。21:51 再次執行來源一致性測試通過。

青元子 93 項離線測試通過，包含官方路由入口進入原有驗證、16 次攻擊與結算的合成場景，以及錯帳號、錯入口、衝突參數、過期入口、未完成 mutation 不重派等案例。相關運維 133 項離線測試也通過。這些結果證明程式修補通過回歸，不能證明今日已打過或下一場已成功。

## 生產核對與驗收界線

生產共用監視器持續監控主群話題 `7310786` 與 `-1009400000005`，入口發送者不作為過濾條件。六帳號在復盤時均為已連線、身分與群成員校驗通過、來源可讀、官方驗證服務可用。

確認六帳號均無進行中的戰鬥後，透過既有 launchd 服務正常結束舊程序並自動載入修補。20:06 回讀六帳號均使用 `qyz-reference-v10`、同一服務 PID 9621，兩群監控、連線、身分、群成員與驗證服務檢查通過；六份原始場次日誌的雜湊完全未變。20:08 的全域健康快照為 88 ok、0 warn、6 bad，六項均指向今日該場次的真實失敗。

60 秒入場窗口已過，不重跑 `12267212` 或 `12248312`。原始失敗紀錄保留，健康檢查仍會反映該失敗；新心跳不能抹掉它。下一場必須取得每帳號 16/16 perfect 及 `finish_confirmed=true` 的伺服器回執，才能宣告自動戰鬥實戰成功。

機器可讀證據與測試輸出位於 `state/reports/2026-09-11-qyz-webview/`：`checkpoint.json`、`readonly-webview-origin.json`、`official-verify-page-current.json`、`qyz-offline-tests.json`、`ops-offline-tests.json`、`deployment-before.json`、`deployment-after.json`。未保存完整 WebView URL、入口 token 或 initData。

## 9 月 12 日：連線恢復修補

00:15 的後續完整核對發現主程序 PID 9621 仍在，但六帳號均已斷線，來源檢查停留在前一晚。Surge 的本機 MTProto 入口可接受連線，實際 Telegram 記錄為 `PROTOCOL MTProto` 經美國節點正常連接。Telethon 耗盡自身重連次數後，原健康循環只呼叫 `get_me()` 和來源讀取，沒有再次建立連線，造成程序存活但無法監聽新入口。

`qyz-reference-v11` 在既有健康循環內使用共用、有限時及並發限制的連線門控恢復原帳號 client，再重新核對身份、群成員資格及兩個來源；同時消費 Telethon 終止 future 的例外。恢復循環沿用現有 handler 和程序。若該帳號持有戰鬥鎖，健康檢查只更新連線觀察，不重連或取消戰鬥。三份 CF 參考程式和戰鬥執行流程未改動，110 個保留函式仍通過來源指紋測試。

確認六帳號沒有排隊或進行中戰鬥、沒有未確認 mutation，且各自持有原租約後，正常結束斷線主程序，由原有 KeepAlive 服務載入修補。00:36 回讀只有一個新 host（PID 22526），六帳號均為 v11，身份、兩群成員及來源、驗證服務檢查全部通過，六個程序租約保持唯一。六份事件日誌的 SHA-256 與部署前一致，唯一業務阻擋仍為 `latest_battle_not_successful`。

青元子 96 項離線測試通過，新增覆蓋終止斷線後重試、重連後仍須驗證身份與來源、失敗戰鬥紀錄保留，以及進行中戰鬥不被重連或取消。沒有發送遊戲測試指令，沒有重跑過期場次，也沒有修改 Surge 全域設定。下一場的 CF 驗證、逐窗攻擊及結算仍須以自然場次的伺服器回執驗收。

本次恢復證據位於 `state/reports/2026-09-11-question-replies/`：`surge-readonly-recovery-check.json`、`qyz-reconnect-tests.json`、`qyz-reconnect-deployment-before.json`、`qyz-reconnect-deployment-verified.json`、`health-recovery-first.json` 及 `health-recovery-confirmed.json`。00:36:24 與 00:41:11 兩次全量快照均為 88 OK、0 WARN、6 BAD；六項均為保留的原場次 WebView 失敗，連線及來源檢查已恢復。
