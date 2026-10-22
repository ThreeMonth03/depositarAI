# 產品 CLI、設定與故障參考

本頁供維護者查詢產品設定、命令的副作用、重用條件與錯誤處理。
首次部署的操作順序見[部署與維護](../guide/operations.md)；元件與網路責任見
[私有 runtime](PRIVATE_RUNTIME.md)。命令由 repository 根目錄，以非 root 操作帳號執行。

已備妥 Docker、GPU 與模型的主機，可用 `up-private` 串接首次私有啟用。
另保留分段準備、啟動、loopback activation 與素材打包命令，供診斷與維護使用。
完整產品的 `install`、`update`、`rollback`、stop／retire 尚未提供；公眾入口有自己的
網站成對更新與人工回退，不能用來更新整套模型、catalog 或私有 release。

## 配置與 GPU 分工

唯一範本：[product.example.json](../../deploy/env/product.example.json)。
複製到 Git 外的私有位置，替換所有路徑與 UUID；不要將真實設定或憑證放入 Git。

| 欄位 | 契約 |
|---|---|
| `schema_version` | 固定為整數 `2` |
| `catalog_storage` | 必填且固定為 `"docker-volume"` |
| `state_root` | 專用產品 state、metadata cache、操作收據及 catalog 準備目錄 |
| `api_model_root` | 已備妥的 Ollama scope／planner 模型快取 |
| `embedding_model_root` | 已備妥的 Granite embedding 模型快取 |
| `reranker_model_root` | 已備妥的 reranker 模型與配置 |
| `gpus.scope` | 一個實體 GPU UUID，供用途／輸入分類 |
| `gpus.planner` | 恰有兩個實體 GPU UUID 的陣列，供搜尋條件解析 |
| `gpus.reranker` | 一個實體 GPU UUID，供候選重排 |
| `cache_ttl_seconds` | 必填整數 `0`–`3600`；填 `0` 關閉 TTL 重用，不是排程間隔 |

JSON 必須恰有上述欄位，不接受重複鍵、額外欄位或以字串代替數字。
四個 GPU UUID 必須互不重疊，且存在於硬體 inventory；容器內的 GPU 編號不能替代 UUID。
設定不會替操作者取得 GPU 獨占權，啟動前仍須確認其他工作的 ownership。

設定檔上限為 16 KiB，須為操作帳號擁有、權限 `0600`、link count 為 1 的 regular file；
拒絕 symlink、FIFO 與讀取期間的檔案替換。載入設定不建立目錄、不產生 key、不改寫 env。

所有 root 使用不含 `..` 的絕對路徑，彼此不能相等或互相包含，也不能與 repository
重疊、指向 `/`、home 根目錄或 symlink。既有 `state_root` 須由操作帳號擁有且為
`0700`；若尚未存在，只允許最後一層缺少，其父目錄也須由操作帳號擁有且為 `0700`。
模型目錄須已存在，可由 root 或操作帳號擁有，不可被 group／world 寫入。
模型檔案的完整大小與 digest 另由 doctor deep check 及啟動命令核對。

### 本機 Docker 索引 volume

OpenSearch 索引資料固定放在 catalog project 專用的 Docker local named volume；
metadata、Fess dictionary、模型、配置與憑證仍使用受保護的私有路徑與 bind mounts。
`state_root` 可放在 home 下的專用子目錄，須符合上述權限與路徑限制。

CLI 建立並核對固定 volume identity；Compose 使用 external volume 與 `nocopy=true`。
不接受任意 volume 名稱、driver options，亦不自動認領同名舊 volume。
首次初始化的 ownership 證據與之後的 runtime 觀察分開：後者核對收據、目前 volume
metadata 與實際 mount，不重新執行 root 初始化，也不掃描索引內容。

local volume 使用 Docker daemon 的本機儲存，不會自動設定 quota 或備份。
容量須另計 metadata、保留版本、catalog 準備時複製的 embedding 模型、映像及交付包。
`doctor` 的 `available_bytes` 是檔案系統可用量，不代表個人或群組 quota 剩餘額度。
後續命令保持同一個 `state_root`；不要搬動目錄或修改收據來認領既有資料。

