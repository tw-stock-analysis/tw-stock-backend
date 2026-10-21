# 本機 Docker 開發

此設定管理三個獨立容器：Vite、FastAPI、PostgreSQL。前後端可直接編輯原始碼並熱更新；另有執行完即結束的 migration 容器。

## 準備與啟動

安裝 Docker Engine 與 Compose v2，或 Docker Desktop，並啟動 Docker。兩個儲存庫須使用包含 Dockerfile.dev 的版本，放在同一層：

```text
workspace/
├── tw-stock-backend/
└── tw-stock-frontend/
```

在後端目錄執行：

```bash
python scripts/init_dev_env.py
docker compose --env-file .env.development -f docker-compose.dev.yml config --quiet
docker compose --env-file .env.development -f docker-compose.dev.yml build backend
docker compose --env-file .env.development -f docker-compose.dev.yml build frontend
docker compose --env-file .env.development -f docker-compose.dev.yml up -d --wait
```

初始化腳本使用 Python 標準函式庫，產生隨機資料庫密碼，既有設定不會覆寫；檔案不進 Git。不需要在主機安裝 uv、Node 或 PostgreSQL。沒有 Python 時，可複製 .env.development.example 為 .env.development，再自行替換 POSTGRES_PASSWORD。

啟動依序等待資料庫健康、alembic upgrade head 成功、後端 ready，最後啟動前端。Migration 失敗時後端不會啟動，先查看 migrate 日誌。

| 用途 | 位置 |
| --- | --- |
| 網站 | http://localhost:3007 |
| Swagger | http://localhost:3007/api/v1/docs |
| Readiness | http://localhost:3007/api/v1/health/ready |
| 主機上的資料庫工具 | 127.0.0.1:5433 |
| 容器內的 PostgreSQL | db:5432 |
| 容器內的 FastAPI | backend:8000 |

