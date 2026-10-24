# 多市場、基本面選股、工作區與投資研究

本輪沿用 `feat/market-research-tools`，提醒通知依使用者決定先跳過。沒有排程、通知寄送或背景輪詢，也沒有建立 PR／合併。

## 目前可用流程

| 功能 | 前端入口 | 後端 |
| --- | --- | --- |
| 上市、上櫃、興櫃、下市櫃參考名錄 | `/instruments` | `GET /api/v1/instruments?market=ALL&q=&page=1&limit=20` |
| 證券身分參考與區間收盤價 | `/instruments/:market/:code` | `GET /api/v1/instruments/{market}/{code}` |
| 多市場技術／營收／法人研究 | `/instruments/:market/:code/history` | `GET …/{market}/{code}/analysis` |
| 多市場進階研究 | `/instruments/:market/:code/research/:topic` | `GET …/{market}/{code}/research?topic=…` |
| 基本面與法人候選篩選 | `/research/factors` | `POST /api/v1/research/factors` |
| 私人研究文件 | `/workspace` | `/api/v1/workspace/items` CRUD |
| 手動帳本與當次估值 | `/portfolio` | `GET /api/v1/workspace/items/{id}/valuation?as_of=YYYY-MM-DD` |
| 單一證券策略回測 | `/research/backtest` | `POST /api/v1/research/backtest` |

所有行情均重新向 FinMind 查詢，來源呼叫依序執行，沒有重試、快取、行情表或舊數值回退。FinMind Token 只設定於後端。所有 API 回應 `Cache-Control: no-store`。瀏覽器離開頁面會取消請求；後端在後續來源查詢前後檢查斷線。

### 市場與身分

固定白名單來源為 `TaiwanStockInfo`、`TaiwanStockDelisting`，每次查詢重新依序取得兩份名錄；支援 4–6 碼大寫英數代碼。市場為 `TWSE`、`TPEX`、`EMERGING`、`DELISTED`。同一代碼保留來源市場／名稱／日期及下市櫃參考；缺少日期（含來源字串 `None`）保留 null。名錄查詢的 meta 標示 `request_scope=catalogue`，`query_start`／`query_end` 為 null，不虛構時間範圍或歷史涵蓋。

來源參考日期不是上市日，也不是完整的歷史市場別時間軸。`market_at_as_of` 永遠不推定，為 null；不同市場參考不保證當日身分。名稱衝突或同日不同市場等無法安全確認身分時，保留名錄供查看，行情研究停止並回報 `INSTRUMENT_IDENTITY_AMBIGUOUS`。沒有建立永久證券 ID，也不將改名／代碼重用的資料自動合併為同一證券。

名錄包含歷史參考，不能把每一列都理解為目前仍交易。來源沒有當期行情時顯示空資料；可自行選擇歷史日期，最多 1,830 天。興櫃開盤欄是來源參考資訊，可能為前日均價；不作可成交開盤價，回測不接受興櫃。

### 基本面／籌碼選股

每次指定 **1–10 檔**候選，並非全市場掃描；空白條件不查詢該項。可設定月營收年增率、毛利率、營業利益率、負債比，以及外資／投信連買筆數。可依任一指標排序；排序指標也會納入查詢需求。比率單位為百分比，法人單位為來源連續可用日期的筆數。不同分類分別呈現，不重複加總。

來源依序查詢，總預算 50 秒。回傳每檔 `ok`、`insufficient`、`error`、`not_requested`，`matches` 為 true／false／null；缺值不當作不符合，失敗不補零。額度／權限／服務受限時停止後續候選，保留本次完成部分，顯示未查詢狀態。收支表採來源單期；負債比為 Liabilities／TotalAssets × 100，分母非正時留空。

回傳資料期間、取得時間及來源。日期不是當時已公告資訊快照，財報可能重編；此功能不得直接當作歷史時點選股回測。法人分類缺漏會中斷連買計數，也不把缺漏的交易日推定為連買。

### 私人研究文件

migration head 為 **`0005_workspace`**，新增唯一一張 `app.workspace_items`，`market` schema 仍無表。

