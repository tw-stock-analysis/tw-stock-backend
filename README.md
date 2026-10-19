# 台股分析網站後端

使用 Python 3.12、FastAPI 與 uv。

## 安裝

在 backend 資料夾執行：

```bash
uv sync --python 3.12
test -f .env || cp .env.example .env
```

依需求填寫 `.env` 設定。

## 啟動

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

若終端機已啟用 `.venv`，直接執行第二行即可。

API：http://localhost:8000

API 文件：http://localhost:8000/api/v1/docs

## TWSE 交易資料 API

每次查詢皆由 FastAPI 直接呼叫 [TWSE OpenAPI](https://openapi.twse.com.tw/)，不寫入或讀取市場資料庫、不提供行情快取或舊資料回退。提供 44 個對應端點，涵蓋價格、成交、指數、外資持股、資券、當沖、鉅額、注意／處置、日曆、權證交易與 ETF 定期定額統計。不含公司基本資料、營收、財報、其他供應商及 auth。

使用 `app/api/v1/endpoints` → `app/services` 的分層，`v1/router.py` 掛載路由，輸入／輸出 BaseModel 位於 `app/schemas`。前端從 `/api/v1/twse/datasets` 取得功能目錄，再呼叫各 API 專屬端點：

```bash
curl 'http://localhost:8000/api/v1/twse/datasets'
curl 'http://localhost:8000/api/v1/twse/exchangeReport/STOCK_DAY_ALL'
```

每次呼叫取得「來源最新發布資料」，不代表所有資料都在盤中更新。資料欄位、日期與單位保留官方格式，數值以字串傳回。來源失敗回 503，格式錯誤回 502。未知查詢參數回 422，不會假裝支援官方未提供的歷史日期或個股查詢。

完整清單、前端路徑、回應契約及驗證方式見 [TWSE 交易資料](docs/twse-trading-api.md)。2026-10-18 已逐一實測 44 個官方 API，全部成功且欄位與即時 Swagger 相符；[驗證紀錄](docs/twse-live-verification.json) 只保存筆數、欄位與取得時間，不保存行情資料。

## 個股搜尋與詳情

新增 4 個明確宣告的端點：`GET /api/v1/stocks`（q／page／limit）、`GET /api/v1/stocks/{code}`、`GET /api/v1/stocks/{code}/valuation`、`GET /api/v1/stocks/{code}/margin`。每次各自重新取得既有 TWSE 來源全集，在記憶體中搜尋或精確比對代碼，保留精確字串，不儲存或快取行情。連同原始端點、目錄及健康檢查，目前共 **51 個 API 路由**。

前端 `/stocks` 可搜尋選股，`/stocks/{code}` 依序取得成交、估值與資券，分別標示日期、取得時間及失敗狀態；來源未提供的資券日期不自行補上。完整契約與 154 項整合驗收見 [個股研究 API](docs/stock-research-api.md)。

## 帳號與自選股資料表（SQLAlchemy ORM）

已建立 `app.users`、`app.auth_sessions`、`app.verification_tokens` 三張帳號表的 SQLAlchemy 2 ORM 模型及 Alembic migration，支援 Email 唯一性、一對多裝置 Session 與驗證 Token 欄位。註冊／登入、Cookie 與寄信 API 尚未實作，其他 table 待後續討論。TWSE 行情維持逐次請求、不入庫、不快取。

另新增 `app.watchlists`、`app.watchlist_items`：每人最多 10 份清單、每份最多 100 檔，清單名稱不分大小寫唯一，股票不重複、排序於 commit 時唯一。已補上服務層的歸屬檢查、即時 TWSE 驗證、並發保護、軟刪除撤銷憑證與一次性 Token 消耗；API 與前端尚未串接。

軟刪除帳號會釋出 Email，重新註冊取得新 user_id，不繼承舊資料。清理指令預設刪除失效超過 30 天的 Session／驗證 Token，支援預覽：

```bash
uv run python -m app.services.auth_cleanup --dry-run
uv run python -m app.services.auth_cleanup
```

```bash
uv run alembic upgrade head
uv run alembic check
RUN_DB_TESTS=1 uv run pytest tests/test_database.py tests/test_watchlists.py tests/test_account_services.py -q
```

詳細內容見 [目前 ER 圖](docs/database-er.md)、[帳號資料表](docs/account-tables.md)、[自選清單資料表](docs/watchlist-tables.md) 及 [服務／遷移／清理排程](docs/account-services.md)。

## Nginx 整站入口（Docker Compose）

兩個儲存庫放在同一層，前端需使用包含 Nginx Dockerfile 的版本：

```text
workspace/
├── tw-stock-backend/
└── tw-stock-frontend/
```

```mermaid
flowchart LR
    Browser[瀏覽器] --> Nginx[Nginx :8080]
    Nginx --> Static[React 靜態檔案]
    Nginx -->|/api 與 /api/*，保留完整路徑| API[FastAPI :8000]
    API -->|每次交易查詢| TWSE[TWSE OpenAPI]
    API -->|既有健康檢查| DB[(PostgreSQL)]
```

在後端目錄準備 `.env`，將 `POSTGRES_PASSWORD` 設為自己的密碼，再執行：

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build --wait
```

啟動順序為 PostgreSQL 健康 → 獨立 migrate 程序執行 `alembic upgrade head` → FastAPI ready → Nginx。Migration 失敗會阻止後端及 Nginx 啟動。資料儲存在 `postgres_data` volume；一般停止使用 `docker compose down`，保留資料，不加 `-v`。

開啟 http://localhost:8080 即可使用前端。瀏覽器的 `/api/v1/health/live`、`/api/v1/health/ready` 由同一入口轉送至 FastAPI；`/stocks`、`/watchlists/demo` 等前端路徑可直接開啟或重新整理。不存在的 API 與靜態資產維持 404，不回傳前端 HTML。

只有 Nginx 對主機開放連接埠，預設綁定 `127.0.0.1:8080`。FastAPI 的 8000 與 PostgreSQL 的 5432 不映射至主機；前端不需要另設 CORS。Compose 會將資料庫主機覆寫為 `db`，並忽略本機用的 `DATABASE_URL`，使用這組 Compose 專屬資料庫。

可在 `.env` 調整：

| 變數 | 預設值 | 用途 |
| --- | --- | --- |
| `FRONTEND_CONTEXT` | `../tw-stock-frontend` | 前端儲存庫路徑 |
| `WEB_BIND_ADDRESS` | `127.0.0.1` | Nginx 對主機的綁定位址 |
| `WEB_PORT` | `8080` | 瀏覽器入口連接埠 |
| `APP_ENV` | 範例為 `development` | `production` 時停用 Swagger 與 OpenAPI 文件 |

目前提供 HTTP 部署入口；公開 HTTPS 部署需另接網域及憑證。Nginx 是此配置的最外層入口，會重設用戶端傳入的轉送標頭；若日後前面再加負載平衡器或 TLS 入口，需配合調整可信代理與協定標頭。FastAPI 僅在內部網路信任 Nginx 傳入的代理資訊。

### 驗證入口

```bash
curl --fail http://localhost:8080/api/v1/health/ready
GATEWAY_BASE_URL=http://127.0.0.1:8080 uv run pytest tests/test_gateway.py -q
docker compose logs web backend migrate
```

整合測試使用實際 Nginx 與 FastAPI，驗證 SPA 子路由、API 前綴、404、靜態資產快取、公開來源及 query string 的代理行為。未設定 `GATEWAY_BASE_URL` 時略過這組測試，一般單元測試不需要啟動 Docker。
