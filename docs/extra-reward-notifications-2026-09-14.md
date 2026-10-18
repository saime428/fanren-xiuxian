# 額外收穫通知：所有物品種類與交付回執

9 月 13 日 22:55 的官方回執
[12309907](https://t.me/example_game_group/7310786/12309907) 確認
@demo_player_01 與 @demo_player_03 共同領悟【紫铖兜图谱】。來源為已驗證的
`hantianzun21_bot`（8816935632），群組、話題及帳號均匹配。
舊解析只接受以丹方、器方、符方、陣方或配方結尾的名稱，因而漏報圖譜。
當時通知程序持續運行；重新取得官方原文亦重現舊分類器返回空值。

依使用者「不管出什麼都通知」的要求，`reward-recipient-v3` 在已驗證
的「天賜機緣」回執內接受所有額外收穫，不再限制物品名稱、種類或獲得
措辭。多件物品、數量及原始獎勵描述一併通知；沒有書名括號的新格式
也使用獎勵原文。一般修為增加不作額外收穫通知。

只通知 @demo_player_01、@demo_player_03 的相關回執；大小寫不敏感，近似名稱、
其他玩家、轉發及錯誤來源不觸發。混合玩家的回執只列出指定帳號。
通知器共用 `command_confirmation.OFFICIAL_REPLY_SENDER_IDS`，避免
單獨的舊清單漏掉其他已驗證遊戲機器人。

Hermes 使用 JSON 交付回執。只有明確成功且有 Telegram 訊息 ID 才記入
`delivery_receipt`；跳過、空回執或交付失敗均不能標記為送達。沿用
訊息 ID、收穫及原有紀錄去重，保留歷史通知。通知標題改為「額外收穫」。

本次屬於 live automation 的既有通知流程修復。原 LaunchAgent
`ai.hermes.xiuxian.tg9100000001.recipe_notice.return_observer` 仍是常駐
擁有者，狀態仍為
`state/accounts/tg9100000001/recipe_notice/notification_state.json`。
精確補發 12309907 時暫時卸載這一個通知實例，確認舊程序退出，再由
使用同一 `recipe_monitor.lock` 與 `process_recipe_message` 的一次性
恢復程序接手寫入；完成或失敗後均重新載入原 LaunchAgent。沒有新增
排程或並行 writer。這條訊息已超出最近 500 則歷史，因此補發使用精確
訊息 ID 的唯讀取得，不修改遊戲或群組訊息。

證據及交付、部署紀錄保存在
`state/reports/2026-09-13-recipe-notice-12309907/`。
