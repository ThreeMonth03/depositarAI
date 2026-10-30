# 共用模組與封裝邊界

本頁說明共用邏輯的歸屬、依賴方向，以及搬移來源時要一起檢查的封裝。
目前只維護一套產品；呼叫端直接使用現行模組，不保留舊路徑轉接、第二份實作
或只供舊測試 patch 的 alias。

## 共用邏輯放哪裡

| 責任 | 來源 |
|---|---|
| 瀏覽器存取確認、搜尋 session、訊息與錯誤恢復 | `web/src/search/session.ts` 的 `useSearchSession` |
| 搜尋畫面與 assistant-ui 訊息轉換 | `web/src/main.tsx`；結果與進度由各自元件呈現 |
| 瀏覽器 HTTP 型別、傳輸與錯誤分類 | `web/src/api.ts` |
| 產品 CLI 參數、dispatch、診斷與退出碼 | `search.operations.cli`；執行入口為 `scripts/depositarai.py` |
| 對話與條件狀態 | `search.api.conversation` |
| 條件檢索、篩選與排序 | `search.api.retrieval` |
| 搜尋結果卡片、OpenSearch 查詢與 Haystack RRF | `search.api.retrieval.results`、`index`、`pipeline` |
| 搜尋用途分類 | `search.scope` |
| 固定模型身分與 request 限額 | `search.model_policy` |
| API strict model、admission、設定及 worker IO | `search.api.shared` |
| 私有 peer 與模型 role／endpoint 政策 | `search.api.shared.peers`、`model_transport` |
| 有界 HTTP request／response／transport | `search.transport.http` |
| 純 JSON 解析、私有 service-key 檔案讀取 | `search.serialization`、`search.secrets` |
| 已建立 process 的雙 pipe 擷取與清理 | `search.operations.shared.bounded_process` |
| Runtime Docker client、完成映像證據 | `search.operations.deployment.runtime_docker`、`images.authority` |
| 共用模型候選、catalog 權限與私有批次擷取 | `search.operations.deployment.candidate` 的明確子模組；`batch.execution` 使用 `search.quality.contract` |
| 離線 metadata 正規化、封存及 builder CLI | `search.catalog.build` 的明確子模組；入口為 `python3 -m search.catalog.build` |
| 封存資料與精確 inventory | `search.catalog.bundle`、`inventory` |
| 純查詢文字正規化與輸入／結果數限額 | `search.catalog.query` |
| Catalog 向量與 query 共用的 embedding 模型身分 | `search.catalog.embedding` |
| 共用 Fess session／transport 與管理 token 生命週期 | `search.catalog.session`、`transport`、`admin` |
| 匯入與向量工作 | `search.catalog.importer`、`vectors`、`journal` |
| Catalog 命令及 stdout 驗證 | `search.operations.catalog.commands` |
| Metadata／catalog 私有收據 IO | `search.operations.metadata.storage` |

跨 domain 使用明確的 module import。`__init__.py` 保持輕量，不在 import 時讀
key、操作 Docker、啟動模型或載入 HTTP app。所有 package initializer 只放文件；
HTTP 介面直接匯入 `search.transport.http`，配置直接匯入
`search.operations.config`，其餘直接匯入所屬子模組。

## 依賴方向

- 前端畫面 → `useSearchSession` → HTTP client。搜尋狀態不依賴 assistant-ui；
  畫面將 `SearchMessage` 轉成套件型別，不複製 session 版本或錯誤恢復邏輯。
- Browser → BFF → conversation → retrieval → workers／adapters。HTTP 請求不載入
  deployment 或 catalog orchestration。
- Retrieval 對 OpenSearch 執行相同條件的文字與向量查詢，再使用 Haystack RRF
  合併候選。Fess administration、inventory 與向量工作留在 catalog 準備流程。
- CLI／lifecycle → operations domain → shared primitives。共用 Docker client 與
  完成映像 reader 不反向依賴 inference、控制器或 GPU ownership。
