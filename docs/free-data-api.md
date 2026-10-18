# 免費官方資料與行情 API

本次在既有 FastAPI 分層增加官方資料集服務。`api/v1/endpoints` 負責 HTTP，`services` 處理查詢與匯入，`services/providers` 處理上游 JSON／CSV，`schemas` 定義 Pydantic input/output BaseModel，`api/v1/router.py` 統一掛載。沒有加入本網站的 auth。

## 可用範圍

已建立 TWSE、TPEx、TDCC、TAIFEX 共 57 個來源資源的 adapter／匯入設定，以及以下查詢端點。來源目錄設定不代表所有來源已成功連線；必須經實際匯入後才會顯示 `available`。部署環境仍需允許官方主機。

- TWSE：公司主檔、日價量、估值、月營收、股利／除權息、重訊、董監持股／設質／轉讓、融資券、借券額度、當沖、盤後定價、零股、鉅額統計、外資前二十名／產業統計、開休市。
- TWSE／TPEx：一般業、銀行、證券、金控、保險及異業的損益表與資產負債表，共 24 個來源。保留產業原生科目。
- TPEx：主檔、日價量、估值、營收、三大法人、融資券。匯入前先讀官方 Swagger，要求它仍公布相應路徑；不存在就回報 `SOURCE_CONTRACT_CHANGED`，不轉往猜測網址。
- TDCC：最新一期股權分散表 CSV，週頻。
- TAIFEX：期貨／選擇權日行情、三大法人、大額交易人、Put/Call 比。保留契約月份、交易時段與法人子分類原欄位。
- Fugle：需自行設定免費方案 key 的 8 個 REST 端點；不含付費市場快照、技術指標與盤後籌碼 API。

這一層提供**資料來源快照與原生欄位**，不是已完成科目統一、完整歷史回補、PIT 回測或跨來源財務指標計算的資料庫。原本 UUID 股票搜尋／盤後報價仍使用既有 `market_import` 與 TWSE 契約，新增目錄不會暗中改變該契約。

## 啟動與匯入

```bash
uv sync --python 3.12
uv run alembic upgrade head

# 不連網、不讀資料庫的來源清單
uv run python -m app.services.open_data_import --list

# 匯入指定來源，可重複 --dataset
uv run python -m app.services.open_data_import \
  --dataset twse_revenue --dataset tdcc_distribution

# 所有官方來源依序匯入，請先確認連線與來源契約
uv run python -m app.services.open_data_import --all
```

匯入每個來源時先完整讀取／驗證，再用短交易發布到 `market.open_data_snapshots` 與 `market.open_data_records`。同來源以 advisory lock 序列化；任何寫入失敗回滾，不會留下半份資料。較舊的已知資料日期或抓取時間不能覆蓋較新快照。每個來源獨立提交，其他來源失敗不抹除已成功的資料；只要有一項失敗，CLI 最後 exit code 為 1。

預設不自動匯入、不在 FastAPI 啟動時下載全市場。沒有排程承諾；可由既有外部排程執行上述 CLI。新版 migration 為 `0003_open_data`。

## 查詢端點

所有路徑加上 `/api/v1`。分類端點都要求 `dataset`，可以從 `/data-sources` 選擇；`dataset` 與分類不符回 422。這能明確指定市場、產業與實際來源，避免自動混合不同口徑。

| 路徑 | dataset 範例 |
| --- | --- |
| `GET /data-sources` | 可用 `provider=twse`、`category=monthly-revenue` 篩選 |
| `GET /data-sources/{dataset}/records` | 所有已登錄來源的統一查詢 |
| `GET /fundamentals/companies` | `twse_companies`、`tpex_companies` |
| `GET /fundamentals/monthly-revenue` | `twse_revenue`、`tpex_revenue` |
| `GET /fundamentals/financial-statements` | `twse_income_ci`、`tpex_balance_basi` |
| `GET /fundamentals/valuations` | `twse_valuations`、`tpex_valuations` |
| `GET /fundamentals/dividends` | `twse_dividends` |
| `GET /fundamentals/corporate-actions` | `twse_ex_dividends` |
| `GET /fundamentals/announcements` | `twse_announcements` |
| `GET /ownership/institutional-trades` | `tpex_institutional` |
| `GET /ownership/foreign-holdings` | `twse_foreign_top20`、`twse_foreign_industries` |
| `GET /ownership/margin-balances` | `twse_margin`、`tpex_margin` |
| `GET /ownership/lending-limits` | `twse_lending_limits` |
| `GET /ownership/holding-distributions` | `tdcc_distribution` |
| `GET /ownership/insider-holdings` | `twse_insiders` |
| `GET /ownership/insider-pledges` | `twse_pledges` |
| `GET /ownership/insider-transfers` | `twse_transfers` |
| `GET /market-data/daily-prices` | `twse_prices`、`tpex_prices` |
| `GET /market-data/day-trading` | `twse_day_trading` |
| `GET /market-data/after-hours` | `twse_after_hours` |
| `GET /market-data/odd-lot` | `twse_odd_lot` |
| `GET /market-data/block-trading` | `twse_block_trading` |
| `GET /market-data/calendar` | `twse_calendar` |
| `GET /derivatives/daily-prices` | `taifex_futures_prices`、`taifex_options_prices` |
| `GET /derivatives/institutional-trades` | `taifex_futures_institutional`、`taifex_options_institutional` |
| `GET /derivatives/large-traders` | `taifex_futures_large_traders`、`taifex_options_large_traders` |
| `GET /derivatives/put-call-ratio` | `taifex_put_call_ratio` |

