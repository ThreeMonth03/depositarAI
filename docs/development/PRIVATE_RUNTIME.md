# 私有 runtime 架構與信任邊界

私有部署將資料準備、GPU 推論與網站服務分開，讓應用程式更新不必重做相容的
catalog，也讓資料準備不直接改動使用者入口。這頁說明元件分工與 runtime 的
信任邊界；首次部署請從[部署與維護](../guide/operations.md)開始，CLI、配置及
操作失敗處理以[產品生命週期](PRODUCT_LIFECYCLE.md)為主。

## 三個平面

| 平面 | 服務與資源 | 責任 |
|---|---|---|
| Catalog | CPU：Fess、OpenSearch、Granite embedding | 準備 metadata、文字／向量索引與查詢權限；新資料使用隔離候選 |
| Inference | GPU：scope 一張、planner 兩張、reranker 一張 | 用途檢查、搜尋條件規劃與候選排序；相同推論配方可跨網站版本沿用既有模型服務 |
| Release 與 gateway | CPU：網站／session BFF、conversation API、query embedding、Nginx | 私有 release 提供查詢；獨立 singleton gateway 管理使用者入口 |

Catalog、inference 與候選 release 均不發布 host ports。正常產品入口由 gateway
綁定 `127.0.0.1:18083`；CPU release 不另啟一套 GPU 模型。服務的 CPU、RAM、PIDs、
mount 與 network 限制來自配置及 Compose，啟動和重用時仍核對實際 Docker 狀態。
這些資源上限不代表實測用量或可承載流量。

## 查詢資料流

```text
瀏覽器 → loopback gateway → 網站／session BFF → conversation API
                                               ├→ GPU scope／planner
                                               ├→ OpenSearch 文字／向量與條件前篩
                                               │       └→ Haystack RRF 合併候選
                                               └→ GPU reranker → 資料集卡片與網址
```

LLM 負責用途檢查與結構化搜尋條件，搜尋結果來自索引及排序服務。此流程不生成
資料內容摘要或保存永久聊天記憶；地理篩選及 PDF／CSV 內文 ingestion 尚未提供。
多輪狀態、篩選範圍與網站 HTTP 契約見[網站參考](../SEARCH_WEBSITE.md)。

所有查詢都走同一條 OpenSearch／Haystack 流程；文字與向量兩路在索引內套用
相同條件，再取得有界候選。Fess 用於資料匯入、inventory 與向量工作，不接收
使用者搜尋。Query embedding 使用與 catalog 向量配方相同的固定模型身分。

資料準備是另一條流程：公開 metadata 經快取與封存後，匯入隔離 catalog 並建立
向量及查詢權限。`stage-catalog` 不下載原始資源檔、不改 active release，也不啟動
網站入口；候選資料完成後，release 與 gateway 仍各自核對並啟用。

## 來源與 catalog 相容性

應用程式映像與 release 綁定目前固定、clean 的 Git 來源；catalog 則保留原本的
`catalog_revision`、metadata manifest、映像及向量紀錄。應用程式版本與資料來源
可以不同，但不能把舊 catalog 重新標成新來源。

重用要求 metadata bundle 與資料生成來源相容，包括模型、chunk／vector recipe、
投影、條件與 ACL，以及原有配置及安全素材；規則見[Catalog cache](CATALOG_CACHE.md)。
重用仍核對 immutable images、實際索引 inventory、vectors、投影及查詢權限。
完成收據不能單獨授權目前的 runtime，資料相容也不會略過失敗的安全檢查。

Compose renderer 只產生配置 plan，不讀憑證、不 build 或啟動服務。
Plan 中的 image tags 不構成 immutable image proof；實際服務須核對 committed
source、固定 image IDs，以及對應的 container、network 與完成收據。

## GPU 資源與 runtime 身分

四張 GPU 以互不重疊的實體 UUID 指定：scope 一張、planner 兩張、reranker 一張。
主機 GPU index 與容器內重新編號都不是穩定的資源身分。

