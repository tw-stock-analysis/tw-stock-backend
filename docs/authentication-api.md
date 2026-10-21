# 後端登入與 Token 驗證

本版提供 Email／密碼登入、目前使用者、登出及多裝置管理。Session 固定有效 7 天，到期重新登入。公開註冊、寄信、忘記密碼 HTTP 流程與前端登入畫面另行實作；目前的驗證信箱／重設密碼能力仍是可信內部服務。

新增 5 個認證端點，加上既有 51 個公開端點，共 56 個 API 路由。沿用 `users`、`auth_sessions`、`verification_tokens`、`watchlists`、`watchlist_items` 五張表，migration head 仍為 `0004_account_rules`。TWSE 查詢維持逐次請求、不存行情資料、不快取，也不因收到 Cookie 而查詢認證資料庫。

## 認證流程與儲存

1. `POST /auth/login` 接受 Email 與原始密碼；Email 去前後空白並轉小寫，密碼不做正規化。
2. `AuthenticationService` 讀取未刪除帳號的 hash，關閉讀取交易，再由 `PasswordService` 使用 `pwdlib` Argon2id 比對密碼。不存在的帳號或不合法 hash 走 dummy hash 比對，最後仍拒絕登入。
3. 密碼正確後，開啟短交易並鎖定使用者，重新確認 hash 未改變、Email 一致、狀態為 `active`、未軟刪除且 `email_verified_at` 有值，再建立 Session。
4. 使用 `secrets.token_urlsafe(32)` 產生 32 個隨機 bytes 的 URL-safe Token；資料庫僅保存其 SHA-256 digest，交易成功後才設定 Cookie。JSON 回應不包含 Token 或任何 hash。
5. 私人端點每次從 Cookie 重新查資料庫，檢查格式、期限、撤銷及帳號狀態，更新 `last_seen_at`。`expires_at` 固定為建立時加 7 天，不隨活動延長；沒有 JWT、Refresh Token 或自動續期。

密碼重設與軟刪除使用同一使用者鎖，因此在舊密碼比對期間發生的重設／刪除，不會在撤銷完成後產生新的有效登入。裝置查詢與撤銷也會在交易中重新確認目前 Session。驗證信箱／重設密碼 Token 的用途及資料表與 Session 分開，不能用來登入。

Argon2 工作在 event loop 外執行，每個後端程序預設最多 2 個同時執行，超量回 503，不建立無限等待隊列。建議參數每個工作約需 64 MiB 記憶體。`PasswordService.hash()` 可供可信帳號建立／改密碼流程使用；目前只限制 1–1024 UTF-8 bytes，尚未定義公開註冊的密碼強度政策。

## Cookie、來源與 CSRF

| 環境 | Cookie 名稱 | 屬性 |
| --- | --- | --- |
| development／testing | `session` | HttpOnly、SameSite=Lax、Path=/，本機 HTTP 不設 Secure |
| production | `__Host-session` | Secure、HttpOnly、SameSite=Lax、Path=/，不設 Domain |

登入設定 `Max-Age=604800` 與到期時間；伺服器的期限與撤銷狀態仍是最終依據。瀏覽器自動附帶 Cookie，JavaScript 不讀取或保存 Token。登出會撤銷 DB Session 並清除相同名稱／路徑的 Cookie。

三個 POST 端點（登入、登出、登出其他裝置）在解析 body 前檢查：

- 只能有一個 `Origin`，且完整字串符合 `AUTH_ALLOWED_ORIGINS` 中的值。缺少、`null`、不同子網域、附加網域或重複值都拒絕；不以 `Host` 或 Referer 代替。
- 必須有且只有一個 `X-CSRF-Protection: 1`。它是瀏覽器跨來源防護的自訂 header，不是登入憑證，也不是秘密 Token。
- 必須有一個 `Content-Type: application/json`，可附 charset；拒絕 form／text body。
- 實際 body 上限為 16 KiB，包含分塊傳輸。登出與登出其他裝置必須沒有 body，連 `{}` 都會被拒絕，但仍須送上述 JSON Content-Type。

此組合適用於目前的同源 JSON API：前端與 `/api` 經同一 Nginx 入口，沒有跨來源 credential CORS，也沒有 `/auth/csrf` bootstrap、匿名 Session 或資料庫 CSRF secret。僅把來源列入設定不會啟用 CORS。未來如需跨網域前端或一般 HTML form，必須重新設計該流程的防護。

所有 API 回應都有 `Cache-Control: no-store` 與 `X-Request-ID`。讀取私人資料的路由使用 `CurrentSessionDependency`；未來新增私人路由仍須明確掛上認證與必要的寫入防護，公開 TWSE／股票／health 不掛認證 dependency。

## 端點契約

以下路徑均包含 `/api/v1` 前綴。