```bash
curl 'http://localhost:8000/api/v1/data-sources?provider=twse'
curl 'http://localhost:8000/api/v1/fundamentals/monthly-revenue?dataset=twse_revenue&symbol=2330'
curl 'http://localhost:8000/api/v1/ownership/holding-distributions?dataset=tdcc_distribution&symbol=0050'
curl 'http://localhost:8000/api/v1/derivatives/daily-prices?dataset=taifex_futures_prices&symbol=TX'
```

共用 query：`symbol`、`date_from`、`date_to`、`page`（預設 1）、`limit`（預設 50，上限 200）。日期反向、超限、未知參數均回 422。不接受來源 URL、SQL、任意供應商路徑或臨時放寬配額參數。

`data` 每筆為 `SourceRecord`：

- `symbol`：來源的證券代碼或契約識別，保留前置零；期貨法人表可能使用中文契約名稱，不能保證都用 `TX` 之類商品代碼。
- `record_date` 與 `record_date_field`：從來源實際日期欄正規化，並明列採用哪一欄；無完整年月日時為 null。只給年度或資料年月時，不虛構每月第一天。
- `fields`：原始欄位名稱及精確字串／null，JSON 數字也保留成字串，避免大數或小數被浮點截斷。`--` 保留原義，不轉成零；股／張、元／千元、比例值不在這一層混算。

`pagination` 含符合條件的總筆數；`meta` 含來源、網址、授權、署名、頻率、涵蓋範圍、抓取時間、版本與來源日期。`freshness=unknown` 不表示即時或當日已更新。`date_from/to` 篩的是上述 `record_date`，財報的出表日期不等於季度期間，營收期間看 `fields` 中的資料年月。

查詢只涵蓋目前保存的來源快照，日期 filter 不會觸發上游歷史回補。外資前二十名只有排行範圍；借券額度不是借券賣出餘額；市場／產業彙總沒有個股代號時，`symbol` filter 明確回 422，不能把空陣列當成該股零餘額。

## Fugle 免費 REST 行情

