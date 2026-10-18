# TWSE 交易資料：按請求取得

目前實作以本文件與 FastAPI OpenAPI 為準；其他規劃文件不是已交付 API。

瀏覽器開啟資料頁 → Nginx → FastAPI endpoint → service → TWSE OpenAPI → 回傳前端。每次進頁及手動更新都發出新來源請求。沒有市場資料表、匯入程序、排程、行情快取、舊資料回退、自動重試或輪詢。列表篩選、圖表欄位選擇及分頁使用本次取得的結果，不額外呼叫來源。功能目錄只有靜態定義，不會一次請求全部 44 個資料 API。

## 已確認範圍

官方網站「證券交易」36 項排除權證基本資料 1 項，加入「指數」5 項、處置股票 1 項、權證交易 2 項及 ETF 定期定額統計 1 項，共 44 項。只使用 `https://openapi.twse.com.tw/v1`，不轉接官方網站其他未列出的內部介面。

不含公司基本資料、營收、財報、權證基本資料／年度發行統計、新聞及券商基本資料。此平台沒有三大法人每日買賣超 T86；外資及陸資持股統計不等於法人買賣超。可借券賣出股數不是實際借券賣出餘額。TPEx、TDCC、TAIFEX、Fugle 與舊匯入程式已移除。

實測 `TWTB4U` 目前只有 `Date`、`Code`、`Name`、`Suspension`，因此以標的及暫停先賣後買註記呈現，不宣稱提供當沖成交量值。`TWT84U` 提供漲跌停價及開盤競價基準，使用價格摘要。`ETFRank` 同時提供股票及 ETF 兩組排行，名稱與交易戶數必須使用同一組欄位。

## 端點與回應

- `GET /api/v1/twse/datasets`：功能目錄；可用 `category`、`q` 篩選。
- 44 個獨立資料端點：`GET /api/v1/twse` + 官方 path，保留官方大小寫。
- 不接受自訂 URL，也不接受未定義的 `date`、`code` 等參數；查詢參數模型為 `EmptyQuery`，額外參數回 422。
- 前端各功能頁：`/market/{dataset.id}`；首頁取 `MI_INDEX`，個股分析 `/stocks` 取 `STOCK_DAY_ALL`。

成功回應為 `TradingResponse` BaseModel：

```json
{
  "dataset": {"id": "daily-prices", "title": "…", "path": "/exchangeReport/STOCK_DAY_ALL", "…": "…"},
  "columns": ["Date", "Code", "Name", "ClosingPrice"],
  "data": [{"Date": "來源原始日期", "Code": "0050", "Name": "示意", "ClosingPrice": "123.4500"}],
  "meta": {
    "source": "TWSE",
    "source_url": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "retrieved_at": "2026-10-18T00:00:00Z",
    "retrieval": "on_request", "freshness": "source_defined", "units": "source_native",
    "row_count": 1, "note": "取得時間不代表成交時間或資料日期。"
  }
}
```

以上只是契約示意，非市場行情。所有原生欄位與字串保留，不自行換算張／股、金額或百分比。原始 JSON 數字以精確字串解碼，避免浮點誤差及大整數截斷。缺值為來源的 null、空字串或原始標記，前端不以 0 補齊。合法空陣列與請求失敗有不同畫面。前端序列圖使用近似數值，精確值以原始表格為準。

`retrieved_at` 是成功取得回應的 UTC 時間，前端以 Asia/Taipei 顯示；來源的交易日／出表日期仍保留在各資料列，不能把本次請求時間當成來源時間。日、月、年、歷史等名稱不代表可傳任意日期取得完整歷史。

來源 HTTP 錯誤／連線失敗回 503 `SOURCE_UNAVAILABLE`，來源 429 回 503 `SOURCE_RATE_LIMITED`；非預期資料格式回 502 `SOURCE_DATA_INVALID`。錯誤包含 request_id，不暴露代理、資料庫或內部例外內容。回應皆 `Cache-Control: no-store`。HTTP client 只共用連線池，不儲存行情，來源取回上限 32 MiB、150,000 列、300 欄，HTTP 20 秒及整體請求 30 秒上限，不追蹤轉址。

## 分層與既有資料庫

端點位於 `app/api/v1/endpoints/twse.py`，由 `app/api/v1/router.py` 掛載。`app/services/twse_catalog.py` 明確登錄每一個 API，`app/services/twse.py` 處理 HTTP 與資料驗證，`app/schemas/twse.py` 定義 BaseModel。44 個端點均獨立列在 OpenAPI，沒有任意網址代理。

