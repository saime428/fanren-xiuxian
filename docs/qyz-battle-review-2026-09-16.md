# 2026-09-16 青元子復盤：事件 126、83/96 服務器命中、Turnstile 串行入場根因與 v19 並行修復

## 一、結論

本輪復盤對象是官方群話題中的青元子事件 126：

- 官方入口：`https://t.me/example_game_group/7310786/12355818`
- 官方結算：`https://t.me/example_game_group/7310786/12355834`
- 入口時間：2026-09-16 13:40:06 Asia/Shanghai
- 結算時間：2026-09-16 13:43:32 Asia/Shanghai
- 動態窗口：每帳號 16 窗，六帳號應有 96 窗
- 本地逐擊回執：83 次服務器接受、77 次服務器 PERFECT
- 正式 `/finish`：4/6 成功
- 官方榜單：四帳號共 61 命中、55 PERFECT，總傷害 25.91446048 億億
- 兩個未正式結算帳號：`tg9100000005/@demo_player_05`、`tg9100000006/@demo_player_06`
- 兩者的 `/finish` 均被服務器以 `boss_client_clock_mismatch` 拒絕；其逐擊回執可證明局部貢獻，但不能冒充正式結算

本輪最大損失不是窗口算法再次本地超時，而是**六帳號共用一個 Turnstile 瀏覽器 worker，導致 `/begin` 被串行化 47.829 秒**。後入場帳號還未完成自己的 16 窗，全局 Boss 已被其他參戰者擊殺並結算。

已將生產運行版由 `qyz-reference-v18` 升為 `qyz-reference-v19`：六個業務帳號各自使用獨立 native Chrome profile、獨立 verifier worker、帳號過濾的內存 broker 視圖；只共享不落盤的總 broker 接口。這消除了本場有直接證據支持的最大瓶頸。

## 二、證據口徑

本文件只採用以下證據：

1. 六帳號各自的 `battle_diagnostics/12355818.json`。
2. 六帳號 `world_boss_state.json` 中同一事件、同一帳號的服務器回執。
3. 官方 Telegram 入口消息 `12355818` 與結算消息 `12355834`。
4. 公開官方頁面的當前前端流程。
5. 上游 `Waaiging/fanrenxiuxian_auto` 最新 `main`：
   `c584bcf24bce29274138d788140d393df3896bed`，標題為
   `Fix world boss deadline waits and reconcile battle results`。

認定規則：

- 本地計時、widget callback、窗口狀態或 `perfect=true` 不是正式結算。
- 單擊成功必須有服務器 `/hit` 接受回執。
- 正式戰績以 `/finish` 和官方 Telegram 結算為準。
- `tg9100000005` 與 `tg9100000006` 沒有正式 finish，因此不把 22 次局部命中加入官方 61 次命中。
- 所有認證材料、Telegram session、initData、Turnstile token、簽名 URL 均未寫入報告。

結構化證據：

- `state/reports/2026-09-16-qyz-review/event-126-stage-analysis.json`
- `state/reports/2026-09-16-qyz-review/telegram-official-messages.json`
- `state/reports/2026-09-16-qyz-review/upstream-differential.json`

## 三、六帳號結果

### `tg9100000001 / @demo_player_01`

- `/begin`：13:41:44.211
- 相對入口延遲：98.211 秒
- 揭示：16/16
- 服務器命中：16/16
- PERFECT：14/16
- 正式 finish：成功
- 官方榜單：第 10 名，3689 分，6.78 億億
- 兩次普通命中：第 4、6 窗

### `tg9100000002 / @demo_player_02`

- `/begin`：13:41:51.789
- 相對入口延遲：105.789 秒
- 揭示：16/16
- 服務器命中：15/16
- PERFECT：14/15
- 正式 finish：成功
- 官方榜單：第 21 名，3330 分，6.43 億億
- 一次普通命中、一窗未接受

### `tg9100000003 / @demo_player_03`

- `/begin`：13:42:01.859
- 相對入口延遲：115.859 秒
- 揭示：16/16
- 服務器命中：16/16
- PERFECT：14/16
- 正式 finish：成功
- 官方榜單：第 16 名，3602 分，6.68 億億
- 兩次普通命中：第 1、3 窗

### `tg9100000004 / @demo_player_04`

- `/begin`：13:42:11.852
- 相對入口延遲：125.852 秒
- 揭示：15/16
- 服務器命中：14/16
- PERFECT：13/14
- 正式 finish：成功
- 官方榜單：第 27 名，3033 分，6.03 億億
- 一次普通命中、一窗未接受、一窗因全局結束未揭示

