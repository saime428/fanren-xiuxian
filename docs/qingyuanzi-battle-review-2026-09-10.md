# 2026-09-10 青元子戰鬥復盤與 Mini App 適配

9 月 10 日六帳號的自動化全部漏場。直接原因是主控把開戰公告發件人固定成 `hantianzun16_bot`，當日正式公告卻由 `hantianzun23_bot` 發出。新版按入口辨識，接受任何發件人，監控使用者指定的兩個群組；同時修正重啟後未結算記錄被心跳覆蓋，以及漏招後仍提交滿血戰果。`qyz-reference-v9` 已於 9 月 11 日 00:22 驗證部署，復盤日期仍為 9 月 10 日。

## 今日實際戰況

統計覆蓋 **2026-09-10 全日（Asia/Shanghai）**。00:00:03–23:59:59 的生產日誌共 30960 行，其中 **8201 次公開頁面 GET，遊戲 POST 為 0**。9 月 11 日 00:21:52 重載前，原主控 PID 68795 的六份 `world_boss_state.json` 仍全部沒有 `world_boss_events`。以下數字只代表本機自動化，不把公開榜單推測當成手動參戰記錄。

| 帳號 | Telegram 使用者 | 自動入場 | begin 確認 | hit 確認 | perfect 確認 | finish 確認 |
|---|---|---:|---:|---:|---:|---:|
| tg9100000001 | @demo_player_01 | 0 | 0 | 0 | 0 | 0 |
| tg9100000002 | @demo_player_02 | 0 | 0 | 0 | 0 | 0 |
| tg9100000003 | @demo_player_03 | 0 | 0 | 0 | 0 | 0 |
| tg9100000004 | @demo_player_04 | 0 | 0 | 0 | 0 | 0 |
| tg9100000005 | @demo_player_05 | 0 | 0 | 0 | 0 | 0 |
| tg9100000006 | @demo_player_06 | 0 | 0 | 0 | 0 | 0 |

官方群 `-1009400000004`、話題 `7310786` 的當日回執：

