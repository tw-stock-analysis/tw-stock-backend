# FastAPI 後端：獨立 Python 環境

本資料夾可獨立安裝、啟動；不需要 frontend 或 Docker 才能啟動 API 程序。

## 第一次安裝

在 backend 內執行：

    uv sync --python 3.12
    # Only when .env does not exist; preserve existing settings.
    test -f .env || cp .env.example .env
    chmod 600 .env

使用 uv 管理本資料夾的 .venv；.python-version 固定使用 Python 3.12，uv.lock 記錄依賴版本。

## VS Code 自動啟用與每次啟動

以 VS Code 的 WSL 視窗開啟 backend 資料夾。沿用 fastapi_back 的方式：由 VS Code 記住已選取的 .venv/bin/python，並由 Python 擴充套件在新終端機自動啟用環境；不使用專案自訂的終端機啟動腳本。

關閉舊終端機，再開啟新終端機。若 VS Code 已記住其他解譯器，先執行 Python: Select Interpreter，選擇 /home/sean/my-project/tw-stock-analysis/backend/.venv/bin/python，再新開終端機。

啟用後提示字首為 (tw-stock-analysis-backend)，可直接執行：

    uvicorn app.main:app --reload

一般 Ubuntu 終端機可使用：

    cd /home/sean/my-project/tw-stock-analysis/backend
    source .venv/bin/activate
    uvicorn app.main:app --reload

也可在 backend 執行 uv run uvicorn app.main:app --reload，由 uv 使用專案環境啟動，不需先手動啟用終端機。

公開 ASGI 入口為 app/main.py 裡的 app。create_app 函式保留供測試使用；啟動不需要 --factory。預設 http://localhost:8000，API 文件 /api/v1/docs。

## 設定與資料庫

只讀 backend/.env 與程序環境變數，不讀專案根目錄或前端 .env。初次安裝時手動複製 .env.example 為 .env，再填入實際的 PostgreSQL 帳號、密碼及資料庫；若已有 .env，請保留原檔。這些步驟不會替你建立資料庫或帳號。

請填入新專案專用的 PostgreSQL 使用者／資料庫及其真正密碼。預設本機埠5432；遠端或 Docker 改對應主機與埠。SQLAlchemy 使用 AsyncSession 和 asyncpg。

DB 設定完成後：

    alembic upgrade head

初始 migration 只建立 app/market schema。GET /api/v1/health/live 不查資料庫；GET /api/v1/health/ready 需要可連線且已 migration 的 DB。資料庫不可用時，API 程序仍可啟動，但需要 DB 的功能不會就緒。

## 驗證

    uv run python -m pytest -q -m 'not integration'
    RUN_DB_TESTS=1 uv run python -m pytest -q

第二條包含真正 PostgreSQL 整合測試，需要先完成資料庫設定與 migration。已驗證 Python 環境、ASGI app 匯入及 3 項單元測試；尚未驗證實際 PostgreSQL 連線。

[整體規劃](docs/master-plan.md) · [後端設計](docs/backend-plan.md) · [資料庫設計](docs/database-plan.md)
## 與 fastapi_back 相同的開發結構

採用 Python 3.12、uv、pyproject.toml、uv.lock、獨立 .venv，以及 app.main:app 作為啟動入口。pyrightconfig.json 只指定本專案 .venv，不關閉額外型別檢查。資料庫版本檔放在 alembic/，對應 alembic.ini；alembic upgrade head 指令不變。

GitLab CI、GitLab Registry、原專案 Git 歷史、帳號密碼、AI/ESG 業務程式與資料、一次性腳本和產生的快取均未複製。新專案已有用途的 docs/ 與 tests/ 保留。VS Code 自動啟用仍由 Python 擴充套件及記住的解譯器負責，不建立自訂 .vscode 啟動腳本。

## 選用 Docker

日常開發仍直接執行 uvicorn app.main:app --reload，不需要啟動 Docker。

Dockerfile 與 docker-compose.yml 提供另一個獨立的本機測試環境，不依賴 GitLab。先確保 backend/.env 的 POSTGRES_PASSWORD 已設成實際密碼；從 backend 執行：

    docker compose up --build -d
    docker compose exec backend alembic upgrade head

API 位址為 http://localhost:8000，不能同時與本機 Uvicorn 使用相同埠。Compose 的 PostgreSQL 使用獨立 volume，不對外開放資料庫埠；不會使用或搬入既有 PostgreSQL 資料。容器中的 POSTGRES_HOST、POSTGRES_PORT 與 DATABASE_URL 由 Compose 指定，以連接此獨立資料庫；帳號、密碼與資料庫名稱取自 backend/.env。

停止但保留資料：

    docker compose down
