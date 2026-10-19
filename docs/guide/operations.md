# 部署與維護

目前的正常部署方式是在同一台已備好模型與 GPU 的 Linux 主機上進行私有試用。
產品 CLI 可準備資料、啟動服務、開啟本機入口及匯出交付素材；從零安裝整套產品、
完整產品更新與回退仍未提供。以下命令都在 repository 根目錄，以操作帳號執行。

## 先準備主機與設定

- 準備 Docker、Compose 及 NVIDIA container runtime，操作帳號須能使用 Docker。
- 準備四張互不重疊的 GPU：scope 使用一張、planner 使用兩張、reranker 使用一張。
- 將固定版本的模型放入專用目錄，預留索引、metadata、模型副本及交付包的空間。
- 使用已提交且沒有未提交修改的 checkout；整個準備流程使用同一版本與設定。

從 `deploy/env/product.example.json` 複製設定到 Git 外，調整 `state_root`、三個
模型目錄及 GPU UUID。設定與 state 須由操作帳號擁有；模型目錄可由 root 或操作
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

## 準備搜尋資料

```sh
python3 scripts/depositarai.py --config "$product_config" stage-catalog
```

這一步同步公開 metadata、準備隔離索引、匯入資料並建立向量，尚不開啟網站。
已有相容且完整的 catalog 時會核對後重用，應用程式修改不必每次重新向量化。

若輸出 `status=checkpoint` 且退出碼為2，表示本輪預算已用完；保持相同來源、
設定與資料，重新執行同一命令續跑。只有完整完成後，才進入服務啟動。
需要使用已下載快照時，加上 `--snapshot /absolute/path/datasets.jsonl.gz`。
這會離線重新驗證、建置 metadata，不確認來源網站目前是否仍公開。

Metadata builder 或資料配方變更時，需要新 catalog、重新匯入及建立向量。
既有 state 的 metadata 基準若無法通過當前來源校驗，`--snapshot` 或 `--force`
也不會繞過它；先保留原始快照、收據與服務，安排隔離資料準備及部署交接。
目前命令不會自動遷移這種基準，也不會更新或回退現役 catalog。

## 啟動網站入口

以下命令以 `&&` 串接，任一步失敗便停止後續操作：

```sh
python3 scripts/depositarai.py --config "$product_config" build-images &&
python3 scripts/depositarai.py --config "$product_config" prepare-secrets &&
python3 scripts/depositarai.py --config "$product_config" start-inference &&
python3 scripts/depositarai.py --config "$product_config" prepare-query &&
python3 scripts/depositarai.py --config "$product_config" start-release &&
python3 scripts/depositarai.py --config "$product_config" prepare-gateway &&
python3 scripts/depositarai.py --config "$product_config" activate-gateway
```

這個流程依序建立映像與私有 key、啟動模型、準備唯讀查詢權限、啟動 CPU 網站服務，
最後驗證並開啟 `127.0.0.1:18083`。請在主機上開啟 `http://127.0.0.1:18083`，
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

| 情況 | 處理方式 |
|---|---|
| `doctor` 回報 `blocked` | 依檢查項修正 Docker、GPU、模型或儲存條件，再檢查一次 |
| `stage-catalog` 回報 `checkpoint` | 保持同一來源與資料續跑；不要當成網站已可使用 |
| `failed`、`recovery_required` 或不確定的中斷 | 停止後續步驟，保留錯誤與 state 目錄，先查明失敗原因；不要刪除狀態後重試 |
| 來源變更或目錄／映像不符 | 回到準備流程核對版本；不要手動改寫完成紀錄或套用其他版本的結果 |
| 網頁無法連線 | 確認 activation 已完成、SSH tunnel 仍連線，以及本機18083未被占用 |

`status` 可以查看本地操作狀態，但不能代替網頁或模型健康檢查。
查詢參數時使用 `python3 scripts/depositarai.py --help` 或子命令的 `--help`。
使用搜尋介面請看[搜尋使用說明](using-search.md)；調整程式與文件請看[開發與文件](development.md)。
