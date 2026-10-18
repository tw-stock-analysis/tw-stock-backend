# API 契約 v1

此文件定義目標行為；目前實作 health、TWSE 股票搜尋、個股摘要、批次盤後報價與市場資料狀態。端點放在 `app/api/v1/endpoints`，`v1/router.py` 統一掛載，輸入／輸出 BaseModel 位於 `app/schemas`，業務處理位於 `app/services`。Auth 與私人資源尚未實作。環境及已交付範圍見 [公開行情 API 說明](public-market-api.md)。前綴 /api/v1；使用 JSON、UTF-8。FastAPI OpenAPI 為目前實作的機器契約；TypeScript client 產生與 CI 契約漂移檢查仍待建置。

目前切片限制：只支援 TWSE，`market=TPEX` 回 422 `MARKET_NOT_SUPPORTED`；`quotes` 的 IDs 使用重複 query parameter（`?ids=UUID&ids=UUID`）。個股摘要先提供 `instrument`、`instrument_meta` 與 `quote`，不填入尚未支援的基本面。搜尋回傳 `instrumentId`、`ticker`、`market`、`name`、`company_name`、`industry_code`、`type`；掛牌／下市生命週期狀態留待後續。資料集／報價有來源、日期、取得時間與版本；未接交易日曆前 freshness 為 unknown。下列未完成項目為目標規格，不能視為已提供。

## 1. 共通規則

- 成功回應通常有 data；分頁有 pagination；市場資料有 meta。204 無 response body。
- 日期 YYYY-MM-DD；時間 RFC3339 UTC；價格、財務小數、成交股數以字串傳送；ID 為字串 UUID。
- 數值物件包含 value、unit、status；null 值有 reason_code，必要時附計算口徑。精確數值不以格式化的「億元」字串取代原值。
- 私人 owner 由 session 決定。所有 mutation 驗證 CSRF、Origin 及 Content-Type；auth/me/watchlists/settings 回應 no-store。
- 市場公共端點不含「這是我的自選」等私人欄位，因此可安全使用公開快取。

| 錯誤欄位 | 用途 |
| --- | --- |
| error.code | 固定英文代碼供前端判斷 |
| error.message | 使用者可讀訊息 |
| error.field_errors | 欄位位置與錯誤，可空 |
| error.request_id | 支援問題追蹤，不含內部 traceback |

| HTTP | 典型 code／行為 |
| --- | --- |
| 401 | UNAUTHENTICATED／INVALID_CREDENTIALS；登入失效 |
| 403 | CSRF_INVALID／EMAIL_NOT_VERIFIED；不可盲目重試 |
| 404 | RESOURCE_NOT_FOUND；不存在或屬別人的私人資源 |
| 409 | VERSION_CONFLICT／NAME_CONFLICT／LIMIT_REACHED |
| 422 | VALIDATION_ERROR／RANGE_NOT_SUPPORTED／PARAMETER_NOT_SUPPORTED |
| 429 | RATE_LIMITED；附 Retry-After |
| 503 | MARKET_DATA_UNAVAILABLE／DEPENDENCY_UNAVAILABLE；完全無可用資料 |
| 500 | INTERNAL_ERROR；不洩露 SQL 或服務金鑰 |

有有效舊資料時回 200 並標明 stale；合法「沒有財報」回 200 空資料及原因；股票不存在才回 404。500、503 不被 UI 解讀為零資料。

## 2. 認證與會員

| 方法／路徑 | 輸入摘要 | 結果 |
| --- | --- | --- |
| GET /auth/csrf | Cookie（如有） | 200 csrf_token；必要時建立匿名 session |
| POST /auth/register | email、password、display_name | 一致 202；建立待驗證帳號／安排適當信件 |
| POST /auth/login | email、password | 200 公開 user 與新 csrf_token；Set-Cookie |
| POST /auth/logout | CSRF | 撤銷目前 session、清 Cookie；204 |
| POST /auth/email-verification/request | email | 一致 202 |
| POST /auth/email-verification/confirm | token | 200 驗證成功；不直接登入 |
| POST /auth/password-reset/request | email | 一致 202 |
| POST /auth/password-reset/confirm | token、new_password | 204；撤銷舊 sessions |
| GET /me | 已登入 Cookie | 200 id、display_name、email、verified；未登入 401 |
| PATCH /me | display_name | 200 更新後公開 user |
| GET /me/settings | — | 200 設定及 version |
| PATCH /me/settings | 允許欄位、expected_version | 200 新設定／version；舊版 409 |
| POST /me/sessions/revoke-others | current_password | 200 新 csrf_token、Set-Cookie；撤銷其他 sessions 並輪換目前 session |

所有成功認證回應都不在 JSON 內包含 session token 本身。登入對未驗證信箱只在密碼正確時回 EMAIL_NOT_VERIFIED，其餘錯誤一致。

註冊／重寄信不自動登入，登入成功後前端重新載入私人資料。auth token 有效期與限制見後端文件。新增密碼長度上限及輸入格式時要同步 OpenAPI 與 UI，不能只在前端擋。