### `tg9100000005 / @demo_player_05`

- `/begin`：13:42:22.931
- 相對入口延遲：136.931 秒
- 揭示：12/16
- 局部服務器命中：12/12
- 局部 PERFECT：12/12
- 正式 finish：失敗
- 服務器原因：`boss_client_clock_mismatch`
- 官方榜單：無正式條目

### `tg9100000006 / @demo_player_06`

- `/begin`：13:42:32.040
- 相對入口延遲：146.040 秒
- 揭示：11/16
- 局部服務器命中：10/11
- 局部 PERFECT：10/10
- 正式 finish：失敗
- 服務器原因：`boss_client_clock_mismatch`
- 官方榜單：無正式條目

## 四、戰鬥邏輯逐環節解析

### 1. 官方入口發現

唯一入口來自已授權官方群、話題與官方 bot 的 `qyz_` deep link。六帳號消費同一 entry fingerprint；沒有發現 fallback 到舊消息、其他群、demo 頁或錯誤 bot。

入口在 13:40:06 發布。六帳號首次 `/start` 確認均在入口後約 7.7–8.7 秒，入口 fan-out 本身沒有形成 48 秒級差距。

### 2. 帳號與 WebView 認證

每個業務帳號仍使用自己的 Telegram session source、WebView RPC、initData、狀態目錄和 `protocol.lock`。`@demo_player_06` 仍由其指定 session source 驅動，但業務狀態歸屬 `tg9100000006`。

認證值只在內存傳遞；沒有寫入診斷、狀態、報告或瀏覽器 profile。

### 3. `/start` 與 challenge

`/start` 只負責入場、身份選擇與 challenge。六帳號均取得服務器聲明的 `windowCount=16`。日誌中的多次 `start_confirmed` 是戰鬥開放前的受控輪詢，不是多次結算，也不是把 `actionLimit=1` 誤解為只能打一擊。

### 4. Turnstile 與 `/begin`

舊生產拓撲只有一個 `VerificationService`、一個 native Chrome、一個 `AutomaticTurnstileWorker`。六個帳號的 challenge 綁定 request 雖然互相隔離，但 worker 每次只能處理一個 widget。

實際 `/begin` 順序及帳號間距：

- `tg9100000001`
- 7.578 秒後 `tg9100000002`
- 10.070 秒後 `tg9100000003`
- 9.993 秒後 `tg9100000004`
- 11.079 秒後 `tg9100000005`
- 9.109 秒後 `tg9100000006`

首尾相差 47.829 秒。這是事件 126 最強、最直接的根因證據。

### 5. 戰鬥時鐘與網絡基準

v18 已把瀏覽器等待時間從戰鬥 RTT 基準中排除，並使用服務器 `startsInMs`、受控 RTT 和 event-loop 絕對 deadline。事件 126 沒有再次出現「本地 scheduler stall 後重睡舊相對延遲」造成的系統性窗口跳過。

因此，本場後十窗缺失不能再歸因於上一輪的本地 20 秒 stall bug；它們發生在晚入場帳號碰到全局 Boss 結束之後。

### 6. `/window` 動態揭示

v18 繼續以服務器窗口為權威，保留 `after_window_id` cursor，單次讀取有界為 1 秒，並按 request start 維持輪詢節奏。

總計只揭示 86/96 窗：

- 完整揭示：`demo_player_01`、`demo_player_02`、`demo_player_03`
- 15 窗：`demo_player_04`
- 12 窗：`demo_player_05`
- 11 窗：`demo_player_06`

未揭示十窗是全局事件在 13:43:32 結算時，晚開始帳號尚未走完個人時間軸；不能靠忽略全局 stop 或偽造窗口補回。

### 7. `/charge-start`

每一窗先按服務器中心時刻、最小 hold 和當前 RTT 計算 charge 目標。只有服務器確認 granted 的 charge 才能進入 hit。

83 份被接受的 hit receipt 證明其對應 charge 已建立。沒有證據顯示本場主要損失來自 charge 被系統性拒絕。

### 8. `/hit` 與 PERFECT

本地共獲得 83 次服務器接受，其中 77 次 PERFECT、6 次普通命中。六次普通命中的共同特徵是 hit HTTP RTT 突增：

- `demo_player_01` 第 4 窗：712 ms RTT，服務器 delta 271.6 ms
- `demo_player_01` 第 6 窗：977 ms RTT，服務器 delta 459.5 ms
- `demo_player_04` 第 1 窗：809 ms RTT，服務器 delta 224.1 ms
- `demo_player_02` 第 3 窗：695 ms RTT，服務器 delta 412.9 ms
- `demo_player_03` 第 1 窗：501 ms RTT，服務器 delta 214.6 ms
- `demo_player_03` 第 3 窗：828 ms RTT，服務器 delta 509.2 ms

