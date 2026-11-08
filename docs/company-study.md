# 上市公司整合研究 v1

`GET /api/v1/stocks/{code}/study` 組合既有 TWSE／免費 FinMind 能力，提供同一家公司五種研究視角。公開研究不依賴登入，不開啟資料庫 session，也不寫入行情。前端產品契約見 frontend `docs/product/stock-research-v1.md`。

## 查詢與回應

查詢 `view=overview|financial|valuation|chips|notes`；日期 `start_date`、`end_date` 沿用 analysis 的 1994-10-01 以後、最多 366 日、不得查未來規則，預設近 180 日。額外 query 參數拒絕。

- overview：最新日成交與估值、原始日線、月營收、三表、股利、法人及官方風險／除權息摘要。
- financial：月營收、三表、EPS 與股利；財報查最近三個來源年度，營收比較最多前 60 個月，均另標示實際期間。
- valuation：最新估值、原始價格、歷史 PER／PBR／殖利率、股利及除權息／分割／減資／面額變更。收盤填息觀察重用本次日線，不重複請求價格。
- chips：法人、最新融資融券、官方交易風險與除權息事件。
- notes：只驗證上市成交身分；私人筆記另走本人 workspace API，不將私人內容放入公開回應。

先取得 TWSE 日成交，找不到代碼時停止。其餘依序查詢，FinMind 503 權限／配額錯誤後停止後續 FinMind 呼叫，獨立 TWSE 區塊仍可完成。單區失敗保留成功內容；整體目標時間上限 55 秒，逾時不宣稱完整。取消請求會在來源之間停止，官方公告也逐次檢查連線。無自動重試、背景輪詢、跨查詢快取或舊行情回退。

`quote` 為必要區；其他 section 含 status/data/meta/error。三表等為 `blocks`；`metrics` 為可解釋摘要；`complete=false` 表示本次至少一個已請求區未完成。來源成功但空資料與錯誤分開。分類來源 metadata 為 `classification_meta`；FinMind 同股多個相容產業標籤全部保留。ETF 不請求一般公司三表；金融業毛利／營益率不適用。

頂端成交、最新估值與官方公告是本次來源快照，不能當作研究截止日當時已知資訊。查詢不構成 point-in-time 回測資料。原始日線未還原；付費還原價、大戶、分點、ETF 持股及全市場快照都不是這個入口的必要依賴。

## 財務語意

五個面向為成長、獲利、財務結構、現金流、股利，沒有總分或買賣推薦。毛利率／營益率只以同一期、正營收計算；負債比只用同一期正資產與非負負債。負 EPS 保留、不同季度 EPS 不加總。現金流沿用年初累計並標示，詳細財報仍保留既有相鄰季度換算。缺科目／比較期不補零；最近股利是單筆分派，不能推定全年、連續配息或持股資格。

## 真正的個人觀察清單

使用現有 WatchlistService 與資料表，不增加 migration。每個端點都以登入 session 推導本人，禁止指定 user_id。

| 方法 | 路徑 | 用途 |
| --- | --- | --- |
| GET / POST | `/api/v1/watchlists` | 本人清單／建立，POST `{name}` |
| GET / PUT / DELETE | `/api/v1/watchlists/{id}` | 取得／改名 `{name}`／刪除 |
| POST | `/api/v1/watchlists/{id}/items` | 加入 `{code}`，重新向 TWSE 驗證 |
| DELETE | `/api/v1/watchlists/{id}/items/{code}` | 移除觀察 |
| PUT | `/api/v1/watchlists/{id}/order` | 完整順序 `{codes: [...]}` |

每人最多 10 份、每份最多 100 檔；名稱不分大小寫去重，代碼不重複，排序必須包含清單全部代碼。沿用本人授權、交易鎖及刪除規則；其他人讀寫回傳 404。只儲存使用者清單名稱、證券參考與順序，不儲存價格。

所有寫入須允許的 Origin、JSON 與 `X-CSRF-Protection: 1`，實際 body 上限 16 KiB；回應 no-store。DELETE 不需要 JSON body，但仍須 Content-Type header。私人筆記及命名比較仍使用 `/workspace/items`，更新筆記須提交版本；409 保留前端草稿，重新載入後由使用者合併。
