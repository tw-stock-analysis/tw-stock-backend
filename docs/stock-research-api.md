# 上市證券搜尋與真實個股詳情

四個端點在 `app/api/v1/endpoints/stocks.py` 以 `@router.get` 明確宣告，經 `StockService` 使用既有 TWSE adapter。沒有行情資料庫、資料匯入、行情快取、自動重試或其他供應商。目前合計 **51 個 API 路由**：44 個原始 TWSE、1 個功能目錄、2 個健康檢查、4 個個股端點。

| GET 端點 | 用途 | 每次取得的官方來源 |
| --- | --- | --- |
| `/api/v1/stocks?q=&page=1&limit=20` | 依代碼／名稱搜尋、分頁 | `/exchangeReport/STOCK_DAY_ALL` |
| `/api/v1/stocks/{code}` | 單一證券成交摘要 | `/exchangeReport/STOCK_DAY_ALL` |
| `/api/v1/stocks/{code}/valuation` | 本益比、殖利率、股價淨值比 | `/exchangeReport/BWIBBU_ALL` |
| `/api/v1/stocks/{code}/margin` | 融資融券明細 | `/exchangeReport/MI_MARGN` |

TWSE 來源仍是全集快照，不傳入自訂 code/date。後端每次重新取得全集，再於本次記憶體中搜尋或精確比對代碼。分頁只縮小回傳結果，不減少來源下載量。`q` 最長 100 字，忽略前後空白及大小寫；page 至少 1，limit 1–50，額外參數回 422。代碼為 4–6 位大寫英文字母或數字，使用字串保留前導零。

搜尋回傳 `data`、`pagination` 與 `meta`；單股三個區塊各自回傳具名型別的 `data` 與 `meta`。數值保持來源精確字串或 null，不換算股／張、不計算缺失的百分比、不填 0。身份欄位缺漏或代碼重複回 502，避免拼接錯誤證券。

`meta.source_url` 為該區塊官方網址，`retrieved_at` 是 UTC 取得時間，`data_date` 保留來源日期字串。成交與估值日期可不同。資券來源未提供日期，`data_date` 固定 null，明示不能以取得日期補上交易日；資券數值沿用來源單位。

成交來源沒有該代碼時，摘要回 404 `STOCK_NOT_FOUND`，不推論是否下市。成功取得估值／資券來源但沒有該代碼，回 200、`data: null`。來源失敗回 503，格式錯誤回 502；標準錯誤含 request_id，沒有內部資訊。所有回應 `Cache-Control: no-store`。

前端 `/stocks` 為搜尋頁，`/stocks/{code}` 為真實詳情。詳情依序請求成交 → 估值 → 資券；每個 HTTP 請求只取一個來源，維持既有來源 30 秒及 Nginx 60 秒上限。成交未知／失敗時停止後續區塊；估值／資券個別失敗時，其他區塊顯示同一輪成功結果。更新期間隱藏舊內容，完成後不以舊值補失敗區塊；取消導覽會中止前端請求並停止後續區塊。

路由按當次來源代碼查詢，不宣稱是跨代碼變更的永久公司 ID。不含公司基本資料、產業、營收、財報、會員、自選儲存、歷史資料庫及其他來源。既有三個 `demo-*` 個股頁與自選頁仍明確標示示範。

## 驗證

先以獨立 Compose 專案啟動整站，再執行：

```bash
GATEWAY_BASE_URL=http://127.0.0.1:8080 uv run pytest tests/test_gateway.py -q
GATEWAY_BASE_URL=http://127.0.0.1:8080 RUN_LIVE_TWSE_TESTS=1 uv run pytest tests/test_gateway_live.py -q
```

真實來源測試逐次取得全部 44 個原始 API，再驗證搜尋、重複成交摘要、估值與資券；沒有錄製行情。資料庫測試以 `RUN_DB_TESTS=1` 指向獨立、已遷移的測試資料庫，確認 `app`／`market` schema 皆無業務資料表。雲端代理與 CA 掛載使用環境專用 Compose override，憑證及測試密碼不納入儲存庫。

2026-10-19（臺北）完整後端測試 **154 項全過、0 略過**：95 項單元／合成契約、1 項 PostgreSQL、13 項 Nginx、45 項真實 TWSE 閘道測試。Ruff、mypy（29 個來源檔）、Python 套件建置與 `alembic check` 通過；沒有新增 migration。

Chromium 在同一個 Nginx 部署完成 8 項桌面／手機流程驗證，共 23 個個股 HTTP 請求，量測同時最多 1 個請求，沒有 JavaScript 錯誤或整頁橫向溢出。涵蓋搜尋分頁、選股、手動更新、直接開啟與重新整理、0050 前導零及無估值、未知代碼，以及受控來源連線失敗回 503 後清除舊值、恢復連線。驗證摘要見 [stock-research-verification.json](stock-research-verification.json)，不含行情紀錄。
