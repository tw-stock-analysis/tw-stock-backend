# 後端、交易與認證規格

後端環境與啟動方式見 [README](../README.md)。目前實作與下方保留的歷史目標設計分開閱讀，現有程式契約以專屬文件為準。

## 目前實作

- 51 個公開端點與 5 個認證端點，合計 56 個；路由使用 `app/api/v1/endpoints`、`app/api/v1/router.py`，不是下方草案的 `app/api/routes`。
- [認證 API](authentication-api.md) 已實作 `pwdlib` Argon2id、已驗證 Email 帳號登入、固定 7 天 opaque Cookie Session，以及本人裝置列表／登出其他裝置。每次驗證更新 `last_seen_at`，不延長 `expires_at`；沒有匿名 Session、idle 欄位、JWT 或 Refresh Token。
- CSRF 採精確 Origin 白名單與 `X-CSRF-Protection: 1`、JSON Content-Type。沒有 `/auth/csrf` 或同步 CSRF secret；正式同源 HTTPS 使用 `__Host-session`，未啟用跨來源 credential CORS。
- 登入限制為每程序每 IP 30 次／60 秒、每 Email 10 次／60 秒，另限制 Argon2 並行。多 worker／replica 須在可信入口補共用限流；沒有新增限流資料表。來源 IP 只採信任代理處理後的 ASGI client。
- ORM、服務與清理方式見 [帳號與清單服務](account-services.md)。目前只有五張 `app` 表，最新 migration 為 `0004_account_rules`；登入／憑證修改採使用者列優先的鎖定順序，密碼比對在交易之外。
- [TWSE](twse-trading-api.md) 與[個股研究](stock-research-api.md) 每次查詢即時請求來源，不存行情、不快取、不重試或回退舊資料。公開端點不查認證資料庫。

前端已接上登入／登出及裝置管理。公開註冊、寄信、忘記密碼入口與自選清單 HTTP API 留待後續討論。

## 歷史目標設計（未核准／未完整實作）

以下保留早期構想供重新討論，不是現有規格，也不是已承諾的下一步。特別是匿名 Session／CSRF secret、閒置期限、共用 DB 限流、outbox、股票主檔、行情同步／快取／舊資料回退與 version 欄位，不可直接套用到目前程式或資料表；與上方「目前實作」不同時，以目前契約為準。

審查補充：登入建立 session、密碼重設、登出其他裝置都須以同一順序鎖定 users 父列；取得鎖後重新核對已驗證的 hash、帳號狀態，並在該短交易內建立／撤銷 session。昂貴的密碼雜湊在交易外執行，避免舊密碼登入在重設撤銷之後才插入新 session。

同步租約使用 dataset+scope_key 的穩定租約列，接手原子遞增 fencing_epoch；不是各 attempt 自己維護版本。發布交易鎖定租約，檢查 owner、epoch 與有效期後才更新資料及 dataset_heads，不符即取消發布。第一版按市場+資料集序列化同步與回補，避免重疊日期範圍使用不同租約。

### 1. 模組化單一後端（歷史草案）

FastAPI 處理 HTTP；Pydantic 管輸入輸出；SQLAlchemy 2.x 使用 asyncpg 連 PostgreSQL。API 與 job 共用專案但不同程序。下列新目錄實作需要時才加入現有骨架。

| 位置 | 職責 |
| --- | --- |
| app/api/routes | HTTP、狀態碼、認證 dependency、回應 DTO |
| app/schemas | Pydantic 欄位限制與契約 |
| app/services | 使用案例、授權、跨表規則、交易界線 |
| app/repositories | SQL 查詢、寫入、flush；不自行 commit |
| app/models、app/db | 資料表、約束、engine、session factory |
| app/integrations | 市場來源與郵件 adapter |
| app/analytics | 不依賴 HTTP／ORM 的計算函式 |
| app/jobs | 資料同步、重算、過期清理、郵件重試 |
| app/core | 設定、安全、日誌、錯誤分類 |