## 3. 自選清單

| 方法／路徑 | 輸入 | 回應與競態 |
| --- | --- | --- |
| GET /watchlists | — | 本人清單摘要，含 id/name/item_count/version |
| POST /watchlists | name | 201；同人同名衝突 409 |
| GET /watchlists/{id} | — | 清單／version／有序 instruments；公開報價由批次端點補 |
| PATCH /watchlists/{id} | name、expected_version | 200 新 version；衝突 409 |
| DELETE /watchlists/{id}?expected_version= | version | 204；僅本人且版本一致 |
| PUT /watchlists/{id}/items/{instrumentId} | 空 JSON | 200 清單 version；已存在為成功且不增加 version |
| DELETE /watchlists/{id}/items/{instrumentId} | — | 200 清單 version；已不存在為成功且不變版 |
| PUT /watchlists/{id}/item-order | ordered_ids、expected_version | 200 新 version；需完整且不重複 ID 集合 |

PUT／DELETE 單項可安全重試；每次實際變更 version+1。第一版每人最多 10 個清單、每清單 100 檔。列表不存在／非本人一律 404；標的不存在回 422 並指出欄位。下市且仍有主檔的標的可保留既有自選，禁止新增是否有必要留到產品使用資料後再決定，預設允許加入並顯示狀態。

## 4. 公開股票與分析

| 方法／路徑 | 參數 | 回應重點 |
| --- | --- | --- |
| GET /instruments | q、market、page、limit | instrumentId、名稱、ticker、market、type、status；代碼精確優先、前綴次之、名稱包含最後 |
| GET /instruments/{id}/overview | — | 公司、現行掛牌、最新日價格、官方變動基準、基本面摘要及各自 meta |
| GET /quotes | ids（最多100） | 一次取得自選清單所需摘要；每檔獨立 status／交易日 |
| GET /instruments/{id}/chart-data | from、to、interval=1d、price_basis=raw、indicators | 同一版本 OHLCV、指標序列與計算 metadata |
| GET /instruments/{id}/revenues | from_month、to_month | 每月營收、YoY／MoM、公告與版本 |
| GET /instruments/{id}/financials | periods（最多12）、scope=consolidated | 報表期間、口徑、指標、EPS period_basis、適用性 |
| GET /instruments/{id}/valuations | from、to | 官方 PE／PB／殖利率，method、price_date 與缺值原因 |
| GET /market/status | — | 各資料集最新日期、最後成功批次與狀態，不含內部錯誤 |

搜尋預設 limit=20、上限50，page 正整數、q 最多80字；參數白名單，不接受任意 SQL 排序欄位。空 q 可回前頁主檔，市場只允許 TWSE/TPEX。

chart-data 預設近一年，起訖包含兩端，最多兩年、1,500 根，from>to 或超限回 422。MVP 僅接受 raw/1d；不支援的復權或週期不能默默降級。指標只接受定義過的 preset：MA 5/20/60/120、RSI14、MACD12-26-9、BOLL20-2，最多八條設定；後續才開放有界自訂參數。

日 K 包含 trade_date、open/high/low/close、volume_shares、turnover、trade_status。價格變動依官方 change_basis 呈現；原始收盤與昨日收盤差另命名 close_to_close_change，除權息時不能混用官方參考價漲跌與前收盤差。

收益／財報列表的上限與資料實際覆蓋分開。請求合法但來源只有部分歷史時回 available_from/to、is_partial 與 warnings；不虛構完整覆蓋，也不以全部 0 補齊。此版不接受歷史 as_of 回測參數。

## 5. 市場 metadata

| 欄位 | 意義 |
| --- | --- |
| source、source_url、attribution | 可追溯來源及顯示署名 |
| data_as_of | 資料代表的日期／時間，不是 HTTP 請求時間 |
| retrieved_at | 系統取得來源資料的時間 |
| published_at、first_seen_at、available_at | 原始公開、首次觀測、保守可用時間；可按資料列提供 |
| expected_trade_date | 依交易日曆及資料集規則預期的最新日期 |
| freshness | current／pending_update／stale／unknown |
| data_version | 已發布批次與來源修訂組合識別 |
| coverage | requested 與 available 範圍、is_partial |
| price_basis、currency、unit | 未還原／幣別／股或元等單位 |
| calculation | 指標參數、algorithm_version、seed_date、input_version |
| warnings | 非致命品質或暖機說明 |

同一頁可有不同資料集版本；同一張 K 線及指標必須同版本。官方估值與自算比率以 method 分開。技術指標前段 null 的 reason 為 INSUFFICIENT_HISTORY；一般財務缺值區分 NOT_PUBLISHED、NOT_APPLICABLE、NON_POSITIVE_BASE、SOURCE_MISSING、INCOMPARABLE_PERIODS。

## 6. 健康與文件

GET /health/live 只檢查程序，GET /health/ready 檢查 DB 等必要依賴；行情上游短暫失敗不讓會員服務整體掉出 readiness。回應不暴露連線字串。開發／測試環境提供 OpenAPI 文件，正式環境是否限制文件存取依部署策略實作。