推論部署依固定模型、reranker 完整來源與 GPU 執行配方識別。只改網站時，可重用
已完成的 schema 2 推論紀錄，保留原 image ID、建置 revision 與封存素材；舊 schema 1
runtime 須安排明確交接，不自動接管或改寫收據。詳見[應用映像與推論](PRODUCT_LIFECYCLE.md#應用映像key-與推論)。

Ownership 檢查盤點所有 running containers，包括尚未執行推論的 GPU claims，
並核對 host compute PID。允許共用的既有服務須先證明 exact container ID、
image ID、role 與 GPU UUID；容器名稱或 image tag 不能代替這份證明。

這項檢查遇到未知或不符狀態即拒絕，但只是有界的狀態快照，不是排他資源
reservation。它不能阻止其他管理員在檢查後啟動工作或修改 runtime；模型已存在、
容器 healthy 與 ownership 通過，也不能各自證明 GPU residency 或推論效能。

## Catalog 平台配置

產品 CLI 封存並驗證 Fess／OpenSearch Compose、bootstrap、security templates 與
排程設定。Catalog 共用 `deploy/compose/compose.catalog.yaml`、
`deploy/env/catalog.env.example` 及內部 `scripts/bootstrap-catalog.sh`；
`scripts/validate_catalog_compose.py` 核對配置。Bootstrap 只由產品 CLI 以封存的
product 配置呼叫，不提供另一個獨立啟動入口。
安全與排程範本在 `deploy/config/opensearch/`、`deploy/config/fess/`；
真實安全素材留在私有 state，不在文件複製第二份配置。

OpenSearch 索引資料使用 catalog project 專用的 Docker local volume。建立與重用時
均核對固定身分、driver、labels、初始化收據與實際 mount；不接受任意 driver options
或只憑同名就接管 volume。Metadata、dictionary、模型與憑證仍各自保留私有路徑。

OpenSearch 保持 authentication／TLS，查詢身分與管理身分分開。
ConfigSync 使用所需的獨立索引與持久 dictionary mount，其餘配置唯讀；
不能為了讓初始化或還原成功就停用安全模組。備份及 ConfigSync restore 限制見
[儲存與復原](../MODEL_STORAGE_AND_RECOVERY.md)。

Catalog embedding 與 Fess 共用 network namespace，使模型 API 保持 loopback-only。
主機／Docker daemon 重啟後，須重新核對實際 namespace 與 peers；三個容器各自
healthy 不足以證明這個關係已恢復，不能直接公開 embedding port 作替代。

向量排程平時停用，只在有界管理工作內啟動，結束後核對已停用。
`OLLAMA_NUM_PARALLEL=1` 是單 runner 的模型並行限制；`OLLAMA_MAX_QUEUE=1`
只限制 pinned Ollama 的 cold-load scheduler queue，同模型已載入的請求可能繞過
該 queue。因此 workload 還需由管理鎖、Fess batch concurrency 與 deadline 約束，
不能把 queue 設定當成使用者請求 admission。續跑與失敗處理見
[Vector passes](VECTOR_PASSES.md)及 [Token 清理](../CATALOG_CLEANUP_RECOVERY.md)。

## Catalog 共享與 client lease

Catalog 的三個 core services 仍各自接受 runtime 驗證；以下 peer 數量只指
`search_backend` network，不是 catalog 容器總數：

| 狀態 | 該 network 允許的實際 peers |
|---|---|
| 基本 catalog runtime | Fess 與 OpenSearch，恰好兩個 |
| 具備有效 client lease | 原本兩個 core peers，加一個封存身分的 conversation API，恰好三個 |

Client lease 綁定 catalog、query authority、release source／image 及 API container
身分。Host 在 product／catalog locks 下，驗證來源與收據，收集同一份實際 Docker
snapshot，再核對 core services、API 與相關 networks。

純驗證函式接收到的字典本身沒有授權效力。Network 必須使用完整、未過濾的實際
peer 清單；不能把 API 隱藏成基本狀態，也不能容許未知 helper 或舊 release 留在
網路中。Lease 是查詢 client 的明確接入契約，不能用名稱豁免或單一 healthy 狀態取代。

## 秘密與查詢權限

產品 API key 由產品自己的私有狀態管理，在 product lock 下建立或核對，API 與
web 透過唯讀檔案 mount 使用。Key 及其私有驗證 digest 不輸出到終端或操作報告；
更新應用程式來源或 metadata 不自動 rotate key。產品根目錄的身分與完成收據
也會阻止 key 目錄遺失後靜默再生；未知或部分狀態須保留並交由操作人員處理。

Catalog 查詢身分與管理身分分開：OpenSearch query role 限定一個 physical index。
Fess 管理憑證只供資料準備使用，不提供給 conversation API。查詢準備須核對正常
搜尋與拒絕權限，並重新核對投影及資料身分，才可提供 `query-ready` authority。
權限、密碼或投影的不確定狀態不會自動重建帳號或重送操作；處理方式見
[產品生命週期](PRODUCT_LIFECYCLE.md)。

## 解讀 readiness 與版本

| API 入口 | 可判斷的事 |
|---|---|
| `/healthz` | API process liveness；不要求 API key，不檢查搜尋 backend |
| 帶 API key 的 `/readyz` | OpenSearch 查詢權限、封存索引身分、query embedding 與模型身分 |
| 帶 API key 的 `/v1/version` | 應用來源 `revision` 與資料 `catalog_manifest_sha256` 兩個身分 |

Conversation API 的 readiness 實作：成功結果最多快取30秒，backend 檢查
上限25秒，每個 readiness 實例最多同時執行一個檢查。例外或 timeout 不會當成
成功，也不會寫入成功快取；readiness 的單一檢查限制與一般搜尋 admission 分開。

選用 reranker 失敗時，搜尋 backend 可以仍回報 ready，並以
`reranker_ready=false` 表示 degraded。因此完整產品 activation 必須另外確認
`reranker_ready=true`，不能只接受 `/readyz` HTTP 200。模型 tag／digest 與 readiness
也不代表模型常駐 VRAM、實際推論速度或完整多輪搜尋正常。

## 入口與交付邊界

`start-release` 在私有候選內核對 runtime、模型暖機、GPU ownership、查詢權限、
API readiness、兩輪搜尋與 reset。Gateway preparation 核對已完成的 release 與即時
authority，不建立 release 服務或重跑模型驗收。Gateway activation 再核對接入的私有 website
network 與 exact peers、實際 loopback 搜尋及 reset，最後才發布 active authority。
單改路由檔或取得一項健康結果不足以完成切換。

目前提供這些分階段準備與啟動入口，尚未提供完整產品的 `install`／`update`／
`rollback` CLI。`package-product` 匯出交付素材；`restore-product` 核對素材及還原來源，
新主機安裝、registry identity restore 與完整更新回退仍須另行完成，詳見
[產品包還原](PRODUCT_RESTORE.md)。選用的[公眾入口](PUBLIC_ACCESS.md)有獨立的
CPU 入口維護流程，其 update／rollback 不代表整套產品更新。

舊版本實機量測透過 Git／PR 查閱；目前服務的身分、健康與資源
應依正在執行的 runtime 重新核對。
