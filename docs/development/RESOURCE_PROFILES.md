# 準備 CSV／PDF 資源摘要

`profile-resources` 將明確選取的公開資源下載到私有快取，使用固定 Fess 映像的
Tika、CSV 與 PDF 抽取器產生欄位與文字摘要。它是離線資料準備指令，
選取的摘要可再透過既有 `sync`／`stage-catalog` 流程封入新的 catalog，
供文字／向量檢索、reranker 與資料集卡片共用。搜尋請求不觸發下載或解析。

## 執行條件

- 已有目前 builder 產生、通過 manifest 校驗的 metadata-only v3 bundle，以及要處理的 resource UUID。
- 主機可使用 Docker、curl；第一次執行需要取得固定的建置映像。
- 使用 Git 外的專用私有目錄保存素材與摘要；父目錄須先存在。

工具獨立建置一次性 CPU helper，透過內容身分重用映像，不啟動 Fess 爬蟲或
網站服務。Java 編譯在容器建置階段完成，主機不需要 JDK。既有產品包的固定
image 集合尚未包含此 helper，搬到離線新主機時需另外備妥它的建置映像或
已建好的對應 helper，不能只依靠網站產品包。

## 選取資源

以下路徑與身分換成自己的封存資料；同一指令可重複提供 `--resource-id`，
最多 20 個。選取不存在、重複或無法唯一對應資料集的 ID，會在下載及建置前拒絕。

```sh
catalog_manifest_sha256=填入封存的64碼SHA256

python3 scripts/depositarai.py profile-resources \
  --bundle /absolute/private/catalog-bundle \
  --expected-manifest-sha256 "$catalog_manifest_sha256" \
  --state-root /absolute/private/resource-profiles \
  --resource-id 11111111-1111-4111-8111-111111111111
```

這個指令不使用 `--config`，也不讀取現役服務憑證。第一版處理來源宣告為 CSV
或 PDF、位於 `https://data.depositar.io` 的直接公開連結；不接受 query、其他
主機、重新導向或壓縮檔遞迴。每檔完整內容最多 16 MiB，超過時留下失敗狀態。
檔案宣告格式還會交給 Tika 核對，例如 HTML 錯標 CSV 不能當成欄位來源。

CSV 預設以 UTF-8 讀取，明確採用第一列作為欄位候選。這是解析政策，
不能當成工具已證明檔案具有標頭。已知為 Big5 時加上 `--encoding cp950`；
已知沒有標頭時使用 `--header-policy absent`，摘要不會捏造欄位名稱。

## 快取與更新

來源素材與解析結果分開保存。24 小時內的來源快取會核對內容雜湊，
沿用記錄的取得時間，不代表已重新向來源確認。URL、格式、大小或來源修改時間
變更會使用新的來源身分；若需要立即重新下載，加入 `--refresh`。

解析快取另外綁定內容 SHA256、抽取器映像、編碼及標頭政策。只改解析設定時，
可重用原檔；只改其他 metadata 時，也能重用同一份解析結果。損毀或身分不符的
快取會明確失敗，不會直接當成命中，也不會悄悄覆寫既有結果。

`state-root` 下分別保存 `acquisition/`、`extractions/`、`profiles/`，
素材與 JSON 使用私有檔案權限。終端只回傳 dataset/resource ID、狀態、
快取計數及 profile 相對路徑；文字留在對應的私有 profile 中。

## 解讀結果

每份 profile 保留 catalog manifest、dataset/resource ID、素材雜湊及取得時間，
並記錄實際使用的映像與解析政策。每檔下載或解析失敗會留下摘要，其餘資源
繼續處理；批次有失敗時退出碼為 1。

- `ok`：取得可用文字或 CSV 欄位。
- `empty_text`：沒有可抽取的內容。PDF 可能需要 OCR，也可能本來是空白文件。
- `unsupported_format`：實際種類不符合支援的 CSV／PDF。
- `parse_error`／`failed`：解析或素材取得失敗，使用摘要中的固定診斷查明原因。

CSV 只做有界抽取，`rows_observed` 不是完整資料筆數，`rows_complete` 不承諾為真。
文字截斷以 `text_truncated` 標示。欄名、文字及少量資料均不能直接用來覆寫
資料集的時間涵蓋或地理範圍。OCR 與 PDF 儲存格結構仍在獨立實驗階段。

## 封入搜尋 catalog

先用 `sync --snapshot` 固定 metadata，保存回傳的 snapshot 路徑及 metadata-only
bundle manifest。對這份 bundle 執行上述 `profile-resources` 後，把回傳的
`profile_file` 加到 `state-root` 路徑，明確選取要納入的摘要：

```sh
python3 scripts/depositarai.py --config /absolute/private/product.json sync \
  --snapshot /absolute/private/frozen-snapshot.jsonl.gz \
  --profile-file /absolute/private/resource-profiles/profiles/PROFILE_SHA256.json
```

`--profile-file` 可重複，最多 20 份、每個資料集最多三個可用資源。
檔案須為 Git 外私有 0700 目錄中的 0600 canonical JSON，檔名為內容 SHA256。
匯入會核對原 metadata-only manifest、dataset/resource ID、來源 URL 雜湊、
完整下載標記、內容雜湊與抽取契約；不接受把舊 receipt 改標成新 catalog。
空文字、沒有欄名政策或實際格式不符的摘要會列出 skipped 原因，不製造空證據。

CSV 僅索引明確採第一列政策的欄名，最多 32 個、單欄 128 字元、合計 2,048 字元；
任意資料列值不進入搜尋。PDF 索引最多 8,192 字元，卡片顯示其中前 320 字元。
這些文字沿用 metadata 的 HTML、控制字元與 email 清理規則。索引內容的截斷記在
私有 `truncated`；卡片一律以節錄標示，不承諾全文或完整欄位表。

相同參數也可交給 `stage-catalog`／`up-private`。每次同步都須重新明確提供選取清單；
省略時產生 metadata-only catalog，不會從上次快取或 profiles 目錄偷偷沿用。
固定 snapshot 可避免重新下載 metadata；資源與抽取快取也可重用，但 metadata
或 builder 改變後，須對新的 metadata-only bundle 重新產生 profile receipt。

新 bundle 格式為 `depositar-public-metadata-v3`，包含逐資料集的
`resource-context.v1.jsonl.gz`；摘要及 provenance 參與 catalog 身分；公開摘要與索引文字另參與 document 身分。
匯入及向量重建仍走既有隔離候選流程，不能把舊索引改標成新版。
API／BFF／前端使用 conversation contract 4，須與新 catalog 配套部署。
這些指令準備候選；切換現役服務仍由部署流程另外完成。
