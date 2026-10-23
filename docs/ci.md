# GitHub Actions CI

工作流程位於 [.github/workflows/ci.yml](../.github/workflows/ci.yml)，在 GitHub hosted Ubuntu 24.04 Runner 執行。

| 事件 | 執行內容 |
| --- | --- |
| 對 develop 或 master 建立／更新 PR | Python 檢查、資料庫測試、Docker 建置與啟動驗證 |
| push 到 develop、master 或 ci/** | 同上；ci/** 讓 CI 本身在合併前即可由遠端驗證 |
| workflow_dispatch | 手動執行；工作流程合併至預設分支後可由 Actions 頁操作 |
| 其他功能分支的一般 push | 不觸發，建立 PR 後才執行 |

同一個 ref 的新執行會取消尚未完成的舊執行；PR 與 push 使用各自的 ref。Workflow 的 GitHub Token 只有 contents: read，checkout 不保留 Git 憑證，Actions 固定完整 commit SHA。

## Python checks and PostgreSQL tests

使用 Python 3.12 與 uv 0.11.26，依 uv.lock 安裝；uv sync --locked 會拒絕未同步的 lockfile。執行 Ruff、mypy、alembic upgrade head、alembic check 與 pytest。

Job 提供 PostgreSQL 17 service，使用固定的測試專用帳密及全新 twstock_ci 資料庫。RUN_DB_TESTS=1 與 RUN_MIGRATION_TESTS=1 會啟用 ORM、帳號、自選清單、Cookie 認證與 migration 回歸測試。Migration 測試在此 service 另外建立暫存資料庫，驗證升級／降級與舊資料限制；不降級共用的 twstock_ci。

CI 不使用儲存庫外的 .env，也不需要開發或正式環境 Secrets。Service 的帳密只適用於該次 Runner 的測試資料庫。

既有 Nginx 整站測試與真實 TWSE 來源測試因未設定 GATEWAY_BASE_URL／RUN_LIVE_TWSE_TESTS 而略過。一般 CI 不向 TWSE 發出行情請求；來源回應由既有測試控制，真實 API 驗證仍由人員另外逐次執行。

## Build and smoke-test runtime image

Python 檢查與測試通過後，使用既有多階段 Dockerfile 建置正式執行映像。映像只載入當次 Runner，不登入或推送 Registry。BuildKit 快取用於相依層，不保存行情或應用資料庫內容。

[smoke_backend.py](../.github/scripts/smoke_backend.py) 會：

1. 建立唯一名稱的 Docker 網路、PostgreSQL 容器與隨機測試密碼。
2. 以 APP_ENV=production 及明確的 HTTPS origin 啟動後端映像。
3. 驗證 migration 尚未執行時，live 回 200、ready 回 503。
4. 使用同一份映像執行 alembic upgrade head 與 alembic check。
5. 驗證 ready 回 200、匿名 /me 回 401、未知 API 與正式環境 Swagger 回 404，以及公開資料集目錄可取得。
6. 在結束時清除自己建立的容器、資料卷與網路；失敗時輸出對應容器日誌。

此檢查不需要前端儲存庫，也不呼叫真實 TWSE API。

## 結果與後續發布

在儲存庫 Actions 頁或 commit／PR 的 Checks 查看兩個 job。Python 問題先看 test job；映像、環境設定或啟動問題看 docker job 與容器日誌。

目前 CI 提供執行結果；若要禁止檢查失敗的 PR 合併，需要另外在 GitHub ruleset／branch protection 設定必要檢查。此工作流程不設定分支規則。

本輪完成自動測試與 Docker 建置檢查。GHCR 發布、前後端發布版本配對、SSH、正式資料庫與正式主機部署留待下一階段。CI 不執行合併或部署，不需要正式帳密。