不直接把 ORM 物件整個回傳前端；DTO 明確挑選欄位，避免洩露 hash 或內部狀態。不為簡單 CRUD 建立多層泛型框架。

### 2. AsyncSession 與交易（歷史草案）

- lifespan 建立 engine/session factory，關閉時 dispose；factory 可共用，session 不可共用。
- 每 request 一個 AsyncSession，每個獨立工作一個；asyncio.gather 的多個 task 必須各有自己的 session。
- 使用 expire_on_commit=False、明確 eager loading 或欄位投影，避免序列化 DTO 時觸發隱式 lazy IO。
- service 在成功回應送出前 commit，任何錯誤 rollback；repository 只 flush。讀取請求在依賴清理時結束交易。
- 認證 SELECT 可能已觸發 autobegin，不能再盲目巢狀 begin。用例遵循一致的 transaction policy，以現有交易完成寫入；需新交易時先明確結束先前讀取。
- 不持有交易等待市場 HTTP、寄信或大量 CPU 運算；先取得資料／計算，再用短交易寫入。登入讀 hash 後結束讀取交易，驗證密碼後再確認帳號及 hash 仍有效並建立 session。
- 不讓 BackgroundTasks 使用 request session；可靠同步／寄信工作交給獨立程序。

這些限制依據 [SQLAlchemy asyncio 文件](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)、[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)。

連線池起始設定：每 API process pool_size=5、max_overflow=5、pool_timeout=10 秒、pre_ping=True；job 程序另用小 pool。總額為所有 process 的 pool+overflow，再加 migration／維運保留，不可超過 DB 配額。SQLAlchemy 已管理 pool，不另外建立同用途的 asyncpg pool。pre_ping 不會救回交易中斷，只有可確認冪等的整筆交易才有限度重試。[SQLAlchemy pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)。

### 3. 登入採 opaque cookie session（歷史草案）

正式環境同源 HTTPS；Cookie 使用 __Host-session、Secure、HttpOnly、SameSite=Lax、Path=/，不設 Domain。session token 使用 32 bytes 密碼學安全隨機值，DB 只保存 hash；前端不把它放 localStorage。伺服器每次驗證到期、撤銷及帳號狀態。

