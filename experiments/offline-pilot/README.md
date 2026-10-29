# 離線資源檢索小樣本

用固定的 20 份 CSV／PDF，量測下載、解析與快取重跑，再以真正的 CPU embedding
和獨立 OpenSearch 索引檢查 BM25、kNN、RRF。這是資料準備實驗；不啟動網站、
不切換現役服務，也不使用 GPU。

實驗重用產品的資源處理與 catalog CLI。資料來源、檔案文字、查詢、憑證及量測
留在 Git 外的私有目錄。產品操作細節見
[資源摘要](../../docs/development/RESOURCE_PROFILES.md)及
[向量建置](../../docs/development/VECTOR_PASSES.md)。

## 固定樣本與操作目錄

準備專用 product config，使用新的 `state_root`，以及可唯讀重用的固定 Granite
Embedding 278M 模型。不得沿用現役 product state 或把原 catalog 映像改標成新來源。
catalog 的三個服務使用自己的內部網路與新資料卷，不發布主機 port。

在 Git 外建立 0700 目錄，JSON 使用 0600 權限。以下 `pilot_root` 是該目錄的
絕對路徑；從 repository root 執行指令。

```sh
pilot_root=/absolute/private/offline-pilot
```

保留一份含所選資料集完整 metadata 的 `state/pilot-source.jsonl`，不要為了只抓
一個檔案而刪減資料集的其他 resources。另存 `state/selection.json`：

```json
{
  "schema_version": 1,
  "kind": "selected-real-resource-offline-pilot",
  "resources": [
    {
      "dataset_id": "DATASET_UUID",
      "resource_id": "RESOURCE_UUID",
      "expected_format": "CSV",
      "source_url_sha256": "RAW_SOURCE_URL_SHA256"
    }
  ]
}
```

`source_url_sha256` 是 metadata 原始下載 URL 的 UTF-8 SHA256。
`resources` 須有 20 筆不重複 resource UUID；順序就是處理順序。涵蓋不同大小的
CSV／PDF，並保留空文字、格式誤標等例子。每檔沿用產品的 16 MiB 限制。
這種刻意選取的樣本不能用來推算全站完成時間或排序品質。

## 解析與快取重跑

先以同一份 snapshot 執行 `sync`，將 JSON stdout 保存為私有的
`results/plain-sync.json`。再執行：

```sh
python3 -m experiments.offline-pilot.profile \
  --selection "$pilot_root/state/selection.json" \
  --plain-report "$pilot_root/results/plain-sync.json" \
  --state-root "$pilot_root/state/profiles" \
  --results-root "$pilot_root/results" \
  --source-root "$PWD"
```

`profile.py` 呼叫真正的下載器及 Fess 抽取 helper，記錄逐檔下載／解析時間、大小、
狀態及快取命中。可選的 `--seed-cache` 僅重用既有且通過內容校驗的 acquisition
與 extraction 記錄，保留原取得時間；冷下載與既有快取須分開解讀。

第二次只重跑已取得 profile 的資源，並禁止下載與解析呼叫。確認
`profile-replay.json` 的兩種 cache hits 都等於重跑數，且 `forbidden_io_calls`
均為零。不能把 `completed` 解讀成每個檔案都有可索引文字；逐檔 `empty_text`、
`unsupported_format` 與失敗原因仍須保留。

## 建立隔離索引

讀取 `profile-first.json` 的 `resources[].profile_file`，加上 profiles state 路徑，
明確透過重複的 `sync --profile-file` 封入相同 snapshot。不要 glob 其他 catalog
的 profile，也不要改寫 receipt 中的 manifest。檢查回應的 included／skipped 數量。

接著使用專用 config 執行既有命令：

```sh
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" prepare-index
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" build-catalog-images
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" start-index
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" import-index
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" build-vectors \
  --max-passes 1 --wall-seconds 3600
python3 scripts/depositarai.py --config "$pilot_root/state/operator-config.json" prepare-query
```

逐步保存退出碼、JSON stdout、stderr 與 wall time，前一步失敗時停止。`checkpoint`
需要按原 catalog 的 durable receipts 判斷是否可續跑；不自動重試不確定的 import
或 vector operation。wall budget 須容納 preflight 加完整 helper 額度，不能只填
2,400 秒最小值後假設已執行 embedding。

## 查詢與來源對照

`evaluate.py` 重用產品的 `IndexClient` 與 Haystack RRF。執行環境須有產品依賴，
只連接本次 catalog 的內部網路，並提供以下唯讀檔案：

- `/run/index/binding.json`：本次 `prepare-query` 產生的 binding。
- `/run/index/root-ca.pem`：本次 OpenSearch 的 CA。
- `/run/secrets/index_query_password`：本次 query-only 帳號密碼。

索引以 `opensearch-search:9200` 解析；CPU query embedding 以
`filtered-embedding:11434` 解析，使用相同固定模型與唯讀模型目錄。catalog 的
embedding 只監聽 Fess namespace 的 loopback，不能直接假設它提供此 DNS endpoint。
實驗另行啟動的 CPU query helper 須有自己的容器名稱、資源限制及清理記錄。
不要使用現役 embedding endpoint，也不要掛載 admin 憑證或 GPU devices。

在查詢前固定私有 casefile，不能看到名次後才改測試問題：

```json
{
  "schema_version": 1,
  "cases": [{
    "id": "csv-column",
    "kind": "csv_header",
    "query": "實際欄名",
    "conditions": {},
    "targets": ["DATASET_UUID"],
    "anchor": {"resource_id": "RESOURCE_UUID", "text": "實際欄名"}
  }]
}
```

內容案例使用 `csv_header` 或 `pdf_phrase`，anchor 須出自封存內容且在所有原
metadata 文字中皆不存在。CSV anchor 是完整欄名；PDF anchor 必須在索引的
8,192 字元範圍內，不必出現在卡片前 320 字節錄。
另外加入 `metadata_control`、`time_control`、`format_control`，其 anchor 為 null。
時間與格式條件仍依資料集 metadata 判定，不由 PDF 年份或這次選取的檔案覆寫。

在上述隔離執行環境內執行：

```sh
python3 -m experiments.offline-pilot.evaluate \
  --bundle /absolute/private/enriched-bundle \
  --manifest-sha256 MANIFEST_SHA256 \
  --casefile /absolute/private/cases.json \
  --output /absolute/private/evaluation.json \
  --vector-cache /absolute/private/query-vectors.json
```

報告保留實際 BM25、kNN、RRF 候選 ID、分數、名次、條件判定及原檔／profile
provenance。query vectors 可依模型與 query 身分重用。退出碼 0 表示追蹤完整，
仍須查看 target 名次與未召回項目；它不代表所有查詢都達到品質目標。
metadata-only 對照由相同 sealed corpus 的純投影產生，只證明 anchor 原本不在
metadata，不是另一個索引的排名比較。本實驗不執行 reranker 或文字生成。

完成後移除本次 query helper 與 catalog 容器／網路，保留私有收據、封存資料與
量測結果。只按精確名稱與 ID 清理本次資源，不刪除共享模型或現役資料卷。