| 文件 kind | 保存的使用者輸入 |
| --- | --- |
| screen | 候選證券、篩選門檻、資料截止日、排序 |
| note | 證券參考、研究文字（最多 10,000 字） |
| chart | 證券參考、MA／布林、RSI／MACD、觀察天數（1–366） |
| comparison | 命名的 2–4 檔不重複證券 |
| portfolio | 最多 500 筆手動帳本、最多 10 檔證券 |

每帳號最多 100 份文件；同帳號同 kind 名稱大小寫不重複。payload 使用 discriminated union 且 `extra=forbid`，拒絕塞入額外行情／查詢結果。只儲存使用者輸入，不保存選股結果、行情、估值或回測結果。

- `GET /workspace/items?kind=…`：列出自己的文件。
- `GET /workspace/items/{id}`：取得自己的文件。
- `POST /workspace/items`：`{"document":{"kind":…, "name":…, …}}`，成功 201。
- `PUT /workspace/items/{id}`：相同 document 加目前 `version`，成功版本加一。
- `DELETE /workspace/items/{id}?version=…`：成功 204。
- 他人文件一律 404；舊版本 409，不覆蓋修改。新增／修改／刪除依 user→document 順序鎖定；唯一性與上限使用真實 PostgreSQL 交易保護。
- 私人 mutation 同樣要求已登入、允許的 Origin、`X-CSRF-Protection: 1`、`application/json`；實際 body 上限 128 KiB。選股／回測只讀 POST 上限 16 KiB，不要求登入。
- 私人頁面通過 RequireAuth 才讀取；請求走 `store.privateRequest`，失效 Session／登出移除私人內容，不使用 localStorage。

篩選文件可重新執行；比較組合逐檔取得最近可用收盤價，標示各自日期；圖表偏好會轉成歷史研究 URL 並套用圖表選項；筆記可開啟該證券研究。

### 帳本、成本與損益

登錄買進／賣出（股數、單價、手續費、稅）、實收現金股利，以及股數調整倍率。日期依序計算，同日保留清單順序。移動平均成本包含買進費稅；賣出依股數比例分攤成本，扣除賣出費稅後算已實現損益；股數調整維持總成本。禁止當時超賣及無持股調整；清倉後剩餘成本歸零。

現金股利單獨列入收入；不自動抓取／入帳，避免重複計入。股數調整可用於分割／配股等數量變動；有返還資本的減資須自行調整原始帳本，本輪未提供完整企業行動會計。持股尚未賣出時依最近 30 天最後可用收盤估值；缺行情時市值／未實現與總損益為 null，不能解讀為零。已實現、實收股利及費稅仍顯示。估值期間若帳本被更新，回 409 要求重新載入。

### 回測假設

單一證券買入持有或 SMA 策略；短均線高於長均線時持有，暖機不足不交易。使用前一有效日收盤訊號、次一有效日開盤成交；查詢前最多一年作暖機，不用同日收盤決定同日開盤。期末持股按最後收盤估值，不強制賣出。

可設定初始資金、手續費率、最低手續費、賣出稅率、滑價及 1／1,000 股交易單位。費率為小數（例如 0.001425）；成本以 Decimal 精確計算，未模擬券商逐筆分進位。買入數量包含最低費用的可負擔限制；會造成負現金的成本設定回錯誤，不自動借款。展示資產曲線、逐筆委託、區間報酬、最大回撤與費稅。

原始價格模式為**不含股利的價格報酬**。暖機至截止區間含分割、減資、面額變更或非純現金除權息時拒絕原始模式，避免產生失真報酬；還原模式需要來源 Backer 權限。還原價格與股數為來源調整單位，不是歷史實際可成交價格。模擬未處理漲跌停排隊、流動性、上市前身分確認及完整存活者偏誤，也不包含跨股因子歷史回測或投資組合配置再平衡。

## 驗證與部署

執行 `uv run alembic upgrade head` 後啟動 API；前端需搭配此分支。此新增表只保存使用者資料，應納入原有帳號資料備份。升版為新增表，既有 API 與資料表保持相容；不要對使用者資料庫任意 downgrade，降版會刪除工作區文件。

自動測試涵蓋來源白名單、名錄缺日期／市場轉換／身分衝突、部分查詢、財報比率、成本損益、超賣、次日成交、費用與單位、企業行動防護、CSRF／body 限制、帳號隔離、競爭更新與上限。真實來源及瀏覽器驗證只保存狀態／筆數等摘要，不保存行情紀錄，見本次驗證 JSON。