既有 PostgreSQL、`0001_create_schemas` 與 readiness 檢查維持原部署用途；交易端點不取得 DB session。此分支先前未合併的 `0002_market_data`、`0003_open_data` 已移除，沒有執行既有資料庫刪表、降版或清除。若某個測試資料庫已套用舊分支版本，請使用獨立測試資料庫驗證本分支，勿直接清除既有資料。

## 驗證與限制

```bash
UV_CACHE_DIR=.cache/uv uv run ruff check app tests alembic
UV_CACHE_DIR=.cache/uv uv run mypy
UV_CACHE_DIR=.cache/uv uv run pytest -q
UV_CACHE_DIR=.cache/uv uv build
# 真實來源檢查：先取 Swagger，再每隔一秒逐一請求資料來源
UV_CACHE_DIR=.cache/uv uv run python -m app.services.twse_check
```

單元測試用明確標示的合成 HTTP 回應，驗證每個端點取對應來源、同一路徑連續呼叫都送出新請求、無 DB session／快取／回退、數值精度、格式錯誤、請求參數及來源失敗。真實 Nginx 測試另以 `GATEWAY_BASE_URL` 執行。

2026-10-18 23:09（臺北）完成加強版真實來源檢查：44／44 成功，44 個回應皆非空，路徑與所有實際欄位均符合當時取得的官方 Swagger。完整筆數、欄位與 UTC 取得時間見 [真實驗證紀錄](twse-live-verification.json)；不儲存原始行情或作為查詢快取。最大單一回應為年成交資訊 72,048 筆，仍在既有限制內。

檢查器支援官方目前直接描述單列物件的 Swagger，以及明確 `items` 陣列格式；欄位新增或缺少會回報 `SOURCE_CONTRACT_CHANGED` 並以非零狀態結束。合法空陣列標為 `column_contract: empty`，不宣稱已驗證其實際欄位。Swagger 前置請求失敗時不繼續發出 44 個資料請求。檢查不重試、不追蹤轉址、不讀寫資料庫。

本輪 Ruff、mypy、75 項單元／合成契約測試及 Python 套件建置通過；12 項 PostgreSQL／Nginx 整合測試因未啟動該部署而略過。這次真實來源成功代表驗證當時的連線與契約成立；來源發布頻率、資料日期與未來可用性仍由 TWSE 決定。

