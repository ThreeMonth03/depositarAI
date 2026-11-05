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

請使用有 Docker 權限的非 root 帳號執行。`run` 與直接解析入口會在處理檔案前
拒絕任何 real／effective UID 或 GID 為 0 的身分，回報
`document_extractor_operator_must_be_nonroot`。解析容器使用呼叫者的 effective
UID／GID，以讀取其擁有的私有素材；不使用 `sudo` 執行。

以下路徑與 SHA256 換成自己的封存資料。`state-root` 使用 Git 外的專用私有目錄，
其父目錄須先存在。所有子命令都不使用產品 `--config`。

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

已知的單一資源拒絕會保留 `failed` 記錄並繼續下一筆，不從計畫分母刪除：

| 來源錯誤碼 | 意義 |
|---|---|
| `document_source_http_failed` | HTTP 請求失敗 |
| `document_source_timeout` | 下載逾時 |
| `document_source_too_large` | 素材超過大小限制 |
| `document_source_empty` | 下載內容為空 |
| `document_source_incomplete` | 下載內容不完整 |
| `document_source_headers_invalid` | 回應標頭不符合來源政策 |
| `document_source_url_unsupported` | 單一資源連結不符合來源政策 |
| `document_source_metadata_invalid` | 單一資源的下載 metadata 無效 |

要重試這些 `failed` 項目時，在相同 `run` 指令加上 `--retry-failed`；待辦項目
優先，每個資源累計最多三次嘗試，單次總量仍受 `--max-resources` 限制。

其他來源錯誤、快取失效或解析工具例外會立即停止這次 `run`，不處理下一筆、
不發布完成集合。資源處理已建立的 `running` 意圖及 helper 身分會保留；
前置檢查失敗不建立新意圖。已完成項目仍保留；先確認儲存狀態、工具與該 helper，
再依正常續跑流程重新驗證。不要把未明錯誤改成完成、刪除執行記錄或直接清除
佇列中的 active intent。CLI 可能只回報安全的通用操作失敗訊息，
不保證顯示每種底層錯誤碼。

未分類的 `document_source_fetch_failed` 也會停止；它可能來自單一站台的 TLS／DNS
問題，也可能來自執行工具或寫入錯誤，目前無法安全地一律當作單檔失敗。解析工具的
例外涵蓋建置、檢查、執行與清理階段，同樣保守停止；正常的解析結果則依下表處理。

下列抽取結果都有 profile，視為已處理，不由 `--retry-failed` 重新解析：

- `ok`：解析完成；是否有可索引欄名或文字，仍由解析政策及匯入報告判定。
- `empty_text`：沒有可抽取文字；PDF 可能需要 OCR，也可能本來是空白文件。
- `unsupported_format`：實際種類不符合支援的 CSV／PDF。
- `parse_error`：抽取器已回報解析失敗；保留診斷以調整後續處理政策。

`parse_error` 計入 `profile_count` 及 `failed_count`，以 profile 身分封入集合；
它與沒有 profile 的下載 `failed` 記錄不同，兩者都保留在原計畫分母。

來源素材與解析結果分開保存。需要取得素材時，24 小時內的來源快取會核對內容
雜湊，沿用取得時間；這不表示已重新向來源確認。解析快取另綁定內容 SHA256、
抽取器映像、編碼及標頭政策。完成計畫不會自動刷新；metadata、選取項目或抽取
政策或抽取器 recipe／映像變更時，建立新的計畫與專用 `state-root`。既有計畫仍綁
原 helper，不能只換映像續跑；舊 profiles 保留其原映像與 provenance。
身分不符或損毀的快取會明確失敗。

素材、profiles 與進度記錄留在私有目錄。每個完整原檔最多 16 MiB；容量需涵蓋
所選原檔、解析快取與 profiles。`run` 會先檢查剩餘空間，空間不足時停止。
終端回傳 ID、狀態、計數與 profile-set 路徑，檔案文字留在私有 profiles。

## 從封存素材衍生新版摘要

metadata manifest 改變時，原 profiles 仍屬於原計畫，不能改寫 manifest 或取得時間
來匯入新 catalog。若要沿用已封存的相同素材，先以新版 metadata-only bundle、
選取清單及相同解析政策 `prepare` 一份新的 target state，再明確指定來源：

```sh
timeout --signal=TERM --kill-after=30s 570s \
  python3 -I -S -B scripts/depositarai.py profile-resources derive \
  --source-bundle /absolute/private/origin/sha256-SOURCE_BUNDLE_ID \
  --source-state-root /absolute/private/origin-profiles \
  --expected-source-plan-sha256 "$source_plan_sha256" \
  --bundle "$catalog_bundle" \
  --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256" \
  --max-resources 20
```

`derive` 只讀取 origin，不下載、刷新快取或啟動解析器。來源的 outcome/profile、
素材雜湊、解析映像／recipe、編碼與標頭政策均須符合；只有通過驗證的 `ok` 或
`empty_text` 可沿用。過期素材保留原取得時間，代表封存內容，並非最新的來源確認。
來源與 target 的 bundle/state 必須是不同且不互相包含的專用路徑。

