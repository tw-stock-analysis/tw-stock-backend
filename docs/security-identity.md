# 證券永久身分與證據時間軸

新增 `app.security_identities`、`app.security_listings`、`app.security_identifiers`，migration head 為 `0006_security_identity`。這三表只保存證券身分、掛牌事件與來源證據，不保存行情、因子值、篩選結果或回測結果。

內部 UUID 是不變登錄身分；ticker、公司名稱、統編、ISIN 都不是直接替代的永久主鍵。統編只作發行人參考，不因相同統編而合併證券。不同時期相同市場／代碼可以對應不同 UUID，條件是掛牌區間不重疊。

## 操作者匯入

公開 API 只有讀取；身分變更只能由受信任操作者執行 CLI。

```sh
UV_CACHE_DIR=/tmp/twstock-uv uv run python -m app.services.security_import --twse --dry-run
UV_CACHE_DIR=/tmp/twstock-uv uv run python -m app.services.security_import --twse --apply
UV_CACHE_DIR=/tmp/twstock-uv uv run python -m app.services.security_import --file /path/reviewed-identities.json --dry-run
UV_CACHE_DIR=/tmp/twstock-uv uv run python -m app.services.security_import --file /path/reviewed-identities.json --apply
```

預設 dry-run，不保留任何資料。`--twse` 每次依序重新取得三份官方 metadata，沒有快取、重試或任意 URL 代理：

1. `https://openapi.twse.com.tw/v1/opendata/t187ap03_L`：公司代號、簡稱、發行人統編、實際上市日期。
2. `https://openapi.twse.com.tw/v1/company/newlisting`：只有 `ApprovedListingDate` 是實際上市買賣日，不能用核准相關 `ListingDate` 欄位代替。
3. `https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml`：終止上市生效日。

目前官方 newlisting 部分舊列日期空白，不能從名稱、核准日或 FinMind 更新日補成上市日。歷史列只在代碼／名稱與可證明的起訖事件唯一配對時納入；未配對、日期缺漏或衝突列記入摘要，不虛構時間軸。股票類型預設 `other`，因公司基本資料不能單獨證明普通股／TDR／ETF 類型。來源目前只足以自動填入 TWSE 部分範圍，TPEX、興櫃、ISIN 與更早缺漏仍需有證據的資料。

JSON 檔必須符合 `IdentityImportBundle`（`app/schemas/security_identity.py`）：

- `schema_version` 固定 1，`securities` 為最多 10,000 筆證券。
- 每筆需要穩定 `registry_key`、顯示 `name`、`instrument_type`、至少一段 `listings`；可提供既有 UUID `id`、`issuer_ubn` 與具日期的 `identifiers`。
- 掛牌段需要市場 `TWSE`／`TPEX`／`EMERGING`、代碼、名稱、`valid_from`、可空 `valid_to`，以及對應的起訖來源證據。
- 證據包含 `source_name`、不含憑證的 HTTPS `source_url`、實際查核日 `observed_on`、文件證明的 `effective_date`。開始／終止證據日期必須等於對應區間邊界。
- `identifiers` 支援 `isin`、`name`，同樣需要起訖生效日期證據；ISIN 驗證格式及檢查碼。名稱只有在具日期別名中才表示歷史名稱；最上層名稱和掛牌顯示名稱不宣稱從上市首日即使用。
- 掛牌與別名區間都是 `[valid_from, valid_to)`：包含開始日，不包含終止生效日；終止日不能當作最後成交日。

重跑相同登錄鍵保留 UUID，內容不變不更新版本時間。掛牌跨市場延續需由同一明確登錄身分的來源證據支援；不按代碼、名稱或統編自動串接。已終止日期不自動改寫或重開，發行人統編變更與 UUID／登錄鍵不一致也需人工核對。匯入使用單一 PostgreSQL 交易和 metadata 表鎖；任一區間重疊或識別碼衝突會整批 rollback。檔案與單一官方回應上限 8 MiB，額外行情欄位被型別驗證拒絕。

## 公開查詢

- `GET /api/v1/securities?code=&market=&as_of=&include_inactive=false&page=1&limit=20`：最多每頁 100 筆。指定日期預設僅列當日有效掛牌；`include_inactive=true` 可同時查看已結束或目前沒有生效區間的登錄身分。
- `GET /api/v1/securities/{uuid}?as_of=YYYY-MM-DD`：完整已登錄時間軸與該日市場判定。
- `GET /api/v1/securities/resolve?market=TWSE&code=2330&as_of=YYYY-MM-DD`：查詢該市場／代碼在指定日是否對應唯一身分。

狀態為 `verified`、`unknown`、`ambiguous`。沒有已驗證區間時保持未知，不用當前市場回填；衝突時不挑選其中一筆。所有回應 no-store；不接受未來的 as_of 判定。永久身分功能使用 metadata 資料庫，不向行情來源取價。

嚴格回測可呼叫 `SecurityIdentityService.verify_range(ref,start,end)`：以截止日定位唯一 UUID，要求期間的掛牌軌跡連續、無多重身分且涵蓋完整請求。已證明且代碼未變的上櫃／上市轉換可跨段；任何興櫃區間、空窗或代碼變更都會拒絕，避免把興櫃參考開盤欄或目前代碼套入舊證券。`DELISTED` 不是場所，內部驗證可用它在上市／上櫃歷史段定位，仍須在請求日有實際有效掛牌證據。

驗證已涵蓋永久 ID 重跑、同碼重用、半開邊界、轉板、空窗、興櫃、代碼變更、UUID 衝突、ISIN、別名終止證據、整批 rollback、競爭匯入、dry-run、直接 DB 衝突防禦及只讀 HTTP。測試只使用合成身分 metadata，不保存行情。