| 方法／路徑 | 輸入 | 成功回應 |
| --- | --- | --- |
| `POST /auth/login` | JSON `email`、`password`，不接受其他欄位 | 200 `{user, expires_at}`，設定 Cookie |
| `GET /me` | 有效 Cookie | 200 使用者物件 |
| `POST /auth/logout` | Cookie 可有可無，沒有 body | 204 空 body，清除 Cookie；重複執行安全 |
| `GET /me/sessions` | 有效 Cookie | 200 `{sessions: [...]}`，只列本人的未撤銷且未到期 Session |
| `POST /me/sessions/revoke-others` | 有效 Cookie，沒有 body | 204 空 body；保留目前 Session，撤銷同帳號其他 Session |

登入 Email 限 1–254 字元，檢查非空及有效 UTF-8；目前不另做完整信箱語法驗證。密碼為 1–1024 UTF-8 bytes。登入請求沒有 `user_id` 或 `password_hash` 欄位。`revoke-others` 的本人身分來自 Cookie，不接受指定其他帳號，也不接收 `current_password` 或輪換目前 Token；歷史草案中的這些欄位不屬於本版契約。

使用者物件欄位：

```json
{
  "id": "6f8c6194-f45d-4f18-a5bc-c8f4670f6dc1",
  "email": "reader@example.test",
  "display_name": null,
  "status": "active",
  "email_verified_at": "2026-10-21T01:00:00Z"
}
```

登入回應的 `user` 使用相同物件，`expires_at` 為 UTC 時間。裝置物件包含 `id`、`created_at`、`last_seen_at`、`expires_at`、可為 null 的 `user_agent`／`created_ip`，以及 `is_current`；依建立時間由新到舊排列。`user_agent` 最多保存 512 字元，僅供辨識參考，不是可信的裝置識別或授權依據。回應沒有密碼 hash、Token hash 或原始 Token。

### 錯誤

使用共通 `{error: {code, message, field_errors, request_id}}` 格式；欄位錯誤不回傳提交的密碼／Token，內部例外不回傳 SQL 或連線字串。

| HTTP／code | 意義 |
| --- | --- |
| 401 `LOGIN_INVALID` | Email／密碼錯誤、不存在、未驗證、停用或已刪除；使用同一回應 |
| 401 `SESSION_INVALID` | 私人請求沒有有效 Session、過期、撤銷或帳號無法使用；同時清除 Cookie |
| 403 `CSRF_REJECTED` | Origin 或自訂 header 不符合規則 |
| 413 `BODY_TOO_LARGE` | body 超過 16 KiB |
| 415 `JSON_REQUIRED` | Content-Type 不符 |
| 422 `VALIDATION_ERROR`／`BODY_NOT_ALLOWED` | 欄位不符，或 bodyless 端點收到 body |
| 429 `LOGIN_RATE_LIMITED` | 登入次數超限，附 `Retry-After: 60` |
| 503 `PASSWORD_SERVICE_BUSY`／`AUTH_CAPACITY_EXCEEDED` | 密碼工作或登入限制器容量已滿 |
| 503 `DEPENDENCY_UNAVAILABLE` | 資料庫不可用 |

前端之後接入時，私人請求的 `SESSION_INVALID` 應清除私人 UI 狀態並要求重新登入；`LOGIN_INVALID` 是登入失敗，不代表公開行情失敗。204 不可再呼叫 `response.json()`。

## 同源呼叫範例

以下函式在同源前端執行；`password` 由表單輸入傳入，不存進 localStorage 或日誌。瀏覽器會自行提供 Origin，JavaScript 不手動指定它。

```javascript
async function login(email, password) {
  const response = await fetch('/api/v1/auth/login', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Protection': '1',
    },
    body: JSON.stringify({ email, password }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error.code);
  return result; // user + expires_at，沒有 Session Token
}

async function currentUser() {
  const response = await fetch('/api/v1/me', {
    credentials: 'same-origin',
    cache: 'no-store',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error.code);
  return result;
}

async function logout(otherDevices = false) {
  const path = otherDevices
    ? '/api/v1/me/sessions/revoke-others'
    : '/api/v1/auth/logout';
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Protection': '1',
    },
    // 不傳 body；成功為 204，不能解析 JSON。
  });
  if (response.status === 204) return;
  const result = await response.json();
  throw new Error(result.error.code);
}
```

這是介面使用範例；本輪沒有修改前端頁面或前端 service。

## 部署設定與限流

本機 `.env` 範例：

```dotenv
APP_ENV=development
AUTH_ALLOWED_ORIGINS=["http://localhost:8080","http://127.0.0.1:8080"]
PASSWORD_MAX_CONCURRENCY=2
```

Origin 必須精確包含實際 scheme／host／port，不加結尾 `/`。若改用其他本機同源入口（例如 8000 或開發伺服器的代理入口），同步調整 JSON 陣列。正式部署例如：

