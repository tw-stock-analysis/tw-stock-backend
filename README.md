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