- CLI 在選定命令後載入對應 domain；help／參數解析不載入產品配置、Fess 或部署
  控制器。測試直接匯入 `search.operations.cli`。
- Planning、transport、serialization 與 key reader 不依賴 app factory、operator
  lifecycle。診斷 peer probe 不為了解析位址而載入 worker dispatcher。
- Metadata sync → `search.catalog.build` 的正規化、snapshot 與 builder owners；
  builder 不下載資料或操作 Docker。`search.catalog.bundle` 負責 reader 校驗，
  query client 不透過 builder 取得封存資料。

責任與測試入口見 [search](../../search/README.md)、[API](../../search/api/README.md)
、[catalog](../../search/catalog/README.md)及 [operations](../../search/operations/README.md)。

## 相似的 IO 是否能共用

先確認信任邊界與錯誤契約相同，再抽出共用邏輯。Strict JSON parser 只負責解碼；
檔案所有權、大小、state schema 與錯誤分類仍由各 domain 管理。

Catalog commands 使用過濾後的 operator environment、文字 capture 與 UTF-8
stdout 上限；runtime Docker client 使用固定環境、binary output 與命令預算。
不能只因兩者都呼叫 subprocess 就合成一個帶很多開關的 runner。
`bounded_process.captured_pipes` 只接管已建立 process 的擷取、byte cap 及清理，
Popen、umask、最後 wait 與整個流程的預算留給呼叫端。API worker 有自己的契約。

## 映像與 catalog

| 修改類型 | 一起核對 |
|---|---|
| App runtime 模組 | Source inputs、Docker COPY、專屬 ignore、reviewed recipe hashes 及 image 內 import |
| Host operator 模組，包括 `search.operations.cli` | 留在操作主機；不要為共用方便加入 API／web runtime image |
| 測試 helpers | 留在 tests，不進產品 runtime source recipe |

每個映像只帶自己的來源；不要整包 COPY `search/` 來解決缺檔。
API 使用一份 Dockerfile，預設 production target；tests target 與測試 lock 不進
封存的產品 build context。

Catalog 資料身分與應用版本分開管理。`catalog.backend` 的 `DATA_FILES`、
`STAGED_FILES`、`BUILD_FILES` 及向量 recipe 決定能否重用；不能把既有資料重新
標成新的 app revision。這不是維護舊產品版本：資料來源仍保留其原始 provenance。

資料配方綁定的來源改動可能要求重新匯入或向量化。Catalog package、匯入入口、
Docker COPY 及 recipe identity 必須同步更新；不保留舊 hash 白名單或 module alias
來繞過相容性校驗。資料內容與原始 provenance 仍分開保存，詳見
[Catalog cache](CATALOG_CACHE.md)。

Metadata manifest 的 `builder.source_sha256` 由 `search.catalog.build.identity`
綁定明列的 builder 模組、package initializers 與 catalog policy，不只 hash
執行入口。拆檔或改動這些來源時，同步處理 reader 校驗、資料配方與來源封裝；
原始快照可供重新建置，但不能把既有 bundle 改標成新的來源身分。

## 搬移與測試

1. 選定唯一 owner，保留拒絕條件、執行順序、錯誤與 cleanup 行為。
2. 同步修改實際 consumers、CLI、測試、映像輸入與文件，刪除舊入口。
3. 執行受影響的 lint、行為／依賴測試及必要的 image-import 檢查。

可重用資料放在 `search/tests/fixtures.py`，BFF doubles 放在
`search/tests/web_support.py`；operator image 與 catalog volume fixtures 分別在
`tests/unit/deployment/images/fixtures.py`、`tests/unit/catalog/volume_fixtures.py`。
由使用它的 test 管理 assertions 與 cleanup，不借用另一個 `TestCase` 當 setup。

目錄規範見 [repository layout](REPOSITORY_LAYOUT.md)，日常檢查見
[開發指南](../guide/development.md)。