每份新 profile 發布前，target 的 `derivations/` 會先保存不可覆寫的來源證據。
origin 的 receipts、profiles 及素材不變。缺失、不合格或尚未驗證的項目仍留在
target 計畫分母，之後可用正常 `run` 處理；不會偷偷改走網路或解析流程。

每次最多驗證 20 份、合計 320 MiB 素材；可用 `--max-resources`／`--max-bytes`
調低。`--wall-seconds` 預設且最多 300 秒，在有界 IO 操作之間合作式檢查；
上例外層 `timeout` 在 570 秒送出 TERM，再給最多 30 秒的終止寬限，共配置
600 秒的程序終止預算。這與內層的 300 秒檢查是不同界線。
`checkpoint` 回傳 exit 2，`partial` 回傳 1，
正常完成回傳 0。中斷後重跑相同指令會核對已發布結果，不覆寫既有證據。

回報的 `derived_count` 是本次完成數；`source_candidate_count` 只代表 outcome
可供檢查的候選，`unverified_candidate_count` 尚未驗證素材。`verified_body_bytes`
是實際核對的 bytes；`fresh_acquisitions` 與 `parser_executions` 均為 0。
離線衍生不表示新 catalog、向量索引或搜尋驗收已完成。

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

若目前 metadata builder 已更新，而 profile-set 仍綁定先前保存的 metadata-only
bundle，可明確提供原始 bundle：

```sh
python3 scripts/depositarai.py --config /absolute/private/product.json sync \
  --snapshot /absolute/private/frozen-snapshot.jsonl.gz \
  --profile-set "$profile_set" \
  --profile-baseline-bundle /absolute/private/captured-metadata/sha256-BUNDLE_ID
```

此選項只能搭配 `--profile-set`，manifest SHA256 取自集合原有記錄。
兩份 bundle 都須通過完整驗證；四個 artifact 的全部描述與 manifest 欄位，
除了 `bundle_id` 與 `builder.source_sha256`，必須連 JSON 型別都相同。
結果 receipt 分別記錄目前 metadata baseline 與原 profile baseline 的 manifest
SHA256。封存摘要仍保留舊 profile、manifest、抽取器及取得時間，沒有重新下載或解析。
每次同步，包括 TTL 快取命中，都重新驗證本次明確提供的 baseline；不沿用上次准入。
省略此選項時，仍依目前 metadata manifest 驗證集合。

`partial` 計畫的失敗項目會保留在集合及匯入報告中，未取得內容的資料集仍可使用
metadata。空文字、格式不符或沒有欄名政策的 profile 會列出 skipped 原因。
重試後產生的新集合使用新的內容雜湊；同步時選擇要採用的那份集合。

少量手動選取也可重複使用 `--profile-file`，最多 20 份；它與 `--profile-set`
互斥。相同選項可交給 `stage-catalog`／`up-private`，沿用同一個匯入器。
每次同步都需明確提供選取；省略時產生 metadata-only catalog，不沿用上次的集合。

## 摘要的範圍

抽取器在文字與 CSV 顯示／索引欄名中，把 NUL（U+0000）換成一個 ASCII 空白，
例如 `A<NUL>B` 變成 `A B`，連續 NUL 保留為連續空白。此步驟不改其他 Unicode
或原始檔案及其 SHA256。只有 NUL／空白的 PDF 回報 `empty_text`，並提示可能需要
OCR；不會自動執行 OCR。輸出契約仍拒絕含 NUL 或其他不合法結構的報告，
抽取器例外仍依既有規則停止批次。

每資料集最多封入三個可用資源，超量選取會拒絕。CSV 僅索引明確
採第一列政策的欄名，最多 32 個、單欄 128 字元、合計 2,048 字元；資料列值不進入
搜尋。PDF 索引最多 8,192 字元，卡片顯示前 320 字元，截斷記在私有 `truncated`。
文字沿用 metadata 的 HTML、控制字元及 email 清理規則。

CSV 的 `rows_observed` 是有界觀察數，`rows_complete` 不承諾完整資料筆數。
摘要不覆寫資料集的時間或地理涵蓋；格式篩選仍以資料集列出的全部資源 metadata
為準。OCR 與 PDF 表格結構需另外處理。摘要及 provenance 會封入 v3 catalog；
後續索引與向量重建沿用既有候選流程。

## 開發者整合測試

整合測試用合成 PDF／CSV 執行真實 Java 抽取器，再交給正常的 Python 輸出契約
驗證。主機不需要 JDK；測試會建置或重用 helper 映像，並啟動短暫的 CPU 容器。
請使用有 Docker 權限的非 root 帳號，先取得獨占測試窗口，避免與資源批次並行。

```sh
DEPOSITAR_TEST_DOCUMENT_EXTRACTOR_RUNTIME=1 \
  python3 -m unittest tests.integration.documents.test_extractor
```

未設定環境變數時，這些整合測試會略過；日常 CPU 單元測試不會因此啟動 Docker。
