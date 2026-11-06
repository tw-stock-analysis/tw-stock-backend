# 證券身分、全市場掃描與跨股回測

本次依使用者指示繼續跳過提醒通知，沿用 `feat/market-research-tools`。所有價格、財報快照與回測結果只存在當次請求的記憶體，不寫行情表、不跨請求快取、不自動重試、不背景輪詢。只有證券身分與證據等參考事實新增永久資料表；這些不是行情快照。

## 永久 ID 與歷史掛牌區間

新增 `app.security_identities`、`app.security_listings`、`app.security_identifiers`，migration 為 `0006_security_identity`。證券使用持久 UUID，與股票代碼、公司統編分開；同代碼重用、公司改名、轉板、換股或同發行人的不同股別不會自動合併。只有經審核登錄鍵／既有 UUID 明確指定連續性時，才可把多段市場、代碼與 ISIN 別名登錄到同一證券。

有效區間為 `[valid_from, valid_to)`，含生效日、不含終止生效日；掛牌起點、終止、ISIN／歷史名稱別名的起訖都須附來源網址、來源名稱、觀察日期與生效日期。未登錄期間回 `unknown`，衝突回 `ambiguous`，不以目前市場回填。代碼可能變更，UUID 保持不變；目前跨股取價仍只支援回測期間同代碼，遇到代碼變更明確停止，避免以目前代碼抓錯歷史行情。

公開只讀 API：

- `GET /api/v1/securities?code=2330&market=TWSE&as_of=2025-03-31&include_inactive=true&page=1&limit=20`
- `GET /api/v1/securities/resolve?market=TWSE&code=2330&as_of=2025-03-31`
- `GET /api/v1/securities/{uuid}?as_of=2025-03-31`

前端入口為 `/research/security-identity` 及其 UUID 詳情頁；可從證券名錄進入。永久網址不依股票代碼改變，頁面保留選定歷史日期與證據連結。

匯入為可信任操作人員使用的 CLI，沒有公開寫入 API：

```sh
uv run alembic upgrade head
# 先取得官方來源並試算，預設不保存
uv run python -m app.services.security_import --twse --dry-run
# 確認來源摘要後，保存身分證據
uv run python -m app.services.security_import --twse --apply
# 補充已審核的轉板、換碼、ISIN 或歷史名稱證據
uv run python -m app.services.security_import --file /path/to/reviewed-identities.json --dry-run
uv run python -m app.services.security_import --file /path/to/reviewed-identities.json --apply
```

JSON 規格由 `IdentityImportBundle` 定義：schema_version=1、securities 陣列，每筆有 registry_key、可選既有 id、name、instrument_type、可選 issuer_ubn，以及 listings／identifiers。每個 listing 含市場、代碼、登錄名稱、有效區間、start_evidence／end_evidence；每個 identifier 包含 kind=isin 或 name、value、有效區間與 evidence／end_evidence。證據網址只作引用，不抓取任意匯入檔中的 URL。ISIN 格式與檢查碼會驗證；同市場代碼或同 UUID 區間重疊、不同 UUID 共用重疊 ISIN 等衝突使整批回滾。匯入冪等，重跑不換 UUID，已關閉區間不會自動重新開啟。

**實際覆蓋範圍**：`--twse` 依序讀取固定官方 `/opendata/t187ap03_L`、`/company/newlisting`、`/company/suspendListingCsvAndHtml`。基本資料的上市日期及 newlisting 的 **ApprovedListingDate** 才作掛牌起點；名稱相同且日期可配對的終止事件才關閉區間。newlisting 的 ListingDate 是另一個核准相關欄位，不能冒充實際交易起日。沒有日期、無法配對或可能混合身分的列會在摘要列為跳過，不推定為完整歷史。

這建立了可持續補證的永久身分系統，**不是已取得全台所有證券的完整永久身分史**。上述官方 API 沒有完整上櫃／興櫃起訖或 ISIN；現有 FinMind 名錄只有來源參考日期。其餘市場、股別、代碼變更與歷史名稱需匯入有證據的官方或授權紀錄。查不到證據時保持未知，不把統編當證券 ID。環境目前只允許指定來源網域，ISIN／櫃買網站未在應用程式網域清單中；不繞過網路政策。

## 全市場基本面掃描

