# 批次準備 CSV／PDF 資源摘要

`profile-resources` 把明確選取的公開檔案下載到私有快取，再用固定 Fess 映像的
Tika、CSV 與 PDF 抽取器產生欄位及文字摘要。整份計畫可以分批執行、中斷後續跑；
完成後把明確的 profile-set 交給既有 `sync`／`stage-catalog` 流程，封入搜尋 catalog。
搜尋請求不觸發下載或解析。

## 固定 metadata 與資源清單

先用 `sync --snapshot` 固定 metadata，保存回傳的 snapshot 路徑、metadata-only
v3 bundle 路徑與 manifest SHA256。整份計畫都使用這份原始 bundle；後續批次
不改用中間產生的 enriched catalog。bundle 目錄須保留 `sha256-<bundle_id>` 名稱。

把 resource UUID 清單存成 Git 外的私有 JSON 檔。目錄使用 0700、檔案使用 0600：

```sh
install -d -m 700 /absolute/private/resource-input
cat > /absolute/private/resource-input/resource-ids.json <<'JSON'
[
  "11111111-1111-4111-8111-111111111111",
  "22222222-2222-4222-8222-222222222222"
]
JSON
chmod 600 /absolute/private/resource-input/resource-ids.json
```

一份計畫最多選取 30,000 個資源，每資料集最多三個。不存在、重複、無法唯一對應
資料集或不符合來源政策的項目，會在下載前拒絕。只處理清單中的資源；資料集列有
其他檔案，不代表那些檔案也會被解析。

來源須宣告為 CSV 或 PDF，並使用 `https://data.depositar.io` 的直接公開連結。
不接受帶 query 的連結、其他主機、重新導向或壓縮檔遞迴。實際格式會交給 Tika
核對，錯標為 CSV 的 HTML 或壓縮檔不會被當成欄位來源。

## 準備與執行計畫

主機需要 Docker、curl。第一次使用須能取得固定的 helper 建置映像；Java 編譯
在容器內完成，主機不需要 JDK。若搬到離線主機，須另外備妥對應 helper 或其
建置映像；既有產品包的固定 image 集合未包含抽取 helper。

以下路徑與 SHA256 換成自己的封存資料。`state-root` 使用 Git 外的專用私有目錄，
其父目錄須先存在。三個子命令都不使用產品 `--config`。

```sh
catalog_bundle=/absolute/private/catalog/sha256-BUNDLE_ID
catalog_manifest_sha256=填入封存的64碼SHA256
profile_state=/absolute/private/resource-batches

python3 scripts/depositarai.py profile-resources prepare \
  --bundle "$catalog_bundle" \
  --expected-manifest-sha256 "$catalog_manifest_sha256" \
  --state-root "$profile_state" \
  --resource-id-file /absolute/private/resource-input/resource-ids.json
```

`prepare` 封存選取項目、原 metadata 身分、抽取政策與 helper 來源身分；
不下載或解析資料檔。保存回傳的 `plan_sha256`，後續操作都明確提供它。

CSV 預設使用 UTF-8，並把第一列作為欄名候選；這是明確的解析政策，並非自動證明
檔案具有標頭。已知為 Big5 時，在 `prepare` 加上 `--encoding cp950`；
已知沒有標頭時加上 `--header-policy absent`。編碼及標頭政策在計畫建立後固定。

```sh
plan_sha256=填入prepare回傳的64碼SHA256

python3 scripts/depositarai.py profile-resources run \
  --bundle "$catalog_bundle" \
  --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256" \
  --max-resources 20
```

每次 `run` 循序執行，最多實際嘗試 20 個資源；可用 `--max-resources` 調低。
重複相同指令即可繼續處理待辦項目。每筆完成後保存結果，下一次 `run` 從未完成的項目繼續。

只查看進度時使用：

```sh
python3 scripts/depositarai.py profile-resources status \
  --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256"
```

`pending_count` 表示尚待處理的項目；`profile_count` 表示已有封存 profile 的項目。
仍有待辦時狀態為 `checkpoint`；全部結束後為 `completed` 或 `partial`。
`completed` 表示處理流程已結束，是否有可用欄位或文字仍看各項目的狀態。

## 中斷、重試與快取

