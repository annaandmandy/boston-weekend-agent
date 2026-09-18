# 波波的 runtime identity 與記憶

波波的 production agent state 分成兩個不同更新速度的部分。

## Core identity

`persona.json` 定義語氣、角色、內容風格、禁止事項與顏文字。它跟著程式碼版本
發布，Lambda 在 ranking 與 writing 時都會把它交給模型。模型不能在 runtime
自行修改這份資料。

## Long-term preference memory

Production memory 位于：

```text
s3://boston-weekend-agent-reports/agent/bobo-memory.json
```

它記錄居住中心、距離取捨、已確認偏好、使用者回饋摘要與 evolution policy。
`services/daily-social/memory.default.json` 是 S3 尚未建立時的安全預設，不是長期
資料庫。

每次 memory 更新都应：

1. 根據明確使用者回饋或累計成效資料提出變更，不讓模型只憑一次輸出自我強化。
2. 將舊版複製到 `agent/memory-history/YYYY/MM/`。
3. 增加 `memory_version`、`updated_at`、證據數量與修改理由。
4. 經過人工 review 後才更新 `agent/bobo-memory.json`。
5. 在 ranking analytics 中記錄當次使用的 memory version。

初期可记录 `liked`、`not_interested`、`visited`、`too_far`、`worth_the_trip` 与
自由文字原因。累積至少三筆一致證據後，再將傾向整理進 `learned_preferences`。
