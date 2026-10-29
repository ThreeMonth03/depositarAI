# 量測離線資源處理容量

這個實驗用固定抽樣估計循序下載與解析 CSV／PDF 的時間，並分開量測計畫續跑及
最終 catalog 建置。使用現有 `profile-resources` 流程，不改解析政策或現役服務。
產品操作方式見 [資源摘要指南](../../docs/development/RESOURCE_PROFILES.md)。

## 固定比較範圍

先從封存 metadata 篩出符合現有來源政策、已知大小不超過 16 MiB 的 CSV／PDF；
大小未知者保留，下載時仍受 16 MiB 限制。每個資料集以固定雜湊選至多三份，形成
本次實驗的目標母體。這項選法只用於量測，沒有變更產品選取政策。

再按格式及宣告大小分層，固定選取 CSV 32 份、PDF 128 份。各非空大小層先保留
兩個樣本，其餘名額按母體比例分配；選取以固定 seed 的雜湊排序。輸出保留每層
母體數、抽樣數與展開權重。CSV 刻意多抽，估計母體時須使用權重。

抽樣在下載前封存，不因失敗、空文字或耗時而換檔。先前已知的異常檔可以另外做
回歸測試，但不能混入這次的加權估計。全程維持預設 UTF-8／首列欄名政策，
不以人工修正的編碼掩蓋失敗比例。

## 執行抽樣與量測

從 repository root 執行，主機須能執行既有資源摘要 CLI。所有資料、狀態及結果放在
Git 外的專用目錄；目錄為 0700，檔案為 0600。以下變數需換成自己的絕對路徑與
SHA256。輸入為同一份封存 catalog 的原始 `source.jsonl.gz` 和正規化 `corpus.jsonl.gz`。

```sh
python3 -B -m experiments.offline-capacity.sample \
  --source "$frozen_source" --source-sha256 "$source_sha256" \
  --corpus "$frozen_corpus" --corpus-sha256 "$corpus_sha256" \
  --output "$selection_root"

python3 -B scripts/depositarai.py --config "$operator_config" sync \
  --snapshot "$selection_root/pilot-source.jsonl"

python3 -B scripts/depositarai.py profile-resources prepare \
  --bundle "$catalog_bundle" \
  --expected-manifest-sha256 "$catalog_manifest_sha256" \
  --state-root "$profile_state" \
  --resource-id-file "$selection_root/resource-ids.json"
```

保存 `sync` 回傳的 bundle 與 manifest，及 `prepare` 回傳的 plan SHA256。量測
Phase A 前四個資源狀態子目錄必須為空；A、B 各處理四批、每批 20 份，兩個 phase
使用不同 Python process。最後重播終態計畫，確認沒有再次下載、啟動解析器或建置映像。

```sh
python3 -B -m experiments.offline-capacity.measure \
  --bundle "$catalog_bundle" --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256" --phase A \
  --output-dir "$results_root"

python3 -B -m experiments.offline-capacity.measure \
  --bundle "$catalog_bundle" --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256" --phase B \
  --output-dir "$results_root"

python3 -B -m experiments.offline-capacity.measure \
  --bundle "$catalog_bundle" --state-root "$profile_state" \
  --expected-plan-sha256 "$plan_sha256" --phase replay \
  --output-dir "$results_root"
```

量測包裝器呼叫原有處理函式，保存下載、解析、profile 發布、批次 wall time 與
追蹤檔寫入時間。終態重播把下載及解析入口換成拒絕呼叫的檢查。記錄包含 ID、
雜湊、狀態與時間，不輸出來源 URL 或抽取文字。既有 phase 結果不能覆寫；
量測程序意外中斷時，保留原始紀錄並另行說明，不能把混合快取續跑當成首次量測。

## 完成後建一次 catalog

160 個項目都到達終態後，將回傳的 profile-set 與同一份抽樣 snapshot 交給 `sync`。
再依既有 CLI 執行 `prepare-index`、`build-catalog-images`、`start-index`、
`import-index`、`build-vectors` 及 `prepare-query`，各自保存原始回應及 wall time。
使用專用 catalog slot、內部網路及 CPU embedding，不啟動第二套線上推論服務。
不要每處理 20 份就重新封存、匯入或建立向量。

```sh
python3 -B scripts/depositarai.py --config "$operator_config" sync \
  --snapshot "$selection_root/pilot-source.jsonl" --profile-set "$profile_set"
```

建置前提交實驗程式，之後保持來源版本不變，讓映像及操作收據綁定實際使用的
commit。保留量測 phase 原本記錄的 source revision 與 runner SHA256；不要把
前後兩階段重新標成同一版本。完成後依專用 runtime 的確切 ID 清除容器及網路，
保留 catalog、模型與私有量測資料供後續比較。

## 解讀結果

完成兩個 phase 後執行加權分析。`state-dir` 指抽樣檔所在目錄，其 `profiles/`
子目錄為上述 `profile_state`；期望雜湊取自抽樣輸出、prepare 與原始 phase 收據。
分析會核對全部樣本、phase 與終態記錄，拒絕缺少或重複的項目。

```sh
python3 -B -m experiments.offline-capacity.analyse \
  --state-dir "$selection_root" --results-dir "$results_root" \
  --expected-selection-sha256 "$selection_sha256" \
  --expected-population-sha256 "$population_sha256" \
  --expected-plan-sha256 "$plan_sha256" \
  --expected-runner-sha256 "$runner_sha256" \
  --output "$results_root/statistics.json"
```

「冷快取」指這份計畫的本機素材與解析快取為空；來源網站或 CDN 的快取不受控制。
同一份已完成計畫的重播主要驗證進度與既有 profile，不代表跨計畫或跨 catalog 都
能省下相同比例的時間。映像及 embedding 模型預先備妥，其取得成本需另記。

下載和解析是每筆處理時間的子項，不能與整筆時間重複加總。估計目標母體時，使用
各層母體數乘上該層平均處理時間；失敗、逾時與空文字都留在分母。批次驗證及其他
固定成本分開報告。少量未知大小樣本和來源網站負載可能造成長尾，點估計不能當成
完成期限保證。

catalog／向量時間另外報告。抽樣 catalog 的 metadata 數、文字量及 chunk 數與
完整 catalog 不同，不能直接按資源數倍乘。這個實驗也沒有涵蓋超過大小上限的
檔案、其他來源網址、OCR、CSV 每筆資料值或完整 PDF 全文索引。
