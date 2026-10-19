# 私有對話搜尋 API

網站後端與其他可信後端以 `POST /v1/turn` 呼叫搜尋服務。瀏覽器使用
[網站 HTTP 契約](SEARCH_WEBSITE.md#瀏覽器-http-契約)，不直接取得這裡的 service key。
完整產品的啟動與路由由[產品 CLI](development/PRODUCT_LIFECYCLE.md)管理。

API factory 是 `search.api.conversation.app:create_app`。條件檢索是內部模組，
不另外提供單輪或 planned HTTP 服務；不接受任意模型、工具或上游 URL。
HTTP 層使用 Django／DRF，非同步工作由 ADRF view 呼叫既有對話與檢索服務。

## 連線與驗證

使用部署設定提供的固定私有 API origin。跨主機連線須經核准的私網 HTTPS；
不要把模型或檢索服務公開來代替 API 入口。每個請求帶上
`Authorization: Bearer <service-key>`，key 只存在可信後端的私有檔案。

| 端點 | 用途 |
|---|---|
| `GET /healthz` | 不需 key，只表示 HTTP process 存活。 |
| `GET /readyz` | 檢查依賴；需 key。模型或 reranker 的狀態須讀取回應，不能只看 HTTP 200。 |
| `GET /v1/version` | 回報來源 revision、catalog manifest、filter scope 及功能旗標；需 key。 |
| `POST /v1/turn` | 執行一輪搜尋或條件調整；需 key。 |

端點使用表列的精確路徑，不加結尾斜線；通過驗證後，其他路徑回404。
JSON 內容以 UTF-8 解碼；只有 POST 接受 body，其他方法的 body 必須為空。

API 不提供公開 Swagger UI；request／response 型別以
[conversation/contract.py](../search/api/conversation/contract.py)為準，
檢索結果型別位於 [retrieval/schemas.py](../search/api/retrieval/schemas.py)。

## 一輪請求

```json
{
  "query": "河川水質觀測資料，只要 CSV",
  "state": {"topic": "", "time": null, "formats": [], "unapplied_time": null},
  "pending": null,
  "context": {"reference_time": "2026-10-16T08:00:00+00:00", "timezone": "Asia/Taipei"},
  "top_k": 5
}
```

| 欄位 | 規則 |
|---|---|
| `query` | 必填，1–512 字元的單一問題；另驗證控制字元與查詢內容。 |
| `state` | 前一輪回傳的結構化條件，首次省略即可；不是聊天 transcript。 |
| `pending` | 前輪待澄清輸入或 null，最多 512 字元；由可信後端保存。 |
| `context` | 當輪的 offset-aware RFC3339 時間與 IANA 時區；相對日期需要可信參考時間。 |
| `top_k` | 整數 1–20，預設 5，表示最多回傳幾張卡片。 |

未知欄位一律拒絕。完整 body 上限為 8,192 bytes；需 `application/json` 與
正確的 `Content-Length`，不接受壓縮、重複 JSON key、NaN／Infinity 或 query string。
讀取 body 最多 5 秒；先驗證 service key，再處理輸入。

時間條件比對資料涵蓋期間的重疊，不是 metadata 修改日。已知不符合的候選不會
補回來湊足 `top_k`；日期資訊不足的候選可能保留並標為 `unknown`。曆法、相對日期
與澄清規則見[搜尋說明](guide/using-search.md)。

## 回應與狀態

回應包含 `status`、`message`、`state`、`reason`、`revision`、`notices`、
`timing_ms`，以及有執行搜尋時的 `search`。`state` 是下一輪可沿用的條件。

| `status` | 呼叫端處理 |
|---|---|
| `ok` | 顯示 `search.cards` 與已套用條件；零張卡片仍可能是正常結果。 |
| `clarification_required` | 顯示問題，保存待澄清輸入；不要自行猜測條件。 |
| `rejected` | 顯示拒絕原因，沿用回傳的既有條件。 |
| `unavailable` | 暫時無法完成，HTTP 503；不要提交這一輪狀態。 |

`search` 包含資料集卡片、解析／套用條件、實際參考時間、排序及降級資訊。
每次搜尋都透過 OpenSearch 進行文字及向量檢索，兩路使用相同的時間與格式條件，
再以 Haystack RRF 合併候選。成功套用條件時，`filter_scope` 為 `index_prefilter`；
沒有條件時為 `none`。每路檢索及合併候選都有上限，不能宣稱找齊所有符合條件的
資料。`condition_counts` 只計本輪候選，不能當成整個 catalog 的命中數。

`ranking` 為 `opensearch-haystack-rrf`，已啟用的 reranker 完成重排後為
`nvidia-rerank`。Reranker 故障時保留 RRF 排序並回報降級，呼叫端應查看
`rerank_status`／warnings。查詢不經過 Fess；Fess 負責資料匯入、inventory
與向量管理工作。

| HTTP | 處理方式 |
|---|---|
| 400／408／411／413／415／422 | 修正格式、長度、欄位或時區；不要原樣重送。 |
| 401 | 檢查後端 key，不把 key 或驗證細節交給瀏覽器。 |
| 429 | 執行額度已滿；尊重 `Retry-After`，避免建立重試迴圈。 |
| 503 | 依賴或本輪工作不可用；保留原本狀態，稍後再試。 |
| 500、連線中斷或無法解析回應 | 記錄 request ID；完成狀態不確定時，由上層開始新搜尋。 |

錯誤 envelope 含 `error.code`、`error.message` 與 `request_id`；回應 header
另有 `X-Request-ID`。不要依賴自由文字判斷錯誤，也不要記錄 service key 或完整輸入。
API 本身不保存 session；網站的版本衝突、到期與 session 所有權由 BFF 管理。

## 呈現與資料權限

所有 title、snippet 與 metadata 都當成不可信文字，以文字 escaping 呈現。
資料集連結只接受核准的 depositar HTTPS 路徑。搜尋使用公開 metadata 快照，
不代表來源目前的 ACL；其他系統整合時，須在顯示前核對資料仍可見。

模型只協助判斷意圖與整理條件。服務不執行任意工具、不生成自由答案，
不提供 PDF／CSV 內文搜尋、永久對話記憶或地理距離篩選。
