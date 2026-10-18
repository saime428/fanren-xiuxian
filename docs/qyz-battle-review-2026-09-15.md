# 2026-09-15 青元子復盤：88/96 窗、兩次 finish 過早、四帳號正式結算

本場入口為 [12337832](https://t.me/example_game_group/7310786/12337832)，
北京時間 13:40:03 發出；官方結算為
[12337844](https://t.me/example_game_group/7310786/12337844)，事件編號 125。
官方公告確認 34 人參戰並成功擊退青元子。本方六帳號沒有進入前十；
`@demo_player_06` 取得珍稀掉落【萬年養魂木】x1。這項掉落證明該帳號有參戰貢獻，
不等於 `/finish` 已成功結算。

## 六帳號服務器證據

| 帳號 | 揭示窗 | 已確認命中 / PERFECT | finish | 正式傷害（億億） | 風控倍率 |
| --- | ---: | ---: | --- | ---: | ---: |
| `@demo_player_01` (`tg9100000001`) | 16/16 | 16/15 | `settled_partial` | 6.8347 | 1.0 |
| `@demo_player_04` (`tg9100000002`) | 16/16 | 15/13 | `settled_partial` | 6.4569 | 1.0 |
| `@demo_player_03` (`tg9100000003`) | 16/16 | 15/15 | `settled_partial` | 6.4917 | 1.0 |
| `@demo_player_02` (`tg9100000004`) | 16/16 | 15/13 | `settled_partial` | 5.8821 | 1.0 |
| `@demo_player_05` (`tg9100000005`) | 13/16 | 12/12，另 1 次過期 | `boss_duration_too_short` | 未正式結算 | 未分類 |
| `@demo_player_06` (`tg9100000006`) | 11/16 | 11/11 | `boss_duration_too_short` | 未正式結算 | 未分類 |

逐窗存檔合計：**96 個應有窗口、88 個已揭示、84 次服務器確認命中、
79 次服務器 PERFECT**。只有四帳號取得正式 finish；這四帳號的正式合計為
**61 命中、56 PERFECT、25.66536222 億億傷害**。兩個 finish 失敗帳號的
逐擊回執仍保留，但沒有用本地加總冒充正式結算。

失分明細：

- `@demo_player_01` 有 1 次服務器普通命中；其服務器 hold 為 1421.9 ms，
  本機 hold 為 1003 ms，該擊 hit 回覆顯著偏慢。
- `@demo_player_04` 有 1 個 `local_window_missed` 和 2 次普通命中。
- `@demo_player_02` 有 1 個 `boss_window_expired` 和 2 次普通命中。
- `@demo_player_03` 有 1 個 `boss_window_expired`，其餘 15 擊全 PERFECT。
- `@demo_player_05` 首窗晚至中心後才進入 charge，收到
  `boss_window_expired`；之後 12 擊全 PERFECT，但後三窗沒有揭示。
- `@demo_player_06` 已揭示的 11 窗全部 PERFECT，後五窗沒有揭示。

## 根因

1. **本地 20 秒無新窗被錯當成服務器終態。** 舊 `_reveal_windows` 在
   `last_reveal_at` 超過 20 秒後直接結束，沒有保持同一
   `afterWindowId` 游標等待服務器 `done`。這直接對應兩帳號只取得 13/16、
   11/16 窗；本地停頓不是服務器結束證據。
2. **窗口讀取和 event-loop 停頓會重複消耗剩餘延遲。** 舊
   `_sleep_until` 先計算相對延遲、讓出執行權後才建立 child sleep；若排程器
   已停頓，舊延遲會再睡一次。窗口讀取錯誤後亦從回覆時刻再加 360 ms，
   把慢 RTT 疊加到下一輪，造成晚揭示、過期和普通命中。
3. **finish 沒有以服務器 `minDurationMs` 作真實時間下限。** 兩個失敗
   帳號都已送達 `/finish`，服務器明確回覆 `boss_duration_too_short`；
   舊 HTTP 分類又把它外層壓成 `server_response_rejected`。上游同場復盤記錄
   兩份舊 proof 為 65,911 ms、74,566 ms，均低於本場 75,000 ms 下限。
4. **慢請求污染 hold 偏差。** 舊邏輯把所有 charge/hit 回覆的服務器 hold
   都放入持久偏差，即使請求本身很慢或曾重試；一次網路慢回覆可錯誤移動
   後續窗口的 release。
5. **「降傷」不是本場四份正式 finish 的根因。** 四份正式結算全部為
   `automation_risk.damageMultiplier=1.0`、`review=false`。兩個 finish 失敗帳號
   沒有正式風控結果，只能保留為未分類；不能把它們報成「無降傷」。
   本場傷害下降主要來自少窗、漏擊及普通命中，而不是已證實的服務器倍率削減。

## 上游對照與 v18 適配

權威上游為 `https://github.com/Waaiging/fanrenxiuxian_auto.git`。三份用戶附件
等價於上游 `4948f4fe6875f4440507340e909c4dd2f8224043`；本次審核最新 revision 為
`c584bcf24bce29274138d788140d393df3896bed`，提交標題為
`Fix world boss deadline waits and reconcile battle results`。本地只移植與本場證據
直接對應、且不破壞現有 Telegram 適配的部分：

- `/window` 上限 1 秒，所有讀取從 request start 保持 360 ms 節奏；本地
  stall 只記診斷，不再宣告終場，仍受 25 次錯誤預算和 92 秒戰鬥 deadline 約束。
- `_sleep_until` 在讓出執行權前註冊絕對 loop deadline，避免停頓後重複睡眠。
- `/finish` 等待真實 `minDurationMs + 50 ms`，並以實際 monotonic elapsed
  建 proof；`boss_duration_too_short` 保留官方錯誤碼。
- 只有單次且 charge/hit RTT 均未超出品質上限的樣本，才更新 hold skew。
- finish 回傳的有效 `hits` / `perfects` 成為結算計數權威；原始逐窗 receipts
  不重寫，非法、布林、非整數、越界計數全部退回本地回執。
- runtime 版本升為 `qyz-reference-v18`，來源 manifest 和完整
  `ADAPTATIONS.patch` 同步更新。

上游新增的磁碟 checkpoint/recovery **沒有移植**：它可能持久化 initData、
session token 或一次性驗證材料，違反本地「認證材料只在記憶體」邊界。
本地仍維持同帳號單一 mutation owner、零盲重播和六帳號獨立 transport/state。

## TDD 與驗證

先新增 6 個失敗測試，舊版實際得到 **6 failures**：

1. 20 秒本地 stall 後必須保留游標；
2. 75 秒最短時長前不可送 finish；
3. 排程器停頓後不可重複睡舊延遲；
4. 慢 charge 不可污染下一擊 hold；
5. `boss_duration_too_short` 不可泛化；
6. 正式 finish 計數必須覆蓋本地回執總數而不改寫 receipts。

最小修復後同組 **6/6** 通過；戰鬥重點套件 **51/51**、
`qingyuanzi-new-source` **149/149**、全倉 **628/628** 通過。
`compileall`、新增行秘密／shell injection／`eval`／pickle／SQL 掃描及
`git diff --check` 通過。`ADAPTATIONS.patch` 已在臨時目錄反向套用，三份附件
SHA-256 全部還原；68 個未適配函式指紋仍由 fidelity test 驗證。

獨立模型審查曾先以三個隔離 reviewer 執行，均因所選模型 HTTP 429 失敗；
其後明確指定同一所選模型 `gpt-5.6-sol` 的 Codex read-only reviewer，亦因服務
high demand 失敗。依 Boss 的「只用所選模型、禁 fallback」政策，沒有切換到
其他模型冒充獨立批准。機械安全與回歸門已完成，但本次沒有取得第二模型／第二
agent 的語義批准，因此提交不使用 `[verified]` 前綴。

## 生產交接與邊界

北京時間 2026-09-16 08:25:34，在確認沒有進行中事件後，原 KeepAlive
LaunchAgent 由 PID **66106** 交接至唯一 PID **88928**。六帳號都已回報
`qyz-reference-v18`、`execution_owner=official_protocol`、`monitor_active=true`；
PID 88928 同時持有六個彼此獨立的 `protocol.lock`，系統只有一個
`qingyuanzi_host.py` 進程。啟動後六條 ready 日誌齊全，沒有新的 traceback、
型別或匯入錯誤。六份事件 12337832 存檔 SHA-256 在交接前後完全不變。

`active_flows.json` 仍只有六個 QYZ 帳號、一個共享 LaunchAgent；Hermes cron
沒有 QYZ mutation job。工作區沒有 `scripts/ops-alignment-check.sh`，所以以
registry、LaunchAgent、進程、鎖和 cron 五項人工對齊替代。

健康檢查如實保持 degraded：四帳號仍是歷史 `settled_partial`，兩帳號仍是
`unknown_mutation_outcome`，重啟沒有把舊失敗洗綠。這次只證明新代碼已由生產
進程載入；沒有新鮮入口，故不能宣稱下一場已達 16/16 PERFECT。

本機脫敏證據：`state/reports/2026-09-15-qyz-review/`，包括
`event-evidence.json`、`telegram-official-messages.json`、
`deployment-before.json`、`deployment-after.json`。沒有向 Telegram 發送訊息，
沒有補做戰鬥 mutation，也沒有 push。