Chromium 經 Vite 同源代理 → FastAPI → 真實 TWSE 載入全部 44 個桌面功能頁與 6 個手機代表頁，逐頁驗證原始值、欄數與分頁；手動更新取得新時間，共 51 個逐次資料請求，沒有整頁橫向溢出或 JavaScript 錯誤。此為真實來源的前後端驗證，未重跑 Nginx 部署層；[前端紀錄](https://github.com/tw-stock-analysis/tw-stock-frontend/blob/feat/twse-trading-pages/docs/twse-browser-verification.json) 僅含驗證摘要，不含行情列。

資料署名為臺灣證券交易所；來源文件、使用條款及授權說明以 [官方 OpenAPI](https://openapi.twse.com.tw/) 為準。

## 完整對應清單

以下表格由同一份固定目錄產生，方便核對每個 endpoint 與頁面。

| 分類 | 功能 | 後端 endpoint（/api/v1/twse 之後） | 前端頁面 |
| --- | --- | --- | --- |
| prices | 上市個股日成交資訊 | `/exchangeReport/STOCK_DAY_ALL` | `/market/daily-prices` |
| prices | 上市個股日收盤價及月平均價 | `/exchangeReport/STOCK_DAY_AVG_ALL` | `/market/average-prices` |
| prices | 上市個股月成交資訊 | `/exchangeReport/FMSRFK_ALL` | `/market/monthly-trading` |
| prices | 上市個股年成交資訊 | `/exchangeReport/FMNPTK_ALL` | `/market/yearly-trading` |
| prices | 上市個股日本益比、殖利率及股價淨值比（依代碼查詢） | `/exchangeReport/BWIBBU_ALL` | `/market/valuations-by-code` |
| prices | 上市個股日本益比、殖利率及股價淨值比（依日期查詢） | `/exchangeReport/BWIBBU_d` | `/market/valuations-by-date` |
| indices | 每日收盤行情－大盤統計資訊 | `/exchangeReport/MI_INDEX` | `/market/market-indices` |
| indices | 每日上市上櫃跨市場成交資訊 | `/exchangeReport/MI_INDEX4` | `/market/cross-market` |
| indices | 寶島股價指數歷史資料 | `/indicesReport/FRMSA` | `/market/formosa-index` |
| indices | 臺灣 50 指數歷史資料 | `/indicesReport/TAI50I` | `/market/taiwan50-index` |
| indices | 發行量加權股價指數歷史資料 | `/indicesReport/MI_5MINS_HIST` | `/market/taiex-history` |
| indices | 發行量加權股價報酬指數 | `/indicesReport/MFI94U` | `/market/taiex-return` |
| foreign | 集中市場外資及陸資投資類股持股比率表 | `/fund/MI_QFIIS_cat` | `/market/foreign-industries` |
| foreign | 集中市場外資及陸資持股前 20 名彙總表 | `/fund/MI_QFIIS_sort_20` | `/market/foreign-top20` |
| market | 每 5 秒委託成交統計 | `/exchangeReport/MI_5MINS` | `/market/five-second-statistics` |
| market | 集中市場每日市場成交資訊 | `/exchangeReport/FMTQIK` | `/market/market-trading` |
| market | 集中市場每日成交量前二十名證券 | `/exchangeReport/MI_INDEX20` | `/market/volume-top20` |
| prices | 集中市場零股交易行情單 | `/exchangeReport/TWT53U` | `/market/odd-lot` |
| prices | 集中市場盤後定價交易 | `/exchangeReport/BFT41U` | `/market/after-hours` |
| market | 每日第一上市外國股票成交量值 | `/exchangeReport/STOCK_FIRST` | `/market/foreign-listed-trading` |
| prices | 上市個股股價升降幅度 | `/exchangeReport/TWT84U` | `/market/price-ranges` |
| market | 集中市場漲跌證券數統計表 | `/opendata/twtazu_od` | `/market/advances-declines` |
| market | 電子式交易統計資訊 | `/opendata/t187ap19` | `/market/electronic-trading` |
| margin | 集中市場融資融券餘額 | `/exchangeReport/MI_MARGN` | `/market/margin-balances` |
| margin | 集中市場停資停券預告表 | `/exchangeReport/BFI84U` | `/market/margin-suspensions` |
| margin | 上市上櫃股票當日可借券賣出股數 | `/SBL/TWT96U` | `/market/lending-limits` |
| day-trading | 上市股票每日當日沖銷交易標的及統計 | `/exchangeReport/TWTB4U` | `/market/day-trading` |
| day-trading | 集中市場暫停先賣後買當日沖銷交易標的預告表 | `/exchangeReport/TWTBAU1` | `/market/day-trading-suspensions` |
| day-trading | 集中市場暫停先賣後買當日沖銷交易歷史查詢 | `/exchangeReport/TWTBAU2` | `/market/day-trading-suspension-history` |
| block | 集中市場鉅額交易日成交量值統計 | `/block/BFIAUU_d` | `/market/block-daily` |
| block | 集中市場鉅額交易月成交量值統計 | `/block/BFIAUU_m` | `/market/block-monthly` |
| block | 集中市場鉅額交易年成交量值統計 | `/block/BFIAUU_y` | `/market/block-yearly` |
| alerts | 上市個股首五日無漲跌幅 | `/exchangeReport/TWT88U` | `/market/no-price-limit` |
| alerts | 集中市場暫停交易證券 | `/exchangeReport/TWTAWU` | `/market/suspended-securities` |
| alerts | 集中市場證券變更交易 | `/exchangeReport/TWT85U` | `/market/changed-trading` |
| alerts | 投資理財節目異常推介個股 | `/Announcement/BFZFZU_T` | `/market/abnormal-recommendations` |
| alerts | 集中市場公布注意累計次數可能達處置標準資訊 | `/announcement/notetrans` | `/market/attention-threshold` |
| alerts | 集中市場當日公布注意股票 | `/announcement/notice` | `/market/attention-securities` |
| alerts | 集中市場公布處置股票 | `/announcement/punish` | `/market/disposition-securities` |
| calendar | 上市股票除權除息預告表 | `/exchangeReport/TWT48U_ALL` | `/market/ex-dividend` |
| calendar | 有價證券集中交易市場開（休）市日期 | `/holidaySchedule/holidaySchedule` | `/market/market-calendar` |
| warrants | 上市認購（售）權證每日成交資料檔 | `/opendata/t187ap42_L` | `/market/warrant-trading` |
| warrants | 上市認購（售）權證交易人數檔 | `/opendata/t187ap43_L` | `/market/warrant-traders` |
| etf | 定期定額交易戶數統計排行月報表 | `/ETFReport/ETFRank` | `/market/etf-recurring-ranking` |
