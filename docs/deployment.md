# GHCR 與正式主機部署

目前提供可驗證的部署設定，尚未建立正式主機，也沒有自動 SSH 部署。GitHub Actions 發布映像後，主機直接拉取指定版本；主機不需要建置前端或安裝 Python 應用依賴。

## 取得前後端映像版本

兩個 repo 的 CI 成功後，publish job 會發布已通過檢查的映像：

| Repo | GHCR |
| --- | --- |
| tw-stock-backend | ghcr.io/tw-stock-analysis/tw-stock-backend |
| tw-stock-frontend | ghcr.io/tw-stock-analysis/tw-stock-frontend |

develop／master 的 push 會更新各自分支標籤；ci/ghcr-release 只發布候選版本供合併前測試，不更新 develop／master。PR 不發布。

在 Actions 的 Publish verified image to GHCR job 摘要，或 image-release-COMMIT_SHA artifact 的 image-release.json，取得：

```text
ghcr.io/tw-stock-analysis/tw-stock-backend@sha256:64個十六進位字元
ghcr.io/tw-stock-analysis/tw-stock-frontend@sha256:64個十六進位字元
```

部署必須固定這兩個 digest。run-ID-ATTEMPT 標籤可追查執行；sha-COMMIT、develop、master 標籤可能因重跑或新提交更新，不作為正式部署輸入。保存本次兩個 image-release.json，可追查原始碼與發布版本配對。

新 package 保留 GitHub 預設可見性；流程不自動改成公開。私人 package 的部署主機須準備有 read:packages 權限的憑證，透過密碼標準輸入或互動式 docker login ghcr.io 登入，不把 Token 寫進程式庫。未來 SSH 部署使用的憑證由 GitHub Environment／Secrets 管理。

## 正式主機需要準備的項目

- Linux、Python 3.12、Docker Engine／Compose v2、OpenSSL。
- 指向主機的網站網域，以及該網域的有效 TLS 憑證。
- 對外 80／443；FastAPI 與 PostgreSQL 留在 Compose 內部網路。
- 後端儲存庫的已審閱版本，包含 docker-compose.deploy.yml、scripts/ 與 deploy/。
- GHCR 拉取權限、足夠的持久磁碟、獨立於主機的備份儲存位置。

正式憑證由選定主機後的憑證管理方式提供。TLS_CERT_DIR 需有 fullchain.pem 與 privkey.pem，憑證／私鑰配對，且憑證網域符合 SITE_HOST。若使用 Certbot，不能只掛載指向目錄外 archive/ 的 live/ symlink；需複製實體檔案至專用 TLS 目錄，或讓所有 symlink 目標都位於同一掛載目錄內。

憑證更新後執行 nginx -t 與 reload，確保續期後提供新版憑證；主機建立時需一起設定續期排程。

## 設定與套用

在主機的後端部署目錄複製 .env.production.example 為 .env.production，限制檔案權限為 0600，填入：

| 變數 | 內容 |
| --- | --- |
| BACKEND_IMAGE／FRONTEND_IMAGE | 前後端各自已驗證的完整 GHCR digest reference |
| SITE_HOST | 小寫網站 hostname，不含 https://、路徑或連接埠 |
| AUTH_ALLOWED_ORIGINS | 精確 HTTPS origin JSON，例如 ["https://stocks.your-domain.com"] |
| TLS_CERT_DIR | 憑證與私鑰的絕對目錄 |
| POSTGRES_USER／POSTGRES_PASSWORD／POSTGRES_DB | 專屬正式資料庫帳密；替換密碼 placeholder |
| WEB_BIND_ADDRESS | 預設 0.0.0.0 |
| WEB_HTTP_PORT／WEB_HTTPS_PORT | 預設 80／443；自訂 HTTPS port 時 origins 需包含該 port |

不提交 .env.production、憑證、私鑰或備份。執行：

```bash
python3 scripts/deploy.py --env-file .env.production --check
python3 scripts/deploy.py --env-file .env.production
```

--check 解析 Compose，驗證 digest 格式、正式來源、資料庫隔離、憑證剩餘有效期超過一天、網域與私鑰配對；不啟動容器，也不代替 Registry 存取檢查或公開 HTTPS 信任鏈驗證。

實際部署採用一份權限 0600 的暫存環境設定快照，並鎖定 .deploy.lock 防止同時部署：

1. 拉取固定 digest 的前後端映像；此步失敗時不變更現有服務。
2. 啟動／確認正式 PostgreSQL 健康。
3. 使用 pg_dump -Fc 備份至 backups/before-deploy-時間.dump，檔案權限 0600；備份失敗即停止。
4. Compose 執行 migration，成功後才啟動 FastAPI，再等待 readiness 後啟動 HTTPS Nginx。
5. 執行 nginx -t；操作人員再驗證公開入口：

```bash
curl --fail https://stocks.your-domain.com/api/v1/health/ready
```

正式 Nginx 直接終止 HTTPS，並使用 $scheme=https 轉送代理資訊；瀏覽器的 Cookie／Origin 與 API 同源。HTTP 網站流量轉到 HTTPS，/healthz 僅供容器存活檢查。正式 APP_ENV 固定 production，Swagger 關閉。

## 資料保存與版本回復

正式專案為 tw-stock-prod，資料卷為 tw-stock-prod_production_postgres_data；與本機 tw-stock-dev 完全分開。停止時使用：

```bash
docker compose --env-file .env.production -f docker-compose.deploy.yml down
```

保留資料時不要加 -v。變更 .env.production 中的 PostgreSQL 密碼不會自動修改既有資料庫角色密碼。

每次部署前的本機 dump 是遷移前快照，仍需另設異地備份、保留期限與還原演練。回復程式版本時指定舊 digest；資料庫 migration 需另外評估與舊程式的相容性，不自動執行 downgrade。

應用程式仍只保存帳號、登入憑證、驗證 Token 與自選清單；TWSE 行情維持逐次請求，不入庫、不快取。

## CI 驗證與尚待設定

後端 CI 除原有測試外，會用已驗證後端映像與固定 frontend commit 9a6bcae89472ee0c64f174b6f5e61e67a53ec247，實際啟動部署 Compose。CI 憑證只信任當次產生的測試 CA，仍驗證 hostname 與 TLS；檢查 HTTP 轉址、migration、Secure／HttpOnly Cookie 登入／登出，並清除自己的測試資料卷。正式部署沒有停用 TLS 驗證的選項。

這項整合測試的 frontend pin 需在 API／部署契約更新時一起更新。實際發布時，仍由操作者選定前後端兩個成功發布的 digest。

主機、DNS、正式憑證與續期、GHCR 拉取憑證、SSH 部署與異地備份排程，要在正式主機選定後再設定。目前的工作流程發布映像，尚未部署網站。