對比：

- 普通命中 RTT 中位數：760.5 ms
- 已保留完整 request diagnostics 的 PERFECT RTT 中位數：387.5 ms
- 普通命中本地 wake lateness 中位數：23 ms，最大 77 ms
- 普通命中服務器 delta 中位數：342.25 ms
- PERFECT 服務器 delta 中位數：51.0 ms

結論：本場六次普通命中與孤立的網絡/傳輸延遲尖峰高度相關，而不是 event-loop 全局喚醒失控。現在沒有足夠的 signed arrival direction 證據支持整體提前固定毫秒數；硬調固定 lead 可能把其餘 77 次 PERFECT 推到過早一側。因此本輪不過擬合 hit lead 或 hold。

### 9. `/finish`

四個較早開始的帳號完成正式 finish。兩個最晚帳號雖然分別拿到 12/12、10/10 的局部 PERFECT，卻在事件接近全局關閉時收到 `boss_client_clock_mismatch`。

此錯誤原樣保留為服務器拒絕，沒有被泛化成成功或普通 timeout。`/finish` 是非冪等結算；上游沒有證明可安全重放，因此不做盲重試。

最小正確修復不是「看到 clock mismatch 就重送」，而是消除造成它的 47.829 秒入場串行，讓六帳號在同一早期批次開始並在全局結束前完成自己的時間軸。

### 10. 官方結算對帳

官方榜單只有四個正式 finish 帳號：

- `demo_player_01`：16 命中 / 14 PERFECT
- `demo_player_03`：16 / 14
- `demo_player_02`：15 / 14
- `demo_player_04`：14 / 13

四者的服務器風控均為 `damageMultiplier=1.0`、`review=false`。所以本場正式帳號的分數損失來自窗口、拒擊、普通命中和開始順序，不是已證實的服務器降傷。

## 五、v19 最小修復

### 生產拓撲

新增 `ParallelVerificationService`：

- 六個業務帳號各一個 `VerificationService`。
- 六個不同的 `browser-profiles/<account>`。
- 六個獨立 `AutomaticTurnstileWorker`，可同時進行官方 widget callback。
- 全局仍只有一個 memory-only broker API，供 `ProtocolRuntime` 使用。
- 每個 worker 只看到 `broker.for_account(account)` 過濾後的 request。
- `record_browser_event`、`submit_token`、`take_token`、`record_result`、`cancel` 全部再次校驗帳號歸屬。
- 子服務停止時只取消自己帳號的 request，不可取消其他帳號。
- 每個帳號只讀取自己的 verifier capability；一個 shard 故障不會把其他五帳號一起擋在 preflight。
- 每個 shard 只在該帳號持有 `account_process_lease` 後啟動，並在釋放同一把鎖之前完成關閉，避免兩個 host 爭用同一 profile。
- fleet close 使用 `return_exceptions` 完成全部 shard 清理，之後無條件清空 broker；重複 close 不會再次執行生命周期。
- profile 根固定在解析後的 canonical `state/` 下；逐級 `lstat` 拒絕 symlink ancestor/child，並在 shard start 與真實 browser start 前重新驗證，阻止構造後替換 profile path。
- token 仍是一次性、內存中、用後即清；沒有持久化 proof。

### 宿主接線

- `qingyuanzi_host.py` 通過 `build_verification_service()` 構造六分片服務。
- `run_account()` 把 shard 的 start/close 包在各帳號 `protocol.lock` 生命周期內。
- host 全局 verifier 引用即使清理拋錯也會在 `finally` 中清空。
- `--check` 顯式輸出：
  - `runtime_version=qyz-reference-v19`
  - `verification_parallelism=6`
  - `verification_isolation=account_profile`
- 舊的單 worker 類保留作為每個帳號 shard 的內部實現；生產不再讓六帳號排同一條 widget 隊列。

### 刻意不改的部分

- 不改 `/window → charge-start → hit → finish` 的上游時序語義。
- 不增加固定 hit lead；事件 126 的普通命中是延遲尖峰，尚無安全的全局提前量證據。
- 不重放 `boss_client_clock_mismatch` 的 finish。
- 不移植上游磁盤 checkpoint/recovery；避免 initData、session token 或一次性驗證值落盤。
- 不忽略全局 boss stop，不偽造尚未揭示的窗口。

## 六、TDD 與驗證

