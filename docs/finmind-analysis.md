# FinMind 個股歷史研究

新增 `GET /api/v1/stocks/{code}/analysis`，對應前端 `/stocks/:code/history`。

## 設定與驗證

後端 `.env` 或執行環境 Secret 設定 `FINMIND_API_TOKEN`。空值使用匿名免費額度；有效 Token 使用會員額度。Token 僅以 Bearer Header 傳給固定主機，不放入 URL、JSON 回應或前端。Compose 既有後端 `env_file` 會載入此設定，無須改資料表或加前端金鑰。

執行環境須允許 `api.finmindtrade.com`。這輪未加入會員用量查詢，不會請求 `api.web.finmindtrade.com`。來源封鎖或無權限直接顯示錯誤，不繞過代理。

只讀驗證，不開資料庫連線、不寫行情檔案：

```bash
uv run python -m app.services.finmind_check --code 2330
uv run python -m app.services.finmind_check --code 0050 --start-date 2026-07-01 --end-date 2026-07-31
```

輸出只有區塊狀態、筆數、時間與錯誤代碼。三區塊成功（包含合法空資料）才回 exit 0。

## 契約與資料流程

- 先用 TWSE 最新日成交清單確認代碼，保留目前只支援現行上市標的的邊界。找不到回 `STOCK_NOT_FOUND`，不請求 FinMind。
- `end_date` 預設台北今天，`start_date` 預設截止前 180 天；不得早於 1994-10-01、開始晚於截止、截止為未來或顯示區間超過 366 天。未知參數與無效日期在呼叫來源前回 422。
- 身分確認後，依序請求 `TaiwanStockPrice`、`TaiwanStockMonthRevenue`、`TaiwanStockInstitutionalInvestorsBuySell`。不提供自由 URL 或任意 dataset 代理。
- 每次重查均為新請求；同一 worker 內的 FinMind HTTP 請求以 lock 逐次執行。不同 worker 不共用 lock，部署容量仍依 Token 額度規劃。
- JSON 數值精確解析為 Decimal／字串，圖形才轉近似值。限制 8 MiB、15,000 列及欄位／數字長度，檢查重複、代碼、日期與必要欄位。
- 不使用行情資料庫、跨請求快取、本機行情檔、自動重試或輪詢。回應為 `Cache-Control: no-store`。

回應含 `code`、`name`、`market`、查詢區間、`identity_source`、`complete` 與三個資料區塊。區塊均有 `status`／`data`／`meta`／`error`：

| status | 含義 |
|---|---|
| `ok` | 成功；空陣列表示來源沒有資料 |
| `error` | 本次曾查詢但失敗，data 為 null |
| `not_requested` | 前一資料集已發生來源存取問題，本次未繼續查詢，附上原因 |

格式問題只影響該區塊，仍取得其餘資料。HTTP／JSON 401、403、402、429 與網路／服務故障會停止後續 FinMind 呼叫。錯誤使用固定 `FINMIND_*` 代碼與訊息，不回傳來源錯誤正文或憑證。

`meta` 含來源、dataset、來源實際查詢區間、取得時間、來源列數與單位。列數包含計算暖機資料，不等於畫面筆數。

## 計算方式

**日線**：向前最多 180 個日曆天作暖機（不早於來源起點）。依日期排序，用最近 20／60 筆來源收盤價計算簡單平均；不足回 null，四捨五入至四位小數。停牌／新上市可能仍無足夠資料。價格未還原，價與成交額為新台幣元、日成交量為股。

現行上市標的也可能有上市前紀錄。開盤欄超出高低範圍時保留數值，標記 `opening_reference`，圖中只畫高低與收盤；興櫃開盤欄可能代表前日均價。高低顛倒或收盤超出範圍則視為格式問題。

**營收**：取得截止前 48 個月份供比較，顯示截止月份往前最多 36 個月。使用 `revenue_year`／`revenue_month` 建立營收月份，金額為新台幣元。YoY／MoM 比較前 12／1 個月；缺少比較月份或基準不大於零時回 null，零營收本身保留為零。`record_date` 沿用來源 date，`recorded_at` 保留可用的 create_time；兩者不推定為正式公告時間。

**法人**：向前最多 90 個日曆天作累計暖機。買進、賣出與差額為股，各來源分類分開顯示，保留未知新分類，不將外資自營或自營商分項重複加總。`net5`／`net20` 使用含當天的最近 5／20 個來源有資料的日期；分類缺漏或筆數不足回 null，不補零，不保證上游完全沒有漏日。

## 前端與驗證範圍

個股摘要提供歷史研究連結。進頁、送出日期或手動重查只發一個站內請求；日期保留於 URL，瀏覽器不直接連 FinMind。提供日 K／成交量／均線、月營收與法人圖表，各有精確明細、來源及台北取得時間。切換法人分類只改本次視圖；更新中隱藏舊圖，失敗不沿用舊值。未新增 FinMind 原始資料下載／匯出。

自動化測試使用合成資料，不作產品行情。此輪執行環境仍回報未允許 FinMind，實際 HTTPS 請求遭代理 403，且未注入會員 Token；真實驗證待網路規則生效後執行上述指令。測試通過不等於真實來源已驗證。

正式公開圖表、API、匯出及商業使用的權限應依用途確認，整合不代表取得再散布或公開即時展示授權：[方案](https://finmind.github.io/Pricing/)、[資料授權](https://finmind.github.io/Disclaimer/)。
