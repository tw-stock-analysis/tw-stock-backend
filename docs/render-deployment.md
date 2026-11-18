# Render 部署

部署來源為前後端的 `feat/market-research-tools`，不需要合併 develop 或 master。後端根目錄 `render.yaml` 同時描述兩個 Git 儲存庫、專案與獨立 PostgreSQL。這是部署準備檔；必須先通過 Render 規劃並取得使用者同意，才套用資源變更。

## 資源

- 工作區：使用者確認的個人工作區。
- 專案：台股研究網站；環境：production。
- tw-stock-web：前端 Docker/Nginx，使用 frontend `Dockerfile.render`。
- tw-stock-api：後端 Docker/FastAPI，使用現有 Dockerfile，啟動命令 `python -m app.render_start`。
- tw-stock-postgres：新的獨立 PostgreSQL 17，與前後端在 Singapore。沒有搬移本機資料，也不共用其他網站的資料庫。

初始配置以 free 計畫請求規劃，最終可用性與費用以 Render 回傳為準。工作區已存在另一個免費 PostgreSQL，若免費額度不允許新增，必須停止並另行確認方案，不能自行改成付費或覆蓋其他資料庫。免費資源可能有休眠、用量或到期限制，需在實際 Render 規劃中確認，不將其當作永久正式資料保存方案。

## 為什麼前端使用 Nginx

目前瀏覽器呼叫同源 `/api/v1/...`，登入 Cookie 為 HttpOnly、Secure、SameSite=Lax。前端 Nginx 維持同源入口，再向後端的公開 HTTPS URL 代理，因此不需要把憑證放 localStorage 或依賴跨網站 Cookie。

免費後端沒有接收私網流量的能力時，前端使用後端公開 HTTPS URL；TLS 驗證及 SNI 均開啟，Host 送往後端的 Render 網址，Origin 保留瀏覽器前端來源。PostgreSQL 仍使用 `fromDatabase.connectionString` 的內部網址，憑證不寫入 Git。

## 部署順序

1. 將前後端部署設定提交並推送到上述功能分支。
2. 建立 Blueprint 規劃，使用 `existingResources=create_new`、`autoSync=false`，不接管已有資源。確認名稱、計畫、區域與費用後才套用。
3. 初始 `deployment-pending.invalid` 是明確的未接線狀態：不能當作完成的部署。新服務取得真實公開網址後，將 frontend 的 `BACKEND_ORIGIN` 改為後端實際 HTTPS origin；backend 的 `AUTH_ALLOWED_ORIGINS` 改為前端實際 origin 的 JSON array。
4. 使用 Render 工具以 merge 方式更新上述環境變數，保留 `DATABASE_URL` 及其他秘密。將同樣的非秘密網址更新回 render.yaml，確保後續規劃不會還原暫存值。
5. 重新部署需要採用新變數的服務，逐一追蹤到 live。服務 `autoDeployTrigger` 維持 off，避免一般功能推送意外更新此站。
6. 從前端網址驗證 SPA、`/api/v1/health/ready`、匿名 `/api/v1/me` 401、允許 Origin／CSRF 與至少一筆真實行情查詢。確認部署與資料庫 migration 都成功後，才提供使用者正式入口。

## 啟動與資料庫

Render 提供的 `postgres://`／`postgresql://` URL 會以 SQLAlchemy URL 物件切換成 asyncpg driver，保留編碼後的使用者密碼與主機，不重新拼接憑證字串。原本 `postgresql+asyncpg://` 仍可使用。

`app.render_start` 先驗證 PORT，再執行 Alembic upgrade head；失敗時不啟動伺服器。成功後用 exec 取代程序，讓 Uvicorn 正確接收 SIGTERM，監聽 `0.0.0.0:$PORT`。此啟動遷移方式針對單 instance 的免費服務；未來多 instance／付費正式環境應改用單獨 pre-deploy migration。

Health check 為 `/api/v1/health/ready`，會核對實際 migration head；前端自身健康檢查為 `/healthz`。新雲端資料庫是空白資料庫，已有本機帳號不會自動出現；建立第一個可登入帳號需另外使用可信的帳號建立流程，不提供公開註冊或預設密碼。

FinMind Token 可在後端 Render Environment 設定 `FINMIND_API_TOKEN`，不放進 Blueprint、前端或聊天。未設定時依既有免費來源行為，資料權限不足仍顯示該區錯誤。行情依使用者逐次請求，不增加快取、資料表或背景輪詢。

## 本機驗證紀錄

- Render 啟動／URL 正規化加上現有登入與部署設定：121 項測試通過。
- 前後端 Docker 映像建置成功。
- 隔離 PostgreSQL 啟動驗證：標準 URL、migration 0007、Render 自訂 PORT、readiness 成功，market schema 無表。
- 前端 `.github/scripts/smoke_render.py` 驗證自訂 PORT、HTTPS 上游憑證與 SNI、Host、URI/query、Origin／CSRF、Secure host-only Cookie 及 SPA 路由。

Render CLI 已安裝並核對官方發行檔 SHA256，但 CLI 是獨立認證，不能沿用 Render 連接器的 OAuth。若未登入 CLI，改用已連線的 Render Blueprint planning 驗證，不要求使用者在聊天貼 API key。此雲端環境讀取 Render 官方文件與部署網址還需要允許 render.com、*.render.com、*.onrender.com；未取得時必須標示無法完成的查證。