中斷後再次 `run`，會驗證已完成 profile 並跳過該資源，原始素材快取超過 24 小時
也不會讓同一份計畫重新下載已完成項目。強制終止可能需要重做尚未完成的那一筆；
若記錄中的 helper 尚存在，工具會拒絕繼續。依該筆執行記錄辨識並處理那一個 helper
後，再執行 `run`。

下載或 helper 執行失敗會保留 `failed` 記錄。要重試時，在相同 `run` 指令加上
`--retry-failed`；待辦項目優先，每個資源累計最多三次嘗試，單次總量仍受
`--max-resources` 限制。

下列抽取結果都有 profile，視為已處理，不由 `--retry-failed` 重新解析：

- `ok`：解析完成；是否有可索引欄名或文字，仍由解析政策及匯入報告判定。
- `empty_text`：沒有可抽取文字；PDF 可能需要 OCR，也可能本來是空白文件。
- `unsupported_format`：實際種類不符合支援的 CSV／PDF。
- `parse_error`：抽取器已回報解析失敗；保留診斷以調整後續處理政策。

來源素材與解析結果分開保存。需要取得素材時，24 小時內的來源快取會核對內容
雜湊，沿用取得時間；這不表示已重新向來源確認。解析快取另綁定內容 SHA256、
抽取器映像、編碼及標頭政策。完成計畫不會自動刷新；metadata、選取項目或抽取
政策變更時，建立新的計畫與專用 `state-root`。身分不符或損毀的快取會明確失敗。

素材、profiles 與進度記錄留在私有目錄。每個完整原檔最多 16 MiB；容量需涵蓋
所選原檔、解析快取與 profiles。`run` 會先檢查剩餘空間，空間不足時停止。
終端回傳 ID、狀態、計數與 profile-set 路徑，檔案文字留在私有 profiles。

## 封入搜尋 catalog

沒有待辦或執行中項目時，`run` 會回傳 `profile_set_file` 與 `profile_set_sha256`。
`profile_set_file` 是 `state-root` 下的 `profile-set-<SHA256>.json`；把完整路徑
明確交給 `sync`：

```sh
profile_set=/absolute/private/resource-batches/profile-set-PROFILE_SET_SHA256.json

python3 scripts/depositarai.py --config /absolute/private/product.json sync \
  --snapshot /absolute/private/frozen-snapshot.jsonl.gz \
  --profile-set "$profile_set"
```

profile-set 可匯入超過 20 份摘要，整份集合最多 30,000 項；匯入報告上限為 3 MiB，
展開的 resource-context 總量上限為 128 MiB。超過任一限制會明確拒絕，不截取部分集合。
檔名綁定內容雜湊，
記錄明確的 profile 身分及失敗項目；只讀取同一私有目錄下明列的 profiles。
匯入會核對原 metadata-only manifest、snapshot 來源、dataset/resource ID、
來源 URL 與素材雜湊、完整下載標記及抽取契約，不從快取目錄自行搜尋摘要。

`partial` 計畫的失敗項目會保留在集合及匯入報告中，未取得內容的資料集仍可使用
metadata。空文字、格式不符或沒有欄名政策的 profile 會列出 skipped 原因。
重試後產生的新集合使用新的內容雜湊；同步時選擇要採用的那份集合。

少量手動選取也可重複使用 `--profile-file`，最多 20 份；它與 `--profile-set`
互斥。相同選項可交給 `stage-catalog`／`up-private`，沿用同一個匯入器。
每次同步都需明確提供選取；省略時產生 metadata-only catalog，不沿用上次的集合。

## 摘要的範圍

每資料集最多封入三個可用資源，超量選取會拒絕。CSV 僅索引明確
採第一列政策的欄名，最多 32 個、單欄 128 字元、合計 2,048 字元；資料列值不進入
搜尋。PDF 索引最多 8,192 字元，卡片顯示前 320 字元，截斷記在私有 `truncated`。
文字沿用 metadata 的 HTML、控制字元及 email 清理規則。

CSV 的 `rows_observed` 是有界觀察數，`rows_complete` 不承諾完整資料筆數。
摘要不覆寫資料集的時間或地理涵蓋；格式篩選仍以資料集列出的全部資源 metadata
為準。OCR 與 PDF 表格結構需另外處理。摘要及 provenance 會封入 v3 catalog；
後續索引與向量重建沿用既有候選流程。
