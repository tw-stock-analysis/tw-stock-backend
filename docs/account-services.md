# 帳號與自選清單服務

本輪不新增 HTTP 路由、股票主檔或行情儲存；現有 API 仍為 51 條。五張 `app` 表由 SQLAlchemy ORM 操作，Alembic 最新版為 `0004_account_rules`。完整欄位與約束見 [ER 圖](database-er.md)。

## 已確認的規則

- 軟刪除釋出 Email，重新註冊建立新 UUID，不轉移舊清單或登入憑證。停用但未刪除的帳號仍保留 Email。
- 同帳號的清單名稱去除前後空白後，以 PostgreSQL `lower(name)` 判斷唯一；每人最多 10 份，每份最多 100 檔。
- 自選股加入時即時查 TWSE，不使用證券主檔或舊驗證結果；只儲存市場、代碼與排序。
- 同清單 position 唯一，使用 `DEFERRABLE INITIALLY DEFERRED`，支援單交易交換順序。
- 同使用者、同用途最多一張未消耗且未撤銷的驗證 Token。新核發會撤銷同用途舊列，包含已過期者。
- Session／驗證 Token 失效超過 30 天才清理，執行頻率由部署排程決定。

## 服務介面

服務接受 `async_sessionmaker[AsyncSession]`，每次操作自己開啟並結束交易，成功才 commit。可使用現有 `app.state.session_factory`。
不把原始 ORM 物件帶出已關閉的 Session；回傳 UUID、不可變清單資料或明確的憑證結果。

| 服務 | 方法 | 行為 |
| --- | --- | --- |
| `AccountService` | `create(email, password_hash=...)` | 儲存預先計算的密碼雜湊，建立待驗證帳號；Email 衝突回 `EMAIL_IN_USE` |
| | `soft_delete(user_id)` | 鎖定帳號、軟刪除、停用及撤銷所有未撤銷 Session／待用驗證 Token，重複呼叫安全 |
| | `verify_email(token)` | 原子消耗驗證信箱 Token，記錄驗證時間並啟用帳號，不自動登入 |
| | `reset_password(token, password_hash=...)` | 消耗重設 Token、更新密碼雜湊、撤銷所有 Session 與其他待用 Token；任一步失敗全部 rollback |
| `VerificationService` | `issue(user_id, purpose, ttl, created_ip=...)` | 使用 32 個隨機 bytes 產生 URL-safe Token，撤銷舊列並建立新列，只存 SHA-256 digest |
| `SessionService` | `issue(user_id, ttl, ...)` | 為已通過認證的 active 帳號建立 Session；多裝置各有一筆 |
| | `authenticate(token)` | 檢查雜湊、期限、撤銷狀態、帳號 active 且未刪除，回傳 Session／使用者 ID |
| | `revoke(user_id, session_id)` | 僅能撤銷所屬帳號的 Session |
| | `revoke_others(user_id, current_session_id)` | 驗證目前 Session 有效且屬於本人後，撤銷其他裝置 |
| `WatchlistService` | `create`、`list`、`get`、`rename`、`delete` | 檢查帳號與清單歸屬、名稱唯一及清單容量 |
| | `add_item`、`remove_item`、`reorder` | 新增前即時驗證來源；保護容量、排序及父清單更新時間 |

`password_hash` 必須由可信的密碼雜湊函式預先產生完整 Argon2id 編碼結果。這些內部服務只檢查 Argon2id 前綴，沒有實作雜湊計算、密碼比對、完整 Email 格式驗證或密碼政策；未來 HTTP 認證入口須負責，不能接受使用者自行提供的 hash。昂貴的雜湊計算應在交易之外完成。
`SessionService.issue()` 本身不驗證密碼；user_id 必須來自已完成認證的可信流程。Cookie／CSRF、寄信、驗證與重設流程的公開回應仍待 HTTP API 設計。
`ttl` 必須由呼叫端明確給正值，尚未固定 Session／驗證連結有效天數；清理的 30 天不是登入有效期。
憑證原文只在 `IssuedCredential.value` 提供，排除於 repr，資料庫及清理輸出沒有原文，不應將回傳物件轉成一般日誌。

## 交易與並發

所有會修改使用者私人資料的服務，鎖定順序一致為使用者列 → 清單／憑證列。清單容量在鎖內檢查後寫入，沒有單純先 COUNT 後無鎖 INSERT 的空窗。
跨帳號清單操作回 `WATCHLIST_NOT_FOUND`，避免暴露另一位使用者的清單。呼叫端仍須提供已驗證的 user_id，這不等於新增了 PostgreSQL RLS。

新增股票的流程：