瀏覽器只連到 Vite；/api/* 代理至 FastAPI。Cookie 與 CSRF 都以同一個 3007 入口運作，不需要 CORS。後端不映射主機連接埠。資料庫工具使用 .env.development 的 POSTGRES_USER、POSTGRES_PASSWORD、POSTGRES_DB；資料庫連接埠只綁定本機 loopback。

登入要求 active 且已驗證信箱的帳號。公開註冊尚未開放，先依 [認證文件](authentication-api.md#本機建立可登入的測試帳號) 將 Python 程式存成 /tmp/provision_local_user.py，再複製到開發容器執行，才能寫入這套開發資料庫：

```bash
docker compose --env-file .env.development -f docker-compose.dev.yml cp /tmp/provision_local_user.py backend:/tmp/provision_local_user.py
docker compose --env-file .env.development -f docker-compose.dev.yml exec backend python /tmp/provision_local_user.py
```

## 日常操作與資料保存

啟動完成後，在主機編輯前後端原始碼即可；Vite 更新頁面，Uvicorn 重新載入後端。

```bash
docker compose --env-file .env.development -f docker-compose.dev.yml ps
docker compose --env-file .env.development -f docker-compose.dev.yml logs --tail=100 frontend backend migrate db
docker compose --env-file .env.development -f docker-compose.dev.yml down
docker compose --env-file .env.development -f docker-compose.dev.yml up -d --wait
```

一般停止使用 down，資料保留在 tw-stock-dev_dev_postgres_data；不要加 -v，因為會刪除資料卷。修改資料庫密碼不會變更既有 PostgreSQL 使用者的密碼，初始化腳本也不會覆寫設定。

| 設定 | 預設值 | 用途 |
| --- | --- | --- |
| FRONTEND_CONTEXT | ../tw-stock-frontend | 前端儲存庫路徑 |
| DEV_WEB_PORT | 3007 | 本機網站入口，CSRF origins 同步使用此值 |
| DEV_DB_PORT | 5433 | 本機資料庫入口，避開常見的原生 PostgreSQL 5432 |
| DEV_USE_POLLING | false | Docker Desktop/WSL 偵測不到檔案變更時改為 true |

調整設定後重新執行 up -d --wait。Polling 每 500ms 檢查一次，會增加 CPU 用量；平常維持 false。前端容器使用 Compose 提供的代理目標，不讀主機前端 .env。

## 依賴與 migration

node_modules 與 Python .venv 留在 Linux 映像內，不掛載主機的相依套件。新增套件或更新 lockfile 後，重新建置對應映像，再執行 up -d --wait；前後端分開建置可降低 8GB 電腦的尖峰負載。

開發映像包含測試工具：

```bash
docker compose --env-file .env.development -f docker-compose.dev.yml exec frontend npm test -- --maxWorkers=1
docker compose --env-file .env.development -f docker-compose.dev.yml exec frontend npm run build
docker compose --env-file .env.development -f docker-compose.dev.yml exec backend pytest -q
```

上述後端指令預設略過資料庫整合測試。若要設定 RUN_DB_TESTS=1，需另準備空白測試資料庫；部分測試會清理憑證資料，不能使用已有個人資料的開發資料庫。

ORM 變更後，可產生並檢查 migration：

```bash
docker compose --env-file .env.development -f docker-compose.dev.yml exec backend alembic revision --autogenerate -m "describe change"
docker compose --env-file .env.development -f docker-compose.dev.yml run --rm migrate
docker compose --env-file .env.development -f docker-compose.dev.yml restart backend
docker compose --env-file .env.development -f docker-compose.dev.yml exec backend alembic check
```

先檢查產生的 migration，再套用；後端重啟會重新載入 migration head 的 readiness 檢查。alembic 目錄可寫回主機；Linux 若有檔案權限問題，需讓容器 UID 1000 可寫入此目錄。

## 資源與環境隔離

| 容器 | 記憶體上限 |
| --- | --- |
| Vite | 768 MiB |
| FastAPI | 512 MiB |
| PostgreSQL | 384 MiB |
| 一次性 migration | 256 MiB |

三個常駐容器的上限合計約 1.63 GiB；這是限制值，非保證用量，也不含映像建置與 Docker Desktop 虛擬機。PostgreSQL 使用 64MB shared_buffers、最多 30 條連線；Argon2 同時處理一個工作。8GB 電腦可先依此設定試用，觀察 Docker stats 與主機記憶體；若容器 OOM，查看日誌並依實際資源調整上限。

```bash
docker compose --env-file .env.development -f docker-compose.dev.yml stats --no-stream
```

開發設定使用 .env.development 與獨立 Compose 專案 tw-stock-dev；既有 Nginx 部署使用 .env、tw-stock-analysis 與 postgres_data。兩者不共用資料卷或後端／前端映像標籤。此輪先提供本機開發資料庫；正式資料庫、備份與 master 部署流程待後續規劃。Git 分支本身不會選擇資料庫，啟動時明確指定環境設定才會。

資料庫仍只有帳號、Session、驗證 Token 與自選清單五張應用資料表。TWSE 行情持續逐次請求，不寫入行情資料庫、不快取。

## 驗證紀錄

2026-10-21 已在獨立 Docker 專案完成實際建置、空白資料庫 migration、Cookie 登入／登出、裝置撤銷、前後端熱更新及 down/up 資料保存測試。後端 346 項、前端 111 項測試通過，前端型別檢查與建置通過；另透過 Vite 成功呼叫一次真實 TWSE API。

測試環境載入登入頁後，Vite 約 257 MiB、FastAPI 約 113 MiB、PostgreSQL 約 27 MiB，合計約 396 MiB。這是工作環境的單次容器用量，未包含主機瀏覽器、建置尖峰或 Docker Desktop 虛擬機，不能直接當成 8GB 電腦的總用量。詳細結果見 [驗證紀錄](local-development-verification.json)。
