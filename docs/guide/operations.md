# 部署與維護

目前的正常部署方式是在同一台已備好模型與 GPU 的 Linux 主機上進行私有試用。
`up-private` 串接資料準備與首次私有網站啟用；主機軟體與模型須預先備妥。
完整產品更新與回退仍未提供。以下命令都在 repository 根目錄，以操作帳號執行。

## 先準備主機與設定

- 準備 Docker、Compose 及 NVIDIA container runtime，操作帳號須能使用 Docker。
- 準備四張互不重疊的 GPU：scope 使用一張、planner 使用兩張、reranker 使用一張。
- 將固定版本的模型放入專用目錄，預留索引、metadata、模型副本及交付包的空間。
- 使用已提交且沒有未提交修改的 checkout；整個準備流程使用同一版本與設定。

從 `deploy/env/product.example.json` 複製設定到 Git 外，調整 `state_root`、三個
模型目錄及 GPU UUID。產品設定固定為 `schema_version: 2` 與
`catalog_storage: "docker-volume"`；OpenSearch 索引放在 Docker daemon 管理的本機
named volume，metadata、配置、憑證及模型另存於私有目錄。
設定與 state 須由操作帳號擁有；模型目錄可由 root 或操作
帳號擁有，須可讀且不可由群組或其他使用者寫入。各目錄須彼此不重疊，不能指向
repository、home 根目錄或 symlink；設定檔設為0600，state 使用私有目錄。
範本的路徑與 UUID 都要替換，不能直接當成可用設定。

以下使用同一個設定路徑：

```sh
product_config=/absolute/path/product.json
python3 scripts/depositarai.py --config "$product_config" doctor --deep
```

`doctor` 核對主機、儲存與模型條件，`--deep` 另外讀取大型模型的 SHA256。
回報 `blocked` 時，先修正列出的條件；它不會代為下載模型或調整主機設定。

## 首次啟用私有網站

```sh
python3 scripts/depositarai.py --config "$product_config" up-private
```

命令在同一設定、固定 clean Git 版本與 product lock 下，先執行 `stage-catalog`，
再串接映像建置、API key、推論服務、查詢權限、私有 release、gateway 準備與啟用。
最後通過網站入口驗收後，才回報 `gateway_active`。

已有啟用紀錄、gateway 目錄或不確定的啟用狀態時，命令會在同步資料前拒絕。
若已用細項 CLI 準備應用映像、API key、推論、查詢或 release，也改由細項命令
診斷及完成；`up-private` 不接管已開始的服務準備流程。
任何一步失敗就停止，保留狀態供調查，不自動修復或回退。這是首次私有啟用入口；
既有網站的更新、回退與公眾 OIDC 入口須另行管理。

若輸出 `status=checkpoint` 且退出碼為2，表示向量預算用完；本輪不繼續建置應用
映像、啟動 GPU 或啟用網站。保持同一來源、設定與 candidate，以 `build-vectors`
直接續跑，完成後依分段流程繼續。重新執行 `up-private` 會先同步 metadata，
範本的 TTL 為0，可能下載新的資料；要重新 staging 同一輸入，使用原本已保留且
身分相符的 `--snapshot`。詳見下方的故障處理。
先前只完成資料準備而尚未啟用網站時，相容且完整的 catalog 可核對後重用，
不必重建向量。

使用已下載快照時：

```sh
python3 scripts/depositarai.py --config "$product_config" up-private \
  --snapshot /absolute/path/datasets.jsonl.gz
```

這會離線重新驗證、建置 metadata，不使用 TTL cache，也不確認來源網站目前是否
仍公開。`--force` 只略過同步的 TTL；`--max-passes` 與 `--wall-seconds` 限制向量
階段，不是整個啟用流程的時限。`--catalog-revision` 可指定資料配方相容的原始
catalog 版本；它不略過相容性或收據檢查。