```dotenv
APP_ENV=production
AUTH_ALLOWED_ORIGINS=["https://stocks.example.com"]
PASSWORD_MAX_CONCURRENCY=2
```

production 會拒絕空的 origins、非 HTTPS origins、萬用字元及含路徑的設定。必須由實際 HTTPS 入口提供服務，才能使用 Secure Cookie；現有 Compose 的 HTTP 入口本身不提供 TLS。正式環境停用 Swagger／OpenAPI 文件頁。

登入限制為每程序、每 IP 30 次／60 秒及每正規化 Email 10 次／60 秒，成功與失敗嘗試均計數。限制器最多保留 10,000 個雜湊識別鍵，容量滿時回 503，不移除仍有效的計數來放行新識別；過期後自動釋放。這些是登入次數的短期計數，與行情快取無關，也沒有新增資料庫表。

限制器在程序重啟時重置，多 worker／replica 不共用計數。正式多程序部署需在可信入口配置共同的限流，不能把單一程序的數值當作整個服務的總上限。應用程式只使用 ASGI `request.client`，不直接相信請求提供的 `X-Forwarded-For`。現有 Compose 的後端只在私有網路接收 Nginx；代理信任設定及 Nginx 重寫標頭需維持一致，增加 TLS／負載平衡層時要重新核對，避免任意來源偽造 client IP。

## 本機建立可登入的測試帳號

目前沒有公開註冊／驗證信件入口。可由有權操作**獨立開發資料庫**的人員執行以下可信腳本，建立帳號並直接消耗內部驗證 Token。這只用來準備本機測試資料，不代表完成真實信箱所有權驗證，也不能作為正式註冊流程。

先在後端目錄設定開發 DB 並執行 `uv run alembic upgrade head`。將下例存成 `/tmp/provision_local_user.py`，再執行 `uv run python /tmp/provision_local_user.py`；密碼以 `getpass` 輸入，不放在 shell 指令或版本控制中：

```python
import asyncio
from datetime import timedelta
from getpass import getpass

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.passwords import PasswordService
from app.services.accounts import AccountService
from app.services.credentials import VerificationService


async def main():
    settings = get_settings()
    if settings.app_env not in ("development", "testing"):
        raise RuntimeError("This script is only for an isolated development database")
    email = input("Test email: ").strip().lower()
    password = getpass("Test password: ")
    if password != getpass("Confirm password: "):
        raise ValueError("Passwords do not match")
    password_hash = await PasswordService().hash(password)
    engine = create_async_engine(settings.sqlalchemy_url())
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        accounts = AccountService(factory)
        user_id = await accounts.create(email, password_hash=password_hash)
        verification = await VerificationService(factory).issue(
            user_id, "verify_email", timedelta(minutes=5)
        )
        await accounts.verify_email(verification.value)
        print(f"Local test user created: {user_id}")
    finally:
        await engine.dispose()


asyncio.run(main())
```

腳本只輸出 user_id，不輸出密碼、hash 或驗證 Token。5 分鐘只是本機腳本的臨時 Token 期限，不是尚待設計的正式驗證信件政策。同一 Email 已有未刪除帳號時會失敗；不要直接覆寫既有使用者的密碼或驗證狀態。

## 驗證與相關文件

本次交付在獨立 PostgreSQL 上執行既有帳號／清單／migration／公開行情測試及新認證測試，共 **351 項通過、無跳過**；Ruff、mypy（46 個來源檔）及 `alembic check` 通過，沒有新 schema 差異。包含真實 Argon2 與 HTTP Cookie jars 的多裝置完整流程、密碼重設競態、部署路徑前綴的 CSRF 回歸，以及取消請求仍維持密碼運算上限。行情來源測試使用既有 mock transport；本輪未重跑 44 個真實 TWSE API 或前端驗收。

```bash
uv run ruff check .
uv run mypy
uv run pytest tests/test_passwords.py tests/test_login_limits.py tests/test_authentication_api.py -q
# 使用獨立、已遷移的 PostgreSQL：
RUN_DB_TESTS=1 uv run pytest tests/test_authentication_service.py tests/test_authentication_flow.py -q
```

HTTP 測試檢查 Cookie／CSRF、輸入與錯誤回應、限流及公開路由隔離；服務整合測試使用真正 PostgreSQL 交易，驗證登入／重設競爭、到期／撤銷、多裝置及交易失敗。原有帳號、清單與 migration 測試應一併保留。上述指令是驗證方式，實際測試結果以該次執行紀錄為準。

相關內容：[帳號資料表](account-tables.md)、[帳號與清單服務](account-services.md)、[ER 圖](database-er.md)。設計依據：[FastAPI 密碼雜湊](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/)、[OWASP Session 管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[OWASP CSRF 防護](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)、[MDN Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)。固定 7 天是本專案已確認的產品規則，並非 OWASP 通用預設。