### RED → GREEN

1. 新增「六帳號驗證必須同時進入、profile 必須唯一」測試；修復前缺少 `ParallelVerificationService`，RED。
2. 新增 host builder 契約；修復前 `build_verification_service` 不存在，RED。
3. 新增 readiness 並行度/隔離/版本契約；修復前缺少欄位，RED。
4. 獨立審查發現 capability 全局連坐、close 非例外安全、profile 生命周期未綁帳號鎖；三類測試先 RED，再做最小修復。
5. 新增 traversal、大小寫 alias、直接 symlink profile 拒絕測試；舊實現先 RED，再加入路徑閘門。
6. 第二次獨立審查發現 ancestor symlink 與構造後替換缺口；兩個回歸測試先 RED，再加入 canonical root、逐級 `lstat` 與 start-time revalidation。
7. 最小實現後，同一組測試全部 GREEN。

### 離線與回歸

- verifier/host 重點測試：25/25
- `qingyuanzi-new-source`：160/160
- 全倉回歸：628/628
- reference fidelity：1/1
- `compileall`：通過
- `git diff --check`：通過
- host `--check`：通過

### 真實公開頁並行預熱

使用臨時、無帳號資料的六個 profile，僅打開官方公開頁，不生成 token、不打戰鬥 POST：

- 六個 shard 全部 `ready=true`
- 各自準備耗時 17.067–17.461 秒
- 六個準備動作並行完成，而不是 6 × 17 秒串行
- 測試結束後無遺留臨時 Chrome 進程

這證明並行瀏覽器和 profile 生命周期可用；它仍不是新事件的真實 `/begin` 或服務器結算證據。

### 獨立審查

- 第一輪拒絕：全局 capability 連坐、close 例外安全、profile 生命周期未綁帳號鎖；全部以 RED→GREEN 修復。
- 第二輪拒絕：ancestor symlink 與構造後 path replacement；全部以 canonical trusted root、逐級 `lstat`、shard/browser start 重驗證修復。
- 最終獨立複審：`passed=true`，`security_concerns=[]`，`logic_errors=[]`。
- 唯一非阻斷建議是日後再加一條直接調用 `PreparedNativeTurnstileBrowser.start()` 的 path replacement 測試；當前 production browser start guard、shard start guard 和對應行為已被複審確認。

## 七、生產載入結果

在確認入口 `12355818` 已有官方結算、六帳號最新事件均為終態後，重載前再次只讀掃描兩個授權 source：第一來源入口後 3000 條消息（ID `12358536..12361727`）及第二來源最新 3000 條消息均未發現新 qyz 入口。

- LaunchAgent：`ai.hermes.xiuxian.qingyuanzi-six.monitor`
- 當前 PID：`64770`
- 生產 host 進程：1 個
- `protocol.lock`：六份，全部由 PID 64770 持有
- 六帳號：`runtime_version=qyz-reference-v19`
- 六帳號：`monitor_active=true`、`connected=true`
- 本次啟動後六帳號均有 `QYZ protocol monitor ready` 日誌
- 本次啟動段未見 traceback、import error 或 type error
- 事件 126 的六份 diagnostics 在重載前後 SHA-256 完全不變

歷史業務狀態沒有被重啟洗綠：

- 四帳號仍為 `settled_partial`
- 兩帳號仍為 `unknown_mutation_outcome`
- 手工 healthcheck 因本場未全 PERFECT，仍把六個 QYZ 項標為 `bad`；這是對事件 126 的真實歷史評價，不是 v19 進程啟動失敗

## 八、下一場驗收標準

v19 的離線和公開頁並行性已驗證，但沒有新事件就不能宣稱實戰修復成功。下一場必須逐項確認：

1. 六帳號 `/begin` 首尾跨度顯著低於本場 47.829 秒；目標小於 5 秒。
2. 六帳號均從服務器揭示 16/16 窗。
3. 六帳號均取得 16/16 服務器接受命中。
4. 六帳號均取得正式 `/finish`。
5. 官方 Telegram 結算同時列出六帳號。
6. 目標 96/96 PERFECT；如仍有普通命中，再按 signed arrival direction、hit RTT 和窗口序號決定是否增加動態 lead，不能按感覺調固定 sleep。
7. `damageMultiplier` 必須保持 1.0，`review=false`。
8. 六帳號的 profile、token、request、狀態、鎖及結果仍須可機械證明互不串線。

本輪優化提升的是**可完成窗口數和正式結算率的首要瓶頸**。PERFECT 延遲尖峰仍需下一場服務器證據驗收，不作提前成功承諾。
