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

API 與 BFF 共用嚴格的回應型別。變更契約時，兩者須協調切換；回退也須選回
對應的 API 與 BFF。不要只更新其中一端，再把回應驗證失敗當成暫時網路問題。

`/v1/version` 的整數 `conversation_contract_version` 目前為 `4`，表示使用目前的
`TurnRequest`／`TurnResponse`，其中資料集卡片必須包含 `temporal_coverage` 與 `resource_context`。
跨主機入口只接受此版本，同時核對固定的 API revision 與 catalog manifest；
舊 API 沒有此欄位時拒絕啟用。
相容的網站更新可以使用不同的 source revision；破壞性 wire 契約變更須同時調整
此版本、BFF 的 `io.depositar.ai.conversation.version` 映像標記及入口驗證。
版本檢查只讀取診斷資訊，不執行模型，也不代替實際搜尋與追問驗收。

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
需要澄清時，使用對話層的 `status`、`message` 與 `reason`；不另在檢索結果保存
澄清或答案物件。是否提供生成式回答等功能，由 `/v1/version` 的功能旗標表示。
每次搜尋都透過 OpenSearch 進行文字及向量檢索，兩路使用相同的時間與格式條件，
再以 Haystack RRF 合併候選。成功套用條件時，`filter_scope` 為 `index_prefilter`；
沒有條件時為 `none`。每路檢索及合併候選都有上限，不能宣稱找齊所有符合條件的
資料。`condition_counts` 只計本輪候選，不能當成整個 catalog 的命中數。

`ranking` 為 `opensearch-haystack-rrf`，已啟用的 reranker 完成重排後為
`nvidia-rerank`。Reranker 故障時保留 RRF 排序並回報降級，呼叫端應查看
`rerank_status`／warnings。查詢不經過 Fess；Fess 負責資料匯入、inventory
與向量管理工作。

每張 card 的 `temporal_coverage` 來自同一 sealed catalog 的資料涵蓋欄位，
與查詢年份無關，也不使用建立或更新日期補值：

```json
{
  "temporal_coverage": {"start": "2020", "end": "2025", "status": "complete"}
}
```

`start`／`end` 為原始精度的日期字串或 `null`，保留年、月或日，不補造缺失端點。
`status` 描述這兩個日期欄位的品質，與本次查詢的 `condition_status` 分開：

| `status` | 含義 |
|---|---|
| `complete` | 兩端日期可解析且順序合理；不代表整個查詢期間都有觀測資料。 |
| `partial` | 僅提供一端有效日期；缺少結束日不代表持續更新至今。 |
| `absent` | 兩端都未提供。 |
| `invalid` | 無法依支援的日期格式判讀，或區間順序不合理；呼叫端應顯示無法判讀，不宣稱涵蓋期間。 |

此欄位為必要契約。採用此版本時，conversation API、BFF 與 frontend 須配套更新；
回退也使用對應版本，不能將新版結果交給舊版嚴格 schema。

每張 card 的 `resource_context` 是必要陣列，最多三筆；沒有選取的可用摘要時為 `[]`。
每筆只有 `resource_id`、`name`、`format`、`url`、`columns` 與 `excerpt`：

```json
{
  "resource_context": [{
    "resource_id": "11111111-1111-4111-8111-111111111111",
    "name": "水質觀測",
    "format": "CSV",
    "url": "https://data.depositar.io/dataset/water/resource/11111111-1111-4111-8111-111111111111",
    "columns": ["eventDate", "pH"],
    "excerpt": ""
  }]
}
```

CSV 僅提供欄名，最多 32 個、每個 128 字元、合計 2,048 字元；不將資料列值加入索引。
PDF 的 `columns` 為空，`excerpt` 是同一份正規化索引文字的前 320 字元。
檔名最多 160 字元，連結固定指向該資料集的 depositar 資源頁，不公開原下載 URL。
欄位與摘錄提供可核對的來源，不能當成模型已證明的命中理由或完整檔案內容。
來源雜湊、抽取器身分與取得時間留在 sealed catalog 的私有 provenance，不送入卡片。
條件篩選仍依 dataset metadata；檔案文字不覆寫時間涵蓋欄位。

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
可搜尋已納入 catalog 的 CSV 欄位及 PDF 文字節錄；不提供檔案內文問答、永久對話記憶或地理距離篩選。