- **13:40:09**，消息 [12248312](https://t.me/example_game_group/7310786/12248312)：`【世界通告｜真仙试锋开启】`，`hantianzun23_bot` / ID `8980154525`。按鈕為 `进入真仙战场`，指向同一 Bot 的 `qyz_` Mini App 入口。公告說明入場 60 秒、至少 10 人、一次完整參戰、單次約 300 秒、總時限約 7 分鐘。
- **13:47:10**，消息 [12248425](https://t.me/c/9400000004/12248425)：`sample_peer_06` / ID `8735907987` 結算 **26 人參战，天道敗退**。
- 公告只列前 10 名，六帳號均未出現；這不足以判斷是否曾手動參戰。上述自動化零參戰結論來自本機事件日誌和請求記錄。

修復前六帳號均為 `blocked_source_unavailable`，Telegram 連線、帳號身份、群成員及原生瀏覽器驗證器均可用。原固定參考消息 `12229459` 已不存在。這次漏場發生在入口識別階段，沒有證據把它歸因於 CF 驗證、命中時機或遊戲伺服器拒絕。

## 來源修復與驗證

依使用者最新要求，發件人不設 Bot 名單，也不要求發件人與入口的 Bot 相同。消息標題及按鈕文字可以變更；解析按鈕、隱藏文字連結及正文內的 Telegram HTTPS `qyz_` Mini App 深連結。`12248312` 只是當日格式參考，不是固定入口。

| 監控群組 | 範圍 |
|---|---|
| `-1009400000004` / `@example_game_group` | 話題 `7310786` |
| `-1009400000005` | 全群，包括其他話題 |

即時新消息、消息編輯及補查共用同一解析器。`latest_source_message()` 在每個群組讀取最近 100 條消息，同時讀取最近 32 條 `世界通告` 搜尋結果，再選出帶入口的最新消息；所有查詢均無 `from_user` 篩選。轉發亦可作為入口，但新鮮度採用原消息與轉發消息日期中較早者，不能用轉發延長 60 秒入場期限。

執行前在帳號鎖內重讀消息，核對來源群組、入口 fingerprint 和目標 Bot username。建立 WebView 時要求目標解析為真實 `bot=True`，再驗證官方 HTTPS origin、頁面路徑和帳號綁定的簽名 initData。發件人身份只讀取已快取資料作記錄，不額外發起查詢，也不作入場限制；離線完整戰鬥測試包含發件人資料不可用的情況。

23:16:50 的只讀探測確認兩個群組均可讀且有成員資格：主群讀到消息 `12248312`，已過期；新增群組未找到青元子入口。成功讀取但沒有入口是正常等待狀態，與來源讀取失敗分開記錄。9 月 11 日 00:22:34，新主控的六帳號均回報兩群監控、身份、成員及驗證器檢查通過，`idle_waiting_event`。探測沒有建立 WebView、發送 Telegram 消息或遊戲 POST，也沒有重用過期入口。

## Mini App 完整結構與現行契約

公開頁面：`https://asc.aiopenai.app/miniapp/xianxia-world-boss`。20:55:13 GET 返回 200，**120663 bytes、3135 行、68 個 JavaScript 函數、27 個 DOM ID**。SHA256 為 `3d1c01e987edfe45ea67e696435ad53bf60290fca91e61016953dfd2f2091d37`，與 9 月 9 日保存的頁面相同。這是公開客戶端分析，沒有取得遊戲伺服器源码。

下列行號指本次保存的 `official-miniapp.html`。完整 DOM、資源、函數及接口索引見同目錄的 `miniapp-structure.json`。

| 區塊 | 頁面位置 | 狀態及行為 | 自動化對應 |
|---|---|---|---|
| 版面、樣式及資源 | 1–768 | 戰場 canvas、身份選擇、加入覆蓋層、血量、狀態、按鈕；Telegram SDK、Turnstile | 公開頁面交由原生瀏覽器處理驗證；視覺層不產生戰果 |
| Telegram 與初始化 | 770–907 | `ready/expand`；entryToken、sessionToken、initData；room/challenge/player/windows | `qyz_auth` 的帳號綁定及簽名 WebView 解析；帳號各自的 HTTP 客戶端 |
| Turnstile | 908–1042 | `qyz_world_boss_begin`；widget callback、過期與錯誤清理；一次性 token + UUID | 原 `NativeTurnstileBrowser` / `AutomaticTurnstileWorker`，帳號隔離的記憶體 broker |
| 請求與錯誤 | 1178–1255 | JSON POST、逾時、限定重試、伺服器錯誤碼 | `qyz_http` 限定官方 origin、接口、欄位、大小；不跟隨跳轉，HTTP 層不自動重放 |
| 身份與加入房間 | 1295–1564 | `needsIdentitySelection`、`identityChoices`、`playerId`、`sessionToken`；等待 joining 轉 battle | 參考 `_wait_for_challenge` 選主魂，使用回傳 sessionToken，按官方剩餘入場時間輪詢 |
| 房間狀態 | 1565–1764 | HTTP state 與 WS ticket；人數不足、鎖房、關閉 | 參考實作從 start/window/charge/hit 回應更新狀態，WS 為頁面的即時房間展示通道 |
| 開始戰鬥 | 1765–1837 | begin 後依 `startsInMs` 和 RTT 建立戰鬥時鐘 | 保留參考的 begin、驗證換發及時鐘校正；排除瀏覽器等待和 Siteverify 時間 |
| 動態窗口 | 1838–1875 | 以 `afterWindowId` 取下一窗；更新 `windowCount`；每約 360 ms 輪詢 | 參考 reveal/strike 並行；去重、處理空窗，過早 done 不代表收齊全部窗口 |
| 反擊、蓄力、命中 | 1876–2221、3031–3080 | 過期窗口扣血，按下取得 chargeTicket，鬆手記錄 action，再送 hit；回執可異步完成 | 新 `qyz_combat.CombatState` 記錄本地扣血與連擊，原參考負責時間與命中；伺服器回執獨立保存 |
| 結算與退出 | 2231–2288 | 等待所有 hit 回執後送 bossProof；讀取 result.grade/score/player_hp | finish proof 使用本帳號實際本地記錄；成功仍要求伺服器 finish 和逐窗 hit/perfect 證據 |
| 畫面與輸入生命週期 | 2289–3130 | RAF、可見性、窗口/特效繪製、pointerdown/up/cancel/leave、online/visibilitychange | 持續事件監聽及非 GUI 戰鬥協程；繪圖與本地 demo 不當作實戰證據 |

共 8 個 JSON POST 契約，逐個從本次 HTML 抽取欄位並與 `qyz_http.ENDPOINT_KEYS` 比對，全部相同。公共欄位均為 `token`、`initData`：

| 接口 | 其餘請求欄位 | 主要回應/用途 |
|---|---|---|
| start | playerId | 身份選擇、sessionToken、boss/player/room/challenge |
| state | 無 | 房間與 Boss 狀態 |
| ws-ticket | 無 | `/ws/miniapp/xianxia-world-boss/state` 的一次性 ticket |
| begin | challengeId、turnstileToken、turnstileIdempotencyKey | startsInMs；參考保留不帶驗證值的初次探測 |
| window | challengeId、afterWindowId | window、windowCount、done |
| charge-start | challengeId、windowId | chargeTicket；沒有票據不發 hit |
| hit | challengeId、windowId、chargeTicket、elapsedMs、holdMs | hit.damageYi、hit.perfect 與時鐘/蓄力反馈 |
| finish | bossProof | result.grade、result.score、result.player_hp |

`actionLimit=1` 代表一次完整參戰資格，不能拿來限制成只打一次。實際攻擊次數仍依每場伺服器聲明的 `windowCount`，本次公告前十名均顯示強攻 16，但沒有用固定 16 取代動態契約。

### 漏招與結算修復

參考 `_fight` 原本把 proof.playerHp 固定為初始血量、dead 固定 false，combo 直接等於全部本地命中數。新增帳號內的 `CombatState`，不修改三份來源腳本：

- 超過 `centerMs + hitMs + 80` 且尚未處理的窗口，依 phase 1/2/3 扣 **16/22/30** HP，同一窗口只扣一次，連擊歸零。
- 未命中且蓄力超過 1800 ms 額外扣 8 HP；完美蓄力仍為 520–1250 ms，時間容差來自各窗口。
- 鬆手時立即登記本地 action。hit 回應遲到、票據缺失或伺服器拒絕，不會被錯算成「本地從未鬆手」；本地 perfect 也不代替伺服器 perfect。
- HP 歸零後停止這個帳號的後續攻擊，提交真實 HP/dead/actions/clientStats。個人倒地不寫共用 Boss 擊敗標記，不停止其他帳號。
- `local_missed_windows` 表示本地過期漏窗，`missed_windows` 表示伺服器聲明窗口数減去已確認 hit 數；兩者不是同一證據。`server_result` 保留伺服器結算，`local_player_hp/local_dead` 保留本地觀測。

只有本帳號的 confirmed hit/perfect 數滿足伺服器聲明窗口數、finish 已確認、無本地漏窗或倒地及伺服器未判死亡，才記錄 `settled_perfect`。離線數字和前端 demo 都不能證明新場次實戰通過。

### 重啟恢復與健康狀態

主控與健康檢查共用根目錄 `qyz_state.py` 的版本和日誌判斷。新主控在取得帳號進程鎖後，將上次遺留的已提交非終態轉為 `unknown_mutation_outcome`，保存原 phase 與 receipts。健康探測仍檢查連線、身份、群成員、來源及驗證器，但不能把這些檢查替代業務結算。未核對事件即使不再是最後一條，也繼續告警並保留，不隨最近 20 條記錄的裁剪丟失；不重新派發已提交事件。

## 驗證與部署

- 先用舊程式重現：23 號公告被拒；四種非終態重啟後被報 idle；漏窗後 finish 回報 100 HP，連續致命漏招仍回報未倒地。
- `qingyuanzi-new-source/` 全目錄 **88 項離線測試通過**，包括同目錄的其他 Mini App 回歸；QYZ 相關的動態窗口、六帳號隔離、任意發件人、兩群監控、過期入口、慢回執、扣血/倒地、CF 及來源腳本 fidelity 均覆蓋。
- 主項目的 QYZ 健康、健康檢查效率、告警、溫養雙修回執、Mini App / 排程註冊表、到期調度及三項註冊清單檢查，共 **122 項測試通過**。不代表主項目全量測試或所有歷史部署清單均通過。
- workspace 與 project 的 `scripts/ops-alignment-check.sh` 均不存在；運維核對以實際 LaunchAgent、PID、版本及帳號狀態完成。
- 確認六帳號無進行中戰鬥後，重載現有 `ai.hermes.xiuxian.qingyuanzi-six.monitor`，由原 KeepAlive 重啟。移除最後一處發件人查詢依賴後，**2026-09-11 02:17:28** 回讀最終 PID **41848**：六帳號均為 v9、兩群監控正常、`idle_waiting_event`。部署記錄保存初次及最終重載時間、PID 和來源檔案 SHA256。
- 生產與測試共 138 個 Python 檔案語法檢查、相關 shell 語法及 `git diff --check` 通過；不讀取 `references/`、`archive/` 作生產檢查。

所有生產程式留在 XIUXIAN 根目錄，沒有新增排程或第二個主控。每帳號狀態仍由現有主控在帳號租約下寫入；`CombatState` 僅在記憶體執行，由原 trace 回寫同一事件日誌。三份 `qyz_reference` 原始來源、適配清單和 fidelity 雜湊未變。

## Hermes 健康檢查告警

修復原有 cron `7bf4e7f517b4`（30 分鐘一次），維持同一個腳本入口和狀態檔。最初 `ok=76, warn=1, bad=7` 的通知只列前三條相同 QYZ 故障，不能從該輸出確定第七項原因；後續快照只剩六項 QYZ 故障及一項溫養雙修警告，不能倒推為第七項已被本輪特定修復。

- 溫養雙修原產生 `success`，健康檢查卻只接受通用 `acknowledged`。現只在 `last_attempt` 與 `last_success` 的帳號、消息 ID、直接回覆關係及成功回執一致且近期時認可，並讀取該流程的 `next_due_ts`。裸 `success`、回執缺失、錯帳號、新失敗或新日誌錯誤不會被放行。
- 通知層以兩次檢查後的實際 severity 為準，不再用 `idle_waiting_event`、冷卻或其他成功狀態名稱掩蓋新的 bad。相同原因按帳號合併，保留其他不同故障；註冊表或狀態根目錄失敗亦可告警。
- 健康快照仍靜默，但必須更新通知去重檔，確保恢復後同一故障再次出現時可以重新通知。腳本整合測試覆蓋「故障 → 健康 → 同一故障」。
- `flow_registry.py` 統一排程解析及標籤映射。六個 QYZ 註冊項只對應同一個 `qingyuanzi-six.monitor`；恢復腳本原本硬編碼的九個其他輔助任務搬至 `helpers`，連同到期調度器共十項一併檢查。`@demo_player_06` 輔助任務採業務帳號 `tg9100000006`，保留憑證來源映射。恢復計畫共 89 個不重複標籤，安裝 plist 全部存在；本輪只執行 `--list`，沒有整批重啟發送器。
- 到期調度改為非強制 `kickstart`，不再殺掉已在等待倒數的發送器。
- 新增 helper 檢查曾把兩個 watchdog 的 `spawn scheduled` 誤判為停止，造成 `ok=92, bad=2` 通知。實際兩者上次退出碼均為 0，屬既有單次檢查後由 KeepAlive 再排程的行為；已修正並覆蓋測試。未載入、停用、非零退出及 KeepAlive 未排程仍告警。

00:29 的原 84 項業務檢查全部正常；補齊 helpers 後共 94 項。01:32 快照為 93 正常、1 故障，兩項 helper 誤報已清除，剩餘為 `tg9100000002/jiyin_choice` 的連線錯誤。01:39 對該任務同一份 session 做只讀探測，授權、身份及群消息讀取全部通過；Surge 記錄亦有成功代理連線，未改動全域路由。02:05 在業務鎖釋放後重載該既有監控，並保留成功無事件掃描日誌作恢復證據；新服務 PID 37033，02:06–02:10 連續多次讀取兩群成功。

**2026-09-11 02:18:41**，原 cron 腳本兩次實際檢查後：`overall_status=healthy, ok=94, warn=0, bad=0`。標準輸出為空，通知去重檔的活躍故障數為 0。回讀結果保存在 `healthcheck-after.json`，沒有手動觸發 Hermes 對外通知。

排程核對亦辨識到既有 `recipe_notice` 由 Hermes gateway 的子進程 PID 73656 執行（父進程 29796），其獨立 LaunchAgent 尚未載入。本輪保留這個既有執行者；94 項健康結果不等於聲稱所有業務均由 launchd 啟動。日後整體恢复需要先核對這個跨 runtime 所有權，避免啟動第二個通知實例。

## 證據與待驗證事項

本機診斷位於 `qingyuanzi-new-source/state/reports/2026-09-10-review/`：`battle-summary.json`、`telegram-announcement-review.json`、`source-validation-multigroup.json`、`frontend-fetch.json`、`official-miniapp.html`、`miniapp-structure.json`、`deployment.json`、`healthcheck-after.json`、`jiyin-readonly-probe.json`、`jiyin-recovery.json` 及測試輸出。早期 Bot 名單僅是歷史觀察，已不作生產政策。無關聊天搜尋結果已移除，沒有保存入口 token、簽名 initData、驗證 token 或蓄力票據。

本輪沒有新的有效場次；9 月 10 日入口已過期。下一場仍需逐帳號核對實際 start/begin、完整動態 hit/perfect 和 finish 回執。入口識別通過、88/122 項測試及空閒心跳，都不能表述為已取得六帳號 16/16 的實戰結果。