1. 檢查帳號與清單 owner，結束 DB 交易。
2. 每次呼叫既有 `StockService.quote()`，取得新的 TWSE `STOCK_DAY_ALL`，不持有 DB 連線或鎖。
3. 開啟寫入交易，鎖使用者與清單，再次檢查歸屬、帳號狀態、重複股票及容量，通過才插入。

來源 404／502／503、讀取期間刪除帳號／清單，都不會插入項目或改寫清單時間。來源使用最新日成交集合，因此只能驗證代碼出現在本次來源，不是永久證券身分保證。
新增附加在最大位置之後；移除後重新編為 0 起算；重排必須提供全部且僅有的股票，失敗整筆 rollback。每次改動項目，同一交易更新父清單 `updated_at`。

Token 消耗在鎖定使用者後使用 DB `clock_timestamp()` 重新檢查期限，再執行條件 UPDATE 與 RETURNING；兩個並發重播只能一個成功。
Partial unique index 只含「未消耗、未撤銷」，不使用 `expires_at > now()` 這類會隨時間改變的 index 條件。過期 Token 仍可能占位，核發服務會先明確撤銷。
直接繞過服務寫 ORM／SQL，不會自動執行容量檢查、即時來源驗證或軟刪除撤銷流程；應用程式須透過服務執行上述操作。

## 清理指令與排程

```bash
uv run python -m app.services.auth_cleanup --dry-run
uv run python -m app.services.auth_cleanup
uv run python -m app.services.auth_cleanup --retention-days 30 --batch-size 1000
```

符合任一條件才清理：`expires_at`、`revoked_at` 或驗證 Token 的 `consumed_at` 早於 DB 目前時間減 30 天。最近失效與仍有效的憑證保留，不刪除帳號或清單。
每批最多 1000 筆，使用 `FOR UPDATE SKIP LOCKED` 與短交易，多個清理程序不會重複刪除同一筆。若某些列正在使用而被略過，後續排程再清理。
輸出只有兩種憑證的數量與 dry-run 狀態；連線失敗回非零 exit code，不輸出 DB URL、憑證或 SQL 參數。

部署時可在主機 cron 加入每日一次的指令，例如下例使用 UTC 19:00（台北次日 03:00），適用於支援 CRON_TZ 的 cron；其他版本應依主機時區換算。需將路徑替換為實際部署位置，並確認 cron 的 PATH 可找到 Docker：

```cron
CRON_TZ=Etc/UTC
0 19 * * * cd /srv/tw-stock-backend && docker compose --env-file .env run --rm --no-deps backend python -m app.services.auth_cleanup
```

這是部署範例，本輪沒有修改主機排程或新增常駐服務。cron 應監控非零退出與日誌；可先執行 dry-run 確認範圍。

## Migration `0004_account_rules`

已推送的 `0002`、`0003` 不修改。升級會：

1. 先檢查同帳號重複名稱、超過 10 份清單或 100 檔項目的舊資料。若存在則停止整筆遷移，需先人工處理；不擅自改名或刪除資料。
2. 改為未軟刪除 Email 部分唯一索引，並加入清單名稱唯一索引。
3. 按原本 `(position, market, stock_code)` 的順序重新編號，保留顯示次序，加入延後檢查排序唯一約束並移除 position 預設值。
4. 增加驗證 Token 的 `revoked_at`，撤銷舊的過期 Token、軟刪除帳號的憑證，以及同用途多張待用 Token 中較舊者，再建立待用唯一索引。

回退至 `0003` 時，若已存在重用 Email 的多個帳號，會停止，不能用刪除帳號方式自動繞過。需先由維運明確處理衝突。
回退前會將已撤銷而未消耗的驗證 Token 標記為已消耗，確保舊版沒有 revoked_at 時也不能重新使用；撤銷與消耗的區分會遺失，排序原本的重複位置也不會還原。
所有 DDL 與資料調整同一 PostgreSQL 交易，失敗不留下部分 migration。

## 驗證

```bash
uv run alembic upgrade head
uv run alembic check
RUN_DB_TESTS=1 uv run pytest tests/test_database.py tests/test_watchlists.py tests/test_account_services.py -q
# migration 測試會建立及刪除自己的隨機測試 DB，需 CREATE DATABASE 權限：
RUN_DB_TESTS=1 RUN_MIGRATION_TESTS=1 uv run pytest tests/test_account_migration.py -q
```

服務測試使用真實 PostgreSQL、多個連線與實際 commit，涵蓋容量競爭、跨帳號操作、來源失敗／無資料、回應期間軟刪除、重播、回滾、鎖等待後到期、清理預覽／多批次／多程序。
TWSE 合成傳輸測試不代表官方實測；本輪另以兩次循序真實 TWSE 請求確認 `0050` 可加入、`ZZZZ` 被拒絕，測試帳號及清單已清除，沒有行情入庫。