設計預設：匿名 session 10 分鐘；登入 session 閒置 24 小時、最長 7 天。last_seen_at／idle_expires_at 約每五分鐘節流更新，實際到期行為允許這個精度差。登入及敏感認證事件 rotate token，登出 DB 撤銷且清 Cookie。本機可用不同 cookie 名稱及非 Secure 設定，正式啟動檢查拒絕不安全設定。[OWASP session 指引](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。

### 4. CSRF 與 CORS（歷史草案）

1. GET /auth/csrf 建立或重用 session；未登入時 user_id=null、state=anonymous。
2. 回應目前 session 的隨機 csrf_secret，前端只存記憶體；DB 可保存此 CSRF secret 供重讀，與 session token 只存 hash 的規則不同。不得記錄日誌。
3. 所有改狀態端點，包括 login/register/logout，檢查 X-CSRF-Token 與 session 綁定並使用常數時間比較，同時驗證 Origin。缺 Origin 時只接受經核對的同源 Referer，兩者皆無則拒絕瀏覽器 mutation。
4. JSON 端點拒絕 form content type。登入成功撤銷舊匿名 session，建立新 session 與新 CSRF；重讀 token 不無條件 rotation，以免互相踢掉多分頁。
5. auth、me、私人回應設 no-store。GET 不變更業務資料；匿名 session bootstrap 是認證輔助行為且受限流。

SameSite 與 CORS 都不能取代 CSRF。正式同源不需要任意跨域；若日後分網域，CORS 僅列明確 origins，不能 credentials 配星號。[OWASP CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)、[FastAPI CORS](https://fastapi.tiangolo.com/tutorial/cors/)。

### 5. 帳號生命週期（歷史草案）

- 密碼採 Argon2id，成熟套件與符合 OWASP 基線的參數，再以部署硬體量測；hash/verify 放受控 thread pool，限制並發，避免阻塞 event loop。[OWASP 密碼儲存](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)。
- email trim 並以產品明訂的 case-insensitive login 政策產生 normalized 值；不刪除加號標籤或 Gmail 句點。原值保留供寄信。
- 註冊成功建立 pending_verification 帳號；驗證前不建立登入 session。註冊／忘記密碼／重寄信回一致 202 訊息，不揭露帳號是否存在。
- 驗證 token 有效期預設 24 小時、reset token 30 分鐘，都是隨機一次性值，DB 僅保存 hash。確認以 POST 執行，重設密碼同一交易消耗 token、更新密碼並撤銷所有舊 sessions，之後重新登入。
- 敏感帳號操作要求近期密碼驗證；第一版支援登出其他裝置，修改 email 與帳號刪除 UI 後續另作完整流程。
- 限流先用反向代理管 IP，應用程式用 DB 的短期時間窗紀錄管帳號與匿名 session 建立；多程序共用限制，過期清理。登入建議起始 5 次失敗／帳號／15 分鐘，另有 IP 上限，壓測與實際行為後調整；避免永久鎖帳號。

寄信採與帳號／token 同交易新增小型 outbox，再由 job 發送與重試。待寄內容中的原 token 必須加密儲存、限期保留且寄出後清除；hash 欄位仍用於驗證。開發用測試信箱，正式上線前配置實際 provider。寄信不能讓 DB 交易一直等外部網路。

### 6. 授權、競態與限制（歷史草案）

清單查詢同時帶 id 與 session.user_id；讀寫他人的清單回 404。初期每人最多 10 個清單、每清單 100 檔，名稱 1–64 字。新增前鎖 owner／清單父列再計數，DB 唯一約束處理並發；不能只用先 SELECT 再 INSERT。

新增單檔用冪等 PUT，已存在不重複新增。清單內容每次實際變更增加 version，改名、排序、刪除清單要求預期 version，衝突回 409；設定也用 version。排序送完整現有 ID 集合，缺漏、重複或別人的項目回 422，避免遺失更新。

reset token 用條件 UPDATE／列鎖消耗，只有一個請求成功。DB UNIQUE、FK、CHECK 為最終防線；錯誤轉成穩定業務代碼，不把 IntegrityError 或 SQL 直接回給使用者。

### 7. 背景同步與快取（歷史草案）

獨立 job 程序採可重跑命令，由平台排程或單一 scheduler 觸發；不要在每個 API worker 啟動排程。工作紀錄包含來源、資料集、範圍、attempt、狀態及租約。到期 lease 可接手，fencing/version 防止舊 worker 覆寫新工作；外部 HTTP 期間不持有一般 DB 交易。

來源設定 timeout、低並發、重試上限、指數退避與 Retry-After。先保存原始資料證據，再驗證、正規化、原子發布；錯誤維持上一版。所有即時計算限制期間、參數與資料點數；大型重算在 job，短且有界的指標計算才可在 request 的受控 executor 執行。

公開價格可用 ETag 與短 HTTP cache，須由純公開端點提供，不能混入自選狀態。私人資料 no-store。API 不依賴上游即時成功才回應。[FastAPI BackgroundTasks 的適用範圍](https://fastapi.tiangolo.com/tutorial/background-tasks/)。

### 8. Migration 與品質（歷史草案）

Alembic 是 schema 唯一變更路徑，autogenerate 必須檢查 rename、刪欄與資料回填；正式 API 啟動不 create_all，也不由每 worker 自動 migrate。應用帳號與 migration 帳號分權限，DB 不開放公網。整合測試必須涵蓋真正 PostgreSQL、並發、CSRF、跨帳號、rollback 與 token 重播。[Alembic autogenerate](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)。