儲存或 bootstrap 配方變更需要新 catalog slot，重新匯入 metadata 並建立向量；
metadata builder 未變時，原有 sealed metadata 仍可核對後沿用。舊 bind 設定、state
與 runtime 收據不原地遷移；改用本範本及新的私有 state，保留原始資料供交接。
不要修改設定 marker、manifest 或完成收據來認領舊服務；詳見
[Catalog cache](CATALOG_CACHE.md#catalog-storage-and-bootstrap-changes)。

## 現在可用的入口

一般產品命令使用 `--config`；以下路徑是待替換範例：

```sh
product_config=/absolute/private/product.json
python3 scripts/depositarai.py --config "$product_config" doctor --deep
python3 scripts/depositarai.py --config "$product_config" status
```

`doctor` 核對 clean Git source、Docker／Compose、GPU inventory、儲存及固定模型條件。
Compose 至少為 `2.38.0`；NVIDIA runtime 註冊檢查不等於已完成 GPU 推論。
預設核對 manifest、小型配置 digest 與大型 blob 的存在／大小；`--deep` 才讀取大型
blob 並校驗 SHA256。它不安裝軟體、pull／build image 或修復主機；Docker visibility
probe 可能建立有界、無網路／無 GPU 的臨時容器，所需 probe image 必須已在本機。

`status` 只讀取本地 candidate、操作收據及固定位置的啟用紀錄，不呼叫 Docker、GPU
或線上服務。狀態摘要的判讀如下：

| `status` | 本地紀錄的意思 |
|---|---|
| `not_initialized` | 尚無產品 state 目錄 |
| `staged` | 尚無應用準備或啟用紀錄；包含乾淨的 catalog staging／checkpoint |
| `partially_prepared` | 已準備部分應用素材或完成部分啟動步驟，尚無完整啟用紀錄 |
| `active_recorded` | 啟用指標、完成紀錄與成功操作收據的身分相符 |
| `recovery_required` | 有失敗、中斷、不完整啟用或不安全的應用紀錄；先依 `recovery_reasons` 與操作收據診斷 |

`active_release` 只列出已核對的本地啟用身分；若指標與完成紀錄相符，但操作收據
未完成，仍保留此身分並回報 `recovery_required`。`active_release=null` 不表示
gateway 一定停止。所有結果的 `runtime_verified` 都是 `false`，不能當成網頁、
模型或 HTTP health check，也不能據此自動重跑啟用。

### 首次私有啟用

```sh
python3 scripts/depositarai.py --config "$product_config" up-private
```

`up-private` 持有同一 product lock、設定與固定 clean source revision，依序執行：
`stage-catalog`、`build-images`、`prepare-secrets`、`start-inference`、
`prepare-query`、`start-release`、`prepare-gateway`、`activate-gateway`。
它呼叫既有控制器，不另建一套部署配方。成功結果保留 gateway 的實際驗收證據，
並附上應用與 catalog revision 及步驟摘要。

這個命令只處理首次啟用。既有 `active.json`、gateway 目錄、成功啟用歷史或
不完整的操作紀錄都會在同步前拒絕；失敗停止後續步驟，不修復或切換現役服務。
它可重用乾淨的 catalog staging／checkpoint，但不接管細項 CLI 已準備的應用映像、
API key、推論、query 或 release；這些中途流程須以原細項命令診斷及完成。
向量回傳 checkpoint 時，以退出碼2及 `staging_only=true` 結束，不進入應用映像
建置、GPU 或網站階段；以相同來源、設定與 candidate 執行 `build-vectors` 可直接續跑。
重新執行 `up-private` 或 `stage-catalog` 會先同步 metadata；如何固定輸入與繼續後續
步驟，見[故障診斷與人工停用](#故障診斷與人工停用)。
這不是從零安裝主機軟體／模型、完整產品 update／rollback 或公眾 OIDC 入口工具。

參數與 `stage-catalog` 相同：`--snapshot`、`--force`、`--max-passes`、
`--wall-seconds`、`--catalog-revision`。Snapshot 仍是離線建置 metadata 的輸入，
不是跳過建置的 sealed bundle；預算只限制向量階段。以下分段命令仍保留原本的
副作用、前置條件及失敗處理。

### metadata 與候選索引

| 命令 | 用途與副作用 |
|---|---|
| `sync` | 下載或讀取公開 metadata，建立 cache、封存資料包及 candidate；不改 live index |
| `prepare-index` | 封存與 candidate、catalog source 相符的隔離 CPU plan；不啟動服務 |
| `build-catalog-images` | 建置或重新核對 catalog／admin images 與完成收據；不啟動服務 |
| `start-index` | 啟動或核對 OpenSearch、Fess、CPU embedding；可能 build／取得必要映像，不發布 ports |
| `import-index` | 將 candidate metadata 寫入隔離索引；不建立向量 |
| `reconcile-index` | 顯式比對完整 inventory，不重送 import；可能建立有界 helper 與本地收據 |
| `build-vectors` | 在固定預算內建立向量或從乾淨 checkpoint 續跑 |
| `stage-catalog` | 持有同一 product lock，串接 sync、prepare、catalog images、start、import、vectors |

`sync`、`stage-catalog` 與 `up-private` 接受 `--snapshot /absolute/private/datasets.jsonl.gz`，
亦可使用 JSONL；離線快照不發下載請求，也不使用 TTL cache。沒有 `--snapshot` 時，
從固定公開來源匿名下載，完整掃描兩次並確認一致；`--force` 略過設定的 TTL，
不略過下載、資料或 digest 檢查。沒有自動安裝 cron／timer。

首次同步不需要基準；之後預設與前次 staged metadata 比較，該基準不是 active catalog。
只有 `sync` 提供自訂基準，兩個參數必須一起使用：

```sh
python3 scripts/depositarai.py --config "$product_config" sync \
  --baseline-bundle /absolute/private/baseline-bundle \
  --baseline-manifest-sha256 EXPECTED_BASELINE_MANIFEST_SHA256
```

基準是可信的 sealed bundle 目錄，manifest SHA256 須由已核對的紀錄取得，
不能從待驗資料自行取值當信任依據。比對以 dataset UUID 與正規化 document digest
為準；改名或網址改變仍是同一筆資料的修改。

基準會在下載、TTL cache 或離線建置之前校驗。Builder 來源改變時，既有 staged
基準可能不再符合當前 reader；`--force` 只跳過 TTL，`--snapshot` 只選擇輸入，
兩者都不會跳過基準校驗。CLI 沒有略過自動基準或遷移舊基準的選項，須保留原 state
並安排明確的部署交接；詳見 [metadata 配方變更](CATALOG_CACHE.md#when-metadata-generation-sources-change)。

| `diff.status` | 判讀 |
|---|---|
| `initial_snapshot` | 首次同步，沒有比較基準。 |
| `no_search_change` | 正規化後搜尋文件未變；原始 metadata 或 bundle identity 仍可能不同。 |
| `review_required` | 有新增、未出現或變動的文件，先審查差異。 |

`added`／`missing`／`changed` 列出資料身分與變動欄位。`missing` 只表示新快照
沒有該 UUID，不推論已刪除、轉私有或暫時不可見；大量缺漏與筆數異常增長另附
review reasons。所有結果固定 `promotion_allowed=false`，不自動改寫 active index。
兩次完整下載一致也不提供 CKAN server-side transaction；離線快照會標為
`source_consistency=offline_not_checked`。

本版只處理公開 dataset／resource metadata，不下載 resource URL、不解析 PDF 或 CSV
內容。metadata snapshot 不能證明來源現在仍公開；資料移除或轉私有的處理與更新排程
須另行管理。下載、候選準備、線上切換及備份是不同操作。

全新的 `prepare-index` 會由 sealed source 建置 password-hash helper，將固定來源
qualified tag 與 immutable image ID 成對傳給內部 bootstrap helper。操作者使用
產品 CLI，不另拼接 bootstrap／Compose 指令；既有 security material 必須先核對
才能重用，不能手動改 hash 來繞過 provenance。

首次初始化索引 volume 還需要已核對的 catalog-admin image，以及本 slot 封存的
`admin-image.json`。`build-catalog-images` 會準備服務與 admin images 及其收據，
這個準備步驟可能建置映像，不是完全不 build 的安裝流程。
`start-index --prebuilt-images /private/catalog-images.json` 本身不 build 或 pull；
它要求這些 admin 素材已存在，缺少或不符時會在寫入啟動 intent 前拒絕。
三個 catalog services 的映像收據不能取代 admin image 或 volume 初始化證據；
不可從別的 slot 複製收據來補足前置條件。

### 相容重用與向量預算

metadata bundle、manifest、document count 與資料產生來源相容時，`stage-catalog`
可選用已完整完成的 catalog，保留原 `catalog_revision`；當前應用程式仍使用自己的
`source_revision`。詳細相容範圍見 [catalog cache](CATALOG_CACHE.md)。
映像、security material、Compose、runtime、import 與 vector journals 仍會重新核對；
完成收據不是跳過實際 authority 檢查的理由。
相同來源與 bundle 的成功 import 或 reconcile 收據可避免重送 import；
後續向量步驟仍會核對 runtime、完整 inventory 與 journal。
只有相符的成功 import 收據才能跳過重送；已完成 vectors 也須核對 runtime、
inventory、helper cleanup 與完整 journal，才回報本次沒有新增 pass。

資料 recipe 改變會產生新 slot；壞掉的證據、缺少原始 Git objects 或當前 partial slot
不會自動改選其他 slot。只改 host orchestration 不必然重建資料，但必須通過既有
相容性規則。query／release 仍須重新證明實際索引、向量、權限與 client lease。

`build-vectors`、`stage-catalog`、`up-private` 都接受以下參數；它們限制向量階段，
不是整個資料準備或啟用流程的總執行時間：

| 參數 | 預設 | 可接受範圍 |
|---|---|---|
| `--max-passes` | `16` | `1`–`40` |
| `--wall-seconds` | `7200` | `2400`–`36000` 秒 |

每個向量 helper 最多 2400 秒；剩餘預算不足一輪時不啟動新一輪。
正常 `checkpoint` 可以在相同來源、資料及乾淨 journal 下續跑；`partial`、timeout、
未確認 cleanup 或殘留 helper 須先調查，不因 done count 增加就當成可續跑。
`stage-catalog` 不會自動 reconcile 不確定的 import，也不清除失敗 slot。
舊 granular runtime 若沒有 image-stage 收據，仍須核對 runtime，不能事後補造可打包證據。

### 應用映像、key 與推論

| 命令 | 前置條件與結果 |
|---|---|
| `build-images` | 從 clean committed source 建置 conversation API、web、reranker、Nginx；封存 immutable image IDs，不啟動服務或推送 registry |
| `prepare-secrets` | 在 product lock 下建立或核對 `state_root/secrets/api_key`；不輸出 key、不啟動 API |
| `start-inference` | 要求已封存的推論映像、本機 pinned Ollama image 與完整模型；核對 GPU ownership 後啟動或重用三個私有 GPU 服務 |
| `prepare-query` | 要求完整 catalog／vectors；建立條件投影與 query-only 身分，完成正／負權限檢查，不啟動網站 |

產品 API key 供 web 呼叫 API，OpenSearch query identity 則由 `prepare-query`
管理，兩者不可混用。key 使用 operator-owned `0600` 檔案與 readonly mounts；
命令不輸出 key bytes 或其私有驗證 digest。相同配置在 source／metadata 更新時沿用，
不自動輪替。建立前先持久化 intent；
既有 durable identity、操作收據或部分 secret 狀態不能冒充首次安裝。
秘密目錄遺失時先恢復原因與原資料，不刪 identity 來產生替代 key。

`prepare-query` 同時持有 product／catalog lock；確認完整 inventory、vectors、projection
及 runtime 後才授權。OpenSearch query-only role 僅涵單一 physical index；Fess
管理身分僅供資料準備使用。完成前須重新核對索引投影、唯讀權限與資料身分。
未知狀態不自動重建帳號、rotate 密碼或重送不確定操作；query-ready 不等於 client lease。

`start-inference` 不下載模型、不 pull image、不發布 host ports，也不建立 API client
連線。模型存在或 `/api/tags` digest 正確不代表已載入 VRAM；成功回報
`model_warmup_verified=false`，真正暖機由 release／activation 的有界檢查負責。

GPU 部署身分由固定模型、GPU／執行設定及 reranker 的完整來源配方決定。
只改網站時可沿用已完成且配方相同的推論服務；其 image ID、原始來源 revision 與
建置紀錄保持不變，不把舊映像重新標成新版本。模型、reranker 程式或執行配方改變時，
須準備新的推論部署。網站版本更新仍使用當前 CPU app images，catalog 也獨立核對。

新版推論紀錄使用 schema 2。舊 schema 1 runtime 不會自動認領；首次切換須由維護者
安排服務交接，保留原設定與紀錄，不手動改寫收據來接受既有容器。

### 私有 CPU release 準備

`start-release` 要求當前來源的 CPU app images、API key、已核對的 inference，以及相容且完整的
catalog／vectors／query authority。它啟動 web BFF、conversation API、CPU embedding，
核對 immutable images、resource／mount／network identities，再封存 API client lease。
此階段不發布 host ports；未知 peer 不能因名字看似正確就被接受或從 inventory 過濾。

完成前從已核對的 API namespace 執行固定模型暖機，確認 pinned digest、實際推論與
GPU residency，再檢查 API readiness、兩輪搜尋及 session reset；重用也須重新通過。
憑證留在容器內，收據不保存 prompt、搜尋卡片或回應原文。這些檢查用來確認啟動契約，
不能代替搜尋品質、容量或任意輸入的評估。

`prepare-gateway` 要求 `start-release` 已完成；它核對封存紀錄、實際 runtime、
映像、模型檔案、GPU ownership 與查詢權限，再封存路由候選，不重跑暖機、搜尋或
重建服務，也不切換 active pointer。缺少或不完整的 release 不會在此自動補建。
`activate-gateway` 是首次啟用入口：先開啟只提供健康檢查的 loopback bootstrap，
通過實際 runtime、`127.0.0.1:18083` 的 HTTP 搜尋／reset 檢查後才寫入 active authority。
中斷或 partial activation 要求明確
recovery；此命令不是完整產品 update／rollback 介面。

### timeout、readiness 與併發

| 範圍 | 固定界限與含義 |
|---|---|
| metadata sync | 共用 600 秒下載／處理 deadline，不是每頁各有 600 秒 |
| app image build | workflow 4000 秒；單次 build 最多 900 秒，另有命令數與輸出上限 |
| inference／private release | workflow 1800 秒，模型完整 hash 與 Docker 操作共用 deadline |
| gateway prepare／activation | workflow 2400 秒，不是無限等待既有服務恢復 |
| 私有模型暖機 | 共用 300 秒；scope 最多 120 秒、planner 最多 180 秒 |
| conversation turn | `Slots(1)`，每個 API process 一次一個 turn；整輪最多 120 秒 |
| backend readiness | `Slots(1)`，單次 probe 最多 25 秒；只快取成功結果 30 秒 |

process health、backend readiness、GPU residency 與完整 HTTP 搜尋各檢查不同條件。
不要以增加 API workers、重試使用者請求或延長搜尋 timeout 來掩蓋冷啟動／ownership 問題；
每個 process 各有 slot，增加 workers 並不維持全產品單一併發。

### 獨立公眾入口

`prepare-public-entry`、`up-public-entry`、`update-public-entry` 接受
`--entry-config`、`--release-compose`、`--web-image`、`--web-nginx-image`；
`rollback-public-entry` 只接受 `--entry-config`。
這些命令與 `restore-product` 都不使用產品 `--config`，混用時 CLI 會拒絕。

公眾入口接到既有 private release，獨立管理 CPU BFF、Nginx、APISIX、Redis 與 loopback 18084。
網站以同一版本的 BFF／Nginx 映像成對更新，更新期間暫停入口；Redis 配額資料保留。
失敗時由維護者明確回退到前一組網站版本；模型、catalog 與私有 backend 不隨之更換。
設定、OIDC／HTTPS 前置條件、配額及更新用法見[公眾入口](PUBLIC_ACCESS.md)。

## 產品素材打包

`package-product` 需要目前的 metadata candidate、prepared catalog plan，以及完成的
app／catalog image 收據與本機映像。命令不下載新快照、不啟動 catalog 或網站：

```sh
python3 scripts/depositarai.py --config "$product_config" package-product \
  --destination /absolute/private/packages/product-v1 \
  --scanner /absolute/private/tools/gitleaks
```

`--destination` 必須是不含 `..` 的絕對路徑且尚未存在；不能與 repository、state 或
模型目錄重疊。父目錄須已存在、由操作帳號擁有且為 `0700`。
`--scanner` 使用 [private release](SOURCE_PACKAGE.md) 指定的 Gitleaks 與 reviewed
history policy；來源先通過 exact Git／archive／CLI roundtrip，才匯出素材。

| 產物 | 內容 |
|---|---|
| `source/` | clean committed source、Git history bundle 與來源掃描證據 |
| `metadata/` | 已封存 metadata bundle 與 manifest |
| `models/` | 固定模型清單與經完整 hash 核對的內容 |
| `images.tar` | 四個 app images、Fess、OpenSearch、catalog admin、hash helper、共用 Ollama 的映像素材 |
| `catalog-images.json` | 原 catalog revision 的映像收據，供明確選擇本機 prebuilt images |
| `package.json`、`SHA256SUMS` | 固定 schema 的素材身分、來源與 checksum |

包內不含真實 product config、password、API key、host paths、Docker inspect 原文或
已建好的索引 snapshot。`--external-models` 省略模型 bytes 並標為 external；
接收者仍需另備相同模型內容，不能把這種包當完整素材交接。

若重用相容 catalog，package 頂層 revision 是當前 app source；catalog 與
`catalog-images.json` 保留原 catalog revision，不重新標記舊映像來源。
只有 payload、checksum、source 與 image authority 最後核對成功，才發布 `package.json`。
失敗目的目錄保留診斷，不覆寫、不自動刪除或續寫；修正原因後改用新的目的目錄。

使用既有 prepared slot 與本機映像，且已完成本 slot 的 catalog-admin image 與
`admin-image.json` 核對時，可明確避免重新 build／pull：

```sh
python3 scripts/depositarai.py --config "$product_config" start-index \
  --prebuilt-images /absolute/private/packages/product-v1/catalog-images.json
```

該收據須為操作帳號擁有的 `0600` regular file，禁止 symlink／hardlink。
缺映像或 admin evidence、source／project 不符、runtime 或 inspect 不符時直接失敗，
不回退成線上 build。`catalog-images.json` 本身不補建這些前置素材。
從包選用原 catalog revision 時，`prepare-index --catalog-revision EXPECTED_CATALOG_REVISION`
或 `stage-catalog --catalog-revision EXPECTED_CATALOG_REVISION` 仍須通過資料來源相容性檢查。

`restore-product` 不需 product config；預期 revision 與 manifest SHA256 必須從可信管道
另行取得，不能把包內自述當成信任依據。詳細參數、來源還原與大素材保留規則見
[離線還原](PRODUCT_RESTORE.md)。素材包不建立 runtime authority，不 load images 或恢復
索引；`runtime_install_verified=false`、`registry_identity_restore_verified=false`、
`install_complete=false` 明確區分素材齊全與整套安裝完成。

## 退出碼與故障處理

CLI 成功結果以 JSON 寫入 stdout；被捕捉的操作錯誤以安全摘要寫入 stderr。
錯誤摘要不輸出秘密、原始 Docker output 或任意 exception 文字；只有受允許的固定
`error_code` 與 catalog diagnostic 會額外公開，其他錯誤使用 `product_operation_failed`。
參數／缺少設定等 argparse 錯誤使用一般 CLI 用法訊息，不是操作結果 JSON。

啟用指標寫入後，若操作無法完成，會回報 `recovery_required` 與
`error_code=product_activation_outcome_unconfirmed`。`active_changed=true` 表示本次
呼叫已完成指標寫入；`null` 表示曾嘗試寫入，但持久化結果待確認。兩者保留原始
操作紀錄，不自動重跑啟用。這是本次指標寫入的證據；服務與路由狀態仍須另查。

| 退出碼／狀態 | 下一步 |
|---|---|
| `0` | 指定命令完成；`up-private` 成功為 `gateway_active`，單獨的 `vectors-complete` 不代表網站已啟用 |
| `2` 且 `status=checkpoint` | 向量預算正常停止；確認同一來源／資料與乾淨 checkpoint，再續跑 |
| `2` 且只有 CLI 用法訊息 | 修正參數；不可當成向量 checkpoint |
| `1`、`blocked` | 修正列出的主機、模型、儲存或權限條件，再執行檢查 |
| `1`、`failed`／`recovery_required` | 停止後續操作，保留 stderr、state、intent 與 helper/runtime 證據，先查明原因 |
| `1`、`deployment_failed` | 公眾入口部署失敗，未自動回退；確認入口已停止，保留 pending，依[公眾入口指南](PUBLIC_ACCESS.md#更新與人工回退)人工回退或重試首次啟動 |

### 故障診斷與人工停用

先停止提交後續操作，以原設定查詢本地證據：

```sh
python3 scripts/depositarai.py --config "$product_config" status
```

此命令不呼叫 Docker、GPU 或線上服務。保留失敗命令的 stderr、Git revision、
設定與 state 目錄；核對 `operations` 的命令、來源及 `status`，再比對該步驟的
native intent、完成紀錄、容器與網路身分。設定、憑證及原始 Docker 輸出留在私有
位置，不貼入公開 issue。`doctor` 會接觸 Docker、GPU 與模型，不是這個本地檢查的替代。

| 本地證據 | 可以做的下一步 |
|---|---|
| 在建立操作收據與 native intent 前失敗 | 修正參數或前置條件後，再執行原命令。 |
| 已捕捉的 `failed` 操作，沒有 `started` 收據 | 依該命令的 native 證據判斷；只有符合原控制器前置條件的分段命令能繼續，不能直接重跑整套啟用。 |
| 任何 `started` 操作收據 | 同一 state 的產品變更命令都會拒絕，包含 `reconcile-index`；目前沒有解決這類收據的 CLI。保留原狀，安排人工診斷。 |
| inference、release 或 gateway 有不完整的 native intent／完成紀錄 | 原控制器不接管或重建；目前沒有 partial start／activation 的修復入口。 |
| 完成紀錄仍在，但容器已停止或 process 已重新啟動 | `start-inference`、`start-release` 的重用只核對原本仍在執行的 process，不會重啟或重新封存。 |
| activation 失敗或 pointer 寫入結果不確定 | 一起核對 gateway intent、路由與實際容器；missing pointer 或 `active_changed=false` 仍可能已有路由流量。 |

`failed` 是已捕捉的失敗，`started` 則可能是中斷或持久化結果不確定。成功執行後續
命令不會刪除歷史失敗；`status` 仍可能回報 `operation_failed`，`up-private` 也仍會
因歷史 `failed`／`started` 拒絕。改用新的 `state_root` 不是服務隔離方式：相同
bundle／來源仍可能產生相同 Docker project，不能藉此繞過既有 runtime。

#### 向量 checkpoint

正常 checkpoint 的 journal 與 helper cleanup 完整時，保持相同設定、來源與
candidate，直接續跑向量，不重新同步資料：

```sh
python3 scripts/depositarai.py --config "$product_config" build-vectors
```

完成後依原分段流程繼續應用準備與啟用。若仍是乾淨的 catalog-only state，也可
再使用 `up-private`，但它與 `stage-catalog` 都會先執行 sync。範本的
`cache_ttl_seconds=0` 不重用 TTL cache；重新下載可能改變 candidate，因此若要
重新 staging 同一輸入，須使用同一份已保留且身分相符的 `--snapshot`，並核對結果。
Snapshot 仍會重新建置 metadata，不能替代 sealed bundle 或跳過 journal 檢查。

#### 已捕捉的匯入失敗

只有原匯入已被捕捉為 `failed`、沒有任何 product `started` 收據，且原設定、來源、
candidate、catalog runtime 與 native journal 可正常核對時，才使用明確的 inventory
核對入口。先確認沒有殘留的執行中 helper，再執行：

```sh
python3 scripts/depositarai.py --config "$product_config" reconcile-index
```

它可能建立有界的唯讀 helper 與本地收據，不重新匯入 metadata；只有完整且一致的
inventory 才接受為 `status=completed`、`result.status=reconciled-active`。
確認此結果後，才執行前述 `build-vectors`。若 inventory 不完整、helper 未清理或
紀錄不符，維持停止狀態；這個命令不修復部分匯入，也不解決 product `started`
收據。成功 reconcile 保留歷史失敗，不能據此改用 `up-private`。

#### 人工維護停用

目前沒有整套產品 stop／restart／reseal 命令。確實需要先停服務時，由維護者先
確認沒有進行中的產品或 catalog 變更，再核對 persisted Compose、exact project、
容器 ID、映像與 mounts 都屬於這次部署，才使用該 project 的原生 Compose `stop`。
依 gateway、CPU release、inference 的順序停用，可保留 catalog、容器、網路及資料。
獨立前端的 `scripts/search-web.sh down` 不管理這些產品 project。

這是隔離故障的維護動作，尚不是可逆的產品停啟流程。推論、CPU release 與 gateway
的完成證據綁定 process 的 PID／StartedAt；手動重新啟動，即使保留相同容器 ID，
也不會恢復原本的 runtime 證據。恢復服務需另外安排重新驗收與 authority 交接，
不能刪除 intent、改寫收據
或直接執行 Compose up／start 來繞過檢查。

Catalog 核對容器、網路、映像與儲存身分，沒有相同的 process-start 封存規則。
同 ID 的 catalog 容器恢復健康可能通過原驗證；這不會重新驗收下游的推論、release
或 gateway。已有 API lease 時，也不能用 baseline `start-index`／`start-inference`
取代包含 client 的 release 驗證，更不能過濾額外 peers 來通過檢查。

產品操作採 nonblocking lock；同一 state 正在操作時，不另開另一個命令競爭。
product 操作收據最多 128 筆、catalog slots 最多 16 個、向量歷史最多 128 輪，
沒有自動 prune；達上限時先規劃封存與明確退役，不能手動刪 lock／receipt 或
`docker rm -f` 來跳過狀態檢查。
SIGKILL 可留下 started intent；失敗收據不會改寫成成功，也不會套用其他 commit 的完成紀錄。

模型／來源／mount／network identity 不符時回到對應準備步驟調查，不能改 digest 或
放寬 peer 檢查；部分 start、build、query 或 activation 狀態不應直接重試。

### 私有 runtime 驗證錯誤碼

CLI 可回報以下固定 `error_code`，協助定位原 owner 拒絕的原因。
推論與 release 類錯誤碼僅適用於 `up-private`、`start-inference`、`start-release`、
`prepare-gateway` 與 `activate-gateway`。上層控制器若已轉成自己的摘要，CLI 不再
拆解 exception chain；沒有細項碼時仍依失敗步驟與私有紀錄調查。
這些診斷不授權重啟、認領或改寫完成證據。

| `error_code` | 含義與下一步 |
|---|---|
| `catalog_runtime_not_healthy` | Catalog 容器未執行、暫停、OOM 或健康狀態不符；核對實際服務狀態與資源。 |
| `catalog_requires_exactly_three_services` | Catalog 容器數量不等於三個；核對 project 與三個預期服務，不刪容器來補齊條件。 |
| `catalog_runtime_receipt_changed` | 即時容器、網路、映像或完成紀錄不一致；保留原收據，核對部署來源與 ownership。 |
| `inference_runtime_resource_or_health_changed` | 推論的資源或程序健康條件不符；此碼未細分停止、OOM、重啟或資源漂移，須核對私有 runtime。 |
| `inference_runtime_network_peers_changed` | 推論網路的身分、屬性或完整 peer 集合不符；先確認 baseline 或 lease-aware 流程，不隱藏 peer。 |
| `inference_runtime_snapshot_invalid` | Runtime snapshot 結構不符；核對檢查來源與 Docker 格式，不以空集合代替失敗查詢。 |
| `inference_gpu_ownership_blocked` | GPU ownership 檢查拒絕目前主機狀態；核對完整容器、掛載及 GPU 程序快照。此碼不細分拒絕原因，不透傳私有 inspect 輸出；不可過濾容器或放寬檢查來通過。 |
| `inference_partial_state_recovery_required` | 啟動紀錄不完整；保留 slot，不重送啟動或刪除 intent。 |
| `inference_completed_runtime_changed` | 現行 runtime proof 不再等於完成證據；核對容器、網路與 process start，不補寫新的 digest。 |
| `inference_start_identity_changed` | 原啟動身分與目前來源、映像或模型契約不一致；回到原 slot 與來源查明差異。 |
| `release_acceptance_blocked` | 暖機、API 版本、readiness 或兩輪搜尋驗收未通過。容器可能已啟動；保留失敗 slot 與操作紀錄，先查明原因，不直接重試部分完成的啟動。 |
| `release_acceptance_invalid` | 驗收回報的結構或識別資訊不符；核對 operator 與應用介面版本，不放寬欄位檢查或補寫成功收據。 |
| `release_acceptance_probe_failed` | 容器內驗收程式無法執行，或其回報未通過結構驗證；核對映像是否包含驗收相依模組，保留失敗 slot，不補寫成功收據。 |

### 共用 Docker client 錯誤碼

`up-private`、`start-inference`、`start-release`、`prepare-gateway` 或
`activate-gateway` 回報下列 `error_code` 時，可依此表調查。這些固定碼來自私有 runtime
共用的 Docker client；`inference_` 前綴也適用於 CPU release 與 gateway。
上層控制器可能改用自己的錯誤摘要。錯誤碼描述失敗類別，服務狀態與副作用仍須
現場核對；先保留 state 與操作收據，定位失敗步驟，再決定恢復方式。

| `error_code` | 含義與下一步 |
|---|---|
| `inference_command_budget_invalid` | 呼叫端提供的命令數預算不合法。核對工具版本與呼叫端參數。 |
| `inference_client_guard_invalid` | 驗證 callback 設定不合法。由維護者檢查呼叫端程式。 |
| `inference_command_budget` | 本次命令將超過呼叫數上限，或執行前共用 deadline 已耗盡。檢查各步驟的耗時與呼叫累積。 |
| `inference_command_invalid` | runner 回傳型別不符契約。核對 stdout、stderr、退出碼的型別與 runner 版本。 |
| `inference_output_limit` | 單次或累積輸出超出限額。於私有環境找出產生大量輸出的步驟。 |
| `inference_command_timeout` | 命令回傳時已超過共用時限。查明該命令的副作用，再安排後續操作。 |
| `inference_command_failed` | 非零退出，或 runner 發生 `OSError`、`TimeoutExpired` 等例外。此碼未細分原因；從失敗步驟檢查 Docker 連線、權限與私有執行紀錄。 |
| `inference_image_inspection_invalid` | 映像查詢回傳結構異常。核對映像身分與 Docker 輸出格式。 |
| `inference_container_inventory_invalid` | ID 格式、編碼、唯一性或容器數不符合預期。核對該 project 的實際容器與 ownership。 |
| `inference_network_inventory_invalid` | 網路清單結構、名稱、唯一性或數量不符合預期。核對該 project 的實際網路與 ownership。 |

實作入口是 [CLI parser](../../search/operations/cli.py) 與
[operations modules](../../search/operations/README.md)。新增或調整命令時，同步維護此頁的
參數、副作用與退出碼；模組 ownership 與封裝規則見[共用模組與封裝邊界](SOURCE_REFACTOR.md)。