新增 `POST /api/v1/research/market-screen`，前端 `/research/market-screen`。依整份所選市場名錄與 FinMind 全市場單期快照，篩選營收年增、毛利率、營業利益率與負債比，不再限制手動候選 10 檔。

輸入 markets（TWSE／TPEX／EMERGING）、revenue_month（月首日）、report_date（季末日）、rules、sort、descending、result_filter（matched／all／insufficient）、page、limit。營收期間 M 使用來源 M+1 月 1 日紀錄，去年同期用同樣映射；財報按指定精確季末。資料來源是固定白名單 snapshot，不帶 data_id，也不在權限受限時改成逐股爬取。全市場快照是 FinMind 付費模式，沒有權限時回固定 `FINMIND_ACCESS_DENIED`，由使用者補相應後端 Token 後手動重新查詢。

回傳整份候選分母、符合、不符合、資料不足與身分錯誤數量；缺資料或異常負負債值不當作低負債優勢，不補零。衝突身分不顯示混合後的比率。所有計算使用 Decimal，門檻比較前不先四捨五入。換頁重新取得來源，結果可能隨來源更新而改變。

單次總預算 50 秒；快照專屬上限 64 MiB／300,000 rows，原個股 API 仍維持 8 MiB／15,000 rows。超限、錯誤日期或權限失敗不截斷回成功；資料轉成所需索引後釋放原始快照。這些上限是防止無限資源使用的界線，付費成功資料若超出會明確拒絕，不聲稱所有季度都已驗證可容納。

全市場指目前來源名錄的候選範圍，不是指定歷史時點完整上市名單。財報沒有可驗證公告時間且可能重編；結果不能直接當作無前視偏誤的財報因子回測。

## 跨股因子與再平衡

新增 `POST /api/v1/research/portfolio-backtest`，前端 `/research/portfolio-backtest`。

- 2–20 檔使用者指定標的；同碼不得以不同市場重複配置，同一永久 UUID 也不得重複。
- 固定權重：各權重 0–1，合計大於 0 且不超過 1，未配置部分保留現金。
- 動能因子：lookback 筆共同有效收盤價的累積報酬，取前 top_n 等權配置。
- 低波動因子：共同窗每日報酬母體標準差乘根號 252，取最低 top_n 等權配置。
- 月／季在區間第一個共同交易日調整；訊號與排序只使用前一有效日以前資料，當日開盤只用於成交和配置股數換算。不得使用當日收盤決定同日開盤。
- 先賣後買，考慮手續費、最低費用、賣出稅、滑價及 1／1,000 股單位；現金不足時按共同縮放比例降低買進股數，不借款、放空或期末強制清倉。
- 起訖須為所有標的有有效價格的共同交易日；區間內任一標的停牌／缺價即停止，不補舊價、不丟掉缺值標的、不偷偷縮短時間。
- 預設嚴格驗證掛牌身分；價格因子另驗證實際需要的暖機窗。未知空窗、興櫃區段、代碼變更及重複身分會拒絕。明確選擇未驗證身分研究模式時，畫面與結果均標示限制。
- 原始模式不含現金股利再投資，含分割、減資、面額變更或非純現金除權息時須改用具來源權限的還原模式。還原模式同時以原始日線遮罩排除來源可能沿用的合成價格，不把停牌日當可成交日。

回傳資產曲線、每日持倉、委託、訊號日／成交日／權重／分數、費稅、報酬、最大回撤及周轉比。周轉比為雙邊累計成交金額除以初始資金，並非每期平均單邊周轉。

這是固定使用者研究組合的價格因子回測，不聲稱無存活者偏誤或全市場歷史因子排名；尚未加入缺公告時間的財報因子，也不模擬漲跌停排隊、成交量限制、券商逐筆進位及所有企業行動會計。

## 驗證與升版

部署先執行 `uv run alembic upgrade head`，再由操作人員按需初始化／補充身分資料；前端搭配同分支後端。身分登錄表應納入一般資料備份；不要對有使用者資料的資料庫任意 downgrade。

自動測試使用隔離 PostgreSQL、合成傳輸與數列，真實來源及瀏覽器驗證另存只含狀態與數量的摘要；不保存價格、快照或回測結果。付費全市場成功模式是否實證會在驗證摘要單獨列明，不以模擬測試冒充付費來源成功。