逐步的 `stage-catalog`、`build-images`、`prepare-secrets`、`start-inference`、
`prepare-query`、`start-release`、`prepare-gateway` 與 `activate-gateway` 仍可用於
診斷及明確的分段操作，參數與前置條件見
[產品 CLI 參考](https://github.com/ThreeMonth03/depositarAI/blob/main/docs/development/PRODUCT_LIFECYCLE.md)。

## 資料重用與配方變更

儲存或 bootstrap 配方不同時，須準備新的 catalog slot，再匯入 metadata 與建立向量。
metadata builder 未變時，可以保留並沿用通過核對的 sealed metadata。
舊 bind 設定使用新的私有 state 準備 schema 2 部署，保留原 state 及收據供交接；
不修改舊 marker、不直接搬移索引或複製 runtime 收據。
完整原則見 [Catalog cache](https://github.com/ThreeMonth03/depositarAI/blob/main/docs/development/CATALOG_CACHE.md#catalog-storage-and-bootstrap-changes)。

Metadata builder 或資料配方變更時，需要新 catalog、重新匯入及建立向量。
既有 state 的 metadata 基準若無法通過當前來源校驗，`--snapshot` 或 `--force`
也不會繞過它；先保留原始快照、收據與服務，安排隔離資料準備及部署交接。
目前命令不會自動遷移這種基準，也不會更新或回退現役 catalog。

## 連線與試用

`up-private` 完成後，請在主機上開啟 `http://127.0.0.1:18083`，
實際搜尋、追問及建立新搜尋，確認使用流程正常。

遠端試用可從自己的電腦建立 SSH tunnel，將 `user@your-server` 換成連線目標：

```sh
ssh -N -L 18083:127.0.0.1:18083 user@your-server
```

保持連線，再於自己的瀏覽器開啟同一網址。這是私有試用入口；提供公眾使用時，
需另外配置選用的 APISIX／OIDC 入口、登入供應者、網域及 HTTPS。
其 `prepare-public-entry`、`up-public-entry`、`update-public-entry`、
`rollback-public-entry` 使用獨立的 `--entry-config`，
管理 CPU 入口及配額，不是整套模型／catalog 的更新工具。

## 打包交接與離線核對

資料與映像準備完成後，指定新的目的目錄及已核對的 Gitleaks 執行檔：

```sh
python3 scripts/depositarai.py --config "$product_config" package-product \
  --destination /absolute/path/packages/product-v1 \
  --scanner /absolute/path/tools/gitleaks
```

目的目錄須尚未存在，其父目錄由操作帳號擁有且為0700。
包內包含來源、metadata、映像及固定模型；真實設定、key與現役索引不在包內。
不要加 `--external-models` 來製作完整交接包，該選項會省略模型內容。

接收者可以核對素材並將來源還原到新目錄；預期版本與 manifest SHA256 應由
交接者另外提供，不從待驗的包自行取值當信任依據：

```sh
python3 scripts/depositarai.py restore-product \
  --package /absolute/path/packages/product-v1 \
  --expected-revision EXPECTED_REVISION \
  --expected-manifest-sha256 EXPECTED_MANIFEST_SHA256 \
  --destination /absolute/path/restored-source
```

`restore-product` 不使用 `--config`，不載入映像或啟動服務；大型素材仍留在原包，
須持續保留。真正移到另一台主機時，仍需重新準備主機與驗證部署。

## 失敗時先看什麼

1. 停止提交後續變更，保留 stderr、使用的 Git revision、私有設定與 state。
2. 先執行下列本地查詢；它不呼叫 Docker、GPU 或線上服務。

   ```sh
   python3 scripts/depositarai.py --config "$product_config" status
   ```

3. 核對 `operations` 與 `recovery_reasons`。有 `started` 收據時，同一 state 的產品
   變更命令都會拒絕，包含 `reconcile-index`；已捕捉的 `failed` 則須依各步驟的
   native 證據判斷。先依[故障診斷與人工停用](https://github.com/ThreeMonth03/depositarAI/blob/main/docs/development/PRODUCT_LIFECYCLE.md#故障診斷與人工停用)
   選擇 checkpoint 續跑、匯入 inventory 核對或人工診斷，不刪狀態後重試。

`status` 區分 catalog 準備（`staged`）、部分應用準備（`partially_prepared`）、
本地啟用已記錄（`active_recorded`）及需要診斷的失敗或中斷（`recovery_required`）。
`active_release` 只顯示核對過的本地啟用身分；`runtime_verified` 一律為 `false`，
不代表網頁或模型健康。沒有本地啟用身分也不表示服務一定停止。

若啟用已發布指標，之後卻無法補寫完成收據，錯誤會保留 `active_changed=true`
並要求恢復診斷；若無法確定指標是否發布，則為 `active_changed=null`。兩種情況都
須核對實際路由；missing pointer 或 `active_changed=false` 也不表示沒有流量。

已停止或重新啟動的容器沒有受支援的產品 restart／reseal 流程。需要隔離故障時，
先核對 ownership、原 Compose 與沒有並行變更，再依上述參考人工停用；
`start-inference`、`start-release` 不能當作 restart 命令。成功的後續操作也不會
清除歷史失敗，`status` 與 `up-private` 仍可能要求診斷。

若 CLI 回報 Docker client 錯誤碼，依
[錯誤碼說明](https://github.com/ThreeMonth03/depositarAI/blob/main/docs/development/PRODUCT_LIFECYCLE.md#共用-docker-client-錯誤碼)
定位失敗類別；服務狀態與恢復方式仍須依 state、操作收據及現場檢查判斷。

查詢參數時使用 `python3 scripts/depositarai.py --help` 或子命令的 `--help`。
使用搜尋介面請看[搜尋使用說明](using-search.md)；調整程式與文件請看[開發與文件](development.md)。
