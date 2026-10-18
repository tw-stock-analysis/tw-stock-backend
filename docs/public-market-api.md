# 公開行情 API：第一批端點

## 分層

```text
app/
├── api/
│   ├── deps.py                 # Session 與 service dependencies
│   ├── exception_handlers.py   # 統一 HTTP 錯誤輸出
│   └── v1/
│       ├── router.py           # /api/v1 統一掛載
│       └── endpoints/          # health、instruments、quotes、market
├── services/                   # 查詢、資料匯入、官方來源 adapter
├── schemas/                    # Pydantic BaseModel 輸入與輸出
├── models/                     # SQLAlchemy 資料表
└── db/                         # Base 與 session 生命週期
```

Endpoint 只負責 HTTP 參數與呼叫 service；service 處理查詢及業務規則；schema 定義前端可見的資料契約。這一版不含 auth、帳號或私人自選股端點。

## 端點與回應

| 方法與路徑 | 功能 |
| --- | --- |
| `GET /api/v1/instruments` | `q` 代碼／名稱搜尋、`market`、`page`、`limit` |
| `GET /api/v1/instruments/{instrument_id}/overview` | 股票基本資料與最近一筆已匯入的盤後報價 |
| `GET /api/v1/quotes?ids=UUID&ids=UUID` | 最多 100 個股票 ID 的批次報價，保留請求順序、去除重複 ID |
| `GET /api/v1/market/status` | 資料集是否已匯入、資料日期、來源、最後成功匯入時間與版本 |

目前支援 TWSE 上市公司主檔所對應的股票，不含 ETF、權證與標示為 `-DR` 的存託憑證。`market=TPEX` 明確回 422 `MARKET_NOT_SUPPORTED`，不回空清單假裝支援。股票 ID 使用資料庫 UUID；重複匯入與公司名稱更正不改變 ID。代碼變更／轉板的身分銜接仍需後續企業行動及掛牌歷史功能。

價格、漲跌、金額及成交股數以 Decimal 字串輸出，價格為未還原新臺幣元、量為股。來源缺值保留 `null` 並提供 `missing_fields`／`missing_reasons`，不補零；試算或盤中價格不混入盤後報價。每筆報價獨立帶交易日期與來源版本，主檔日期可能不同。沒有已驗證交易日曆前，`freshness` 固定為 `unknown`。

```bash
curl 'http://localhost:8000/api/v1/instruments?q=2330'
curl 'http://localhost:8000/api/v1/market/status'
# 將搜尋結果的 instrumentId 帶入，不使用股票代碼當 UUID：
curl 'http://localhost:8000/api/v1/instruments/<instrumentId>/overview'
curl 'http://localhost:8000/api/v1/quotes?ids=<instrumentId>'
```

Nginx 入口使用同樣路徑，將連接埠改為 8080。開發環境的 `/api/v1/docs` 可直接查看完整輸入與輸出 BaseModel。

## 官方資料匯入

先執行 migration，再明確執行匯入工作：

```bash
uv run alembic upgrade head
uv run python -m app.services.market_import
```

Compose 環境則使用：

```bash
docker compose exec backend python -m app.services.market_import
```

來源為 [TWSE 上市公司基本資料](https://openapi.twse.com.tw/v1/opendata/t187ap03_L) 與 [上市個股日成交資訊](https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL)，依 [政府資料開放授權](https://data.gov.tw/license) 保留來源。使用來源最新可取得的快照；此介面沒有任意日期參數，不宣稱提供完整歷史回補。每次執行保存該日價格，重跑更新同日資料，不重複新增。

匯入先抓取與檢查資料，再以單一交易發布主檔、日價量及資料集狀態；失敗回滾、舊日期不覆蓋新快照。保存各資料列的原始欄位、資料版本與取得時間；同日修訂目前保留最新版，不提供完整修訂歷史。HTTP 查詢使用一致的資料庫快照，不在請求期間抓取交易所資料。

新資料庫尚未匯入時，`/market/status` 顯示 `not_loaded`，行情查詢回 503 `MARKET_DATA_UNAVAILABLE`。已知股票沒有報價回 `no_data`，批次未知 ID 回 `not_found`，個股摘要未知 ID 回 404。上游中斷不會產生假報價，也不會清空已發布資料。

此輪雲端連通檢查被環境的對外代理以 403 阻擋；需在雲端環境網路設定允許 `openapi.twse.com.tw:443` 後重新執行真實來源匯入。測試 fixtures 為明確的合成資料，只在測試使用，不會由正式 API 自動載入。上櫃來源、盤中即時行情、歷史回補、財報與圖表端點尚未納入這次交付。

## 測試

```bash
uv run pytest -q
# 指向專用且已 migrate 的測試 PostgreSQL，包含真實資料庫整合測試：
RUN_DB_TESTS=1 uv run pytest -q
uv run alembic check
```

來源 parser、HTTP 失敗及 API 契約以合成 fixtures 測試；資料庫整合使用真實 PostgreSQL 的 UUID、JSONB、Decimal、upsert 與交易回滾。測試寫入會 rollback，不會替正式環境預載示範資料。