在後端 `.env` 設定 `FUGLE_API_KEY`，不要放進前端的 `VITE_*` 或 URL。免費方案文件：[方案](https://developer.fugle.tw/docs/pricing/)、[REST](https://developer.fugle.tw/docs/data/http-api/getting-started/)。本程式不代為註冊或購買。

| 路徑 | 說明 |
| --- | --- |
| `GET /api/v1/realtime/tickers` | 股票／指數列表，可篩選市場、產業與注意／處置狀態 |
| `GET /api/v1/realtime/{symbol}/quote` | 報價與五檔；保留 `lastTrial`／`lastTrade` 區別 |
| `GET /api/v1/realtime/{symbol}/ticker` | 個股基本交易資訊 |
| `GET /api/v1/realtime/{symbol}/trades` | 成交查詢 |
| `GET /api/v1/realtime/{symbol}/candles` | 當日 K 線 |
| `GET /api/v1/realtime/{symbol}/volumes` | 分價量 |
| `GET /api/v1/realtime/{symbol}/history` | 歷史 K 線 |
| `GET /api/v1/realtime/{symbol}/stats` | 歷史統計 |

Quote／trades／candles／volumes 可傳 `type=oddlot`。當日 K 線支援 `timeframe` 與 `sort`；成交明細支援 `offset`、`limit`（本 API 上限 500）、`sort`、`isTrial`。Tickers 支援 `type`、`exchange`、`market`、`industry` 與文件中的狀態旗標。歷史 K 線要求 `date_from` 與 `date_to`，區間小於一年；`timeframe` 支援文件列出的分 K 與 D/W/M，`adjusted=true` 只接受 D/W/M。

```bash
curl 'http://localhost:8000/api/v1/realtime/2330/quote'
curl 'http://localhost:8000/api/v1/realtime/2330/history?date_from=2026-01-01&date_to=2026-10-16&timeframe=D'
```

回應 `data` 保留原生欄位與精確數值字串，`meta` 帶實際抓取時間與試撮旗標。REST 報價是請求時的快照，這版沒有 WebSocket／SSE 推播，也不保證重建每筆中間成交。抓取時間不當成交易時間；普通交易、零股與盤後資料依來源分開。

共用 client/cache 在 lifespan 建立，單一 FastAPI 程序共享 60 次／分鐘預算；日內快取 5 秒、歷史快取 300 秒，最多 256 項，並以原始回應大小限制總額 32 MiB。遇 429 暫停呼叫 60 秒，不重試放大上游負載。多程序部署前需改為集中行情 worker 或共享配額，不可假設每個程序各有免費額度。

未設定 key 回 `SOURCE_NOT_CONFIGURED`。正式環境還需取得對應的公開展示授權，確認後才可設 `FUGLE_DISPLAY_AUTHORIZED=true`；個人免費方案本身不能推導出公開展示權。這是來源設定檢查，沒有加入網站會員登入驗證。

## 刻意未接入的來源／能力

| 項目 | 原因與處理方式 |
| --- | --- |
| FinMind Free 原始資料鏡像 | 免費可取不等於可再散布；現行條款限制商用與原始資料轉提供，因此未透過本 API 重建其鏡像 |
| FinMind 即時行情 | 現行條款禁止直接呈現在對外 Web／App |
| 永豐／富邦券商 API | 需個人開戶、約定、憑證／key，並非免帳號公開資料源；不使用使用者交易憑證 |
| TWSE T86 網站路由、分點 | 官網可查與政府開放 API 的授權不同；未爬驗證碼、未購買或轉售資料 |
| 完整外資持股與借券賣出餘額 | 目前可用的開放來源只接上列明示範圍，不把排行或借券額度冒充完整餘額 |
| 現金流、財報附註、歷史 XBRL | 未查得已確認的通用免費 REST 契約；不編造來源或拿損益表冒充現金流 |
| SITCA／投信基金持股、勞動基金文件 | 可公開查詢，但缺乏本次已確認的統一免費開放 API 契約；未把網站抓取當成已驗證 API |
| 全市場即時快照、付費籌碼 API | 不屬 Fugle 免費方案，不自動升級 |

來源使用條件見 [TWSE](https://openapi.twse.com.tw/)、[TPEx](https://www.tpex.org.tw/openapi/)、[TDCC](https://data.gov.tw/dataset/11452)、[TAIFEX](https://openapi.taifex.com.tw/)、[政府開放授權](https://data.gov.tw/license)、[FinMind 條款](https://finmind.github.io/PrivacyPolicy/)。按每個資料集保留來源與署名，並不一概把網站所有內容宣稱開放授權。

## 驗證

```bash
uv run ruff check app tests alembic
uv run mypy
uv run pytest -q
uv build
```

一般測試用明示的合成契約樣本，沒有正式假資料 fallback。設定專用測試 PostgreSQL 並遷移後，以 `RUN_DB_TESTS=1 uv run pytest -q` 驗證寫入、版本、篩選及回滾；Nginx 測試另設定 `GATEWAY_BASE_URL`。

真實來源測試必須在網域白名單生效後執行。受管環境需允許 `openapi.twse.com.tw`、`www.tpex.org.tw`、`opendata.tdcc.com.tw`、`openapi.taifex.com.tw`；行情另需 `api.fugle.tw` 及自行設定的 key。代理 CONNECT 的 403 不等於交易所封鎖所有雲端主機。

2026-10-18 的真實 adapter 連線檢查：TWSE 營收、TPEx 法人、TDCC 分散表、TAIFEX 日行情皆在代理 CONNECT 階段收到 403，未到達來源服務，因此尚未完成這些來源的實際 payload 驗收。Fugle 尚未設定個人 key。此限制不以合成資料冒充成功；PR 保持草稿，待環境設定套用後繼續驗證全部來源與欄位。
