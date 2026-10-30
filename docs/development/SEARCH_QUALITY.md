# 搜尋正確性與排序檢視

使用 `scripts/check_search_quality.py` 離線核對已取得的搜尋回應。工具重用產品的
日期與條件判定，不呼叫 HTTP、模型或 Docker，也不建立索引。適合在修改對話、
metadata 或排序程式前，保存同一組查詢的可重跑基準。

共用的計畫驗證、條件正規化與 digest 位於 `search.quality.contract`，
回應核對位於 `search.quality.evaluator.evaluate`。CLI 只負責有界檔案讀取、
參數與 JSON／退出碼；批次工具直接使用 domain 契約，不依賴 CLI 模組。

## 固定查詢集

[search-quality.json](../../tests/fixtures/search-quality.json) 是可重複使用的案例計畫，
只有查詢及預期行為，沒有模型回應或人工相關性分數。涵蓋多輪年份／格式覆寫、
獨立清除條件、明確換主題、單側日期、民國紀年、無效日期、澄清及拒絕後繼續搜尋。

先檢查計畫，再將同一份計畫與實機擷取結果一起核對：

```bash
python3 scripts/check_search_quality.py --plan tests/fixtures/search-quality.json
python3 scripts/check_search_quality.py \
  --plan tests/fixtures/search-quality.json --input /private/search-capture.json
```

第一個指令只驗證計畫格式，回報 `plan_valid` 與 `plan_sha256`；不代表模型通過。
第二個指令綁定 case ID、輪次、實際查詢及預期行為。漏掉 case 或尾端輪次會保留在
報告中，列為 `turn_not_captured`，不從分母消失；改寫查詢／預期或多出未規劃輪次
則拒絕輸入。計畫摘要使用排序 JSON key 的 UTF-8 內容計算，不受縮排影響。

每個 case 建立新 session，依序送出各輪，最後清除 session。查詢失敗時保存原始
失敗結果，尚未執行的輪次留缺；不要悄悄重試或把後續輪次往前遞補。跨版本比較
使用相同計畫摘要、catalog manifest、模型及 request policy，並另外保留來源版本。
人工判讀後可以修正不合理的預期，但須更換計畫摘要、重跑兩邊，不能只修改一邊。
更新現行案例檔後，既有擷取仍須搭配當時保存的計畫；新的計畫摘要需要重新擷取。
不能改寫舊擷取中的 `expected`，將原有結果套到修正後的預期。

也可以只核對一份自行擷取的回應：

```bash
python3 scripts/check_search_quality.py --input /private/search-capture.json
```

JSON 報告寫到 stdout。退出碼 `0` 表示計畫格式有效或回應檢查通過，
`1` 表示有不符或無法核對項目，
`2` 表示輸入格式錯誤。每個 issue 都需要按回應、來源資料與部署版本定位，不能
只因一項失敗就斷言是搜尋演算法有 bug。

## 合併多批擷取

`scripts/check_search_batches.py` 先核對各批收據，再使用同一套 evaluator 評估完整
題組。輸入為完整 plan、各批原始 subplan，以及包含 `run.json`、`capture.json`、
`outcomes.json` 的私有目錄；一個 case 的完整對話只能屬於一批。

```bash
python3 scripts/check_search_batches.py \
  --plan /private/full-plan.json \
  --batch /private/batch-a.json /private/capture-a \
  --batch /private/batch-b.json /private/capture-b \
  --output /private/quality-report.json
```

輸入及輸出目錄須為目前使用者擁有的 `0700` 目錄，輸入檔須為 `0600`，路徑使用
絕對路徑。輸出必須是新檔，工具不覆寫既有報告。一般 stdout 僅含狀態、摘要與計畫
digest；詳細報告含 case、卡片與來源身分，寫入指定的 `0600` 檔案，勿提交 Git。

工具核對檔案 bytes／SHA256、實際 query／expected、outcomes 計數、來源／catalog／
模型／policy 的完整身分，以及回應的 revision／manifest。預設採第一份身分已驗證
的收據作為 baseline；也可用 `--baseline /private/identity.json` 明確指定完整身分。
摘要與私有檔案權限無法證明收據作者身分；仍須由受控擷取流程保存證據。

| 批次證據 | 評估方式 |
|---|---|
| `complete` 且 `identity_verified: true` | 納入 evaluator；品質斷言可以通過或失敗。 |
| `failed`、`quarantined` 或未提供的批次 | 整批不採用卡片，保留完整題組分母；即使部分查詢成功也不當成有效證據。 |
| 混用身分、改寫計畫、重複 case、缺檔或 hash 不符 | 拒絕輸入，不產生可用報告。 |

`evidence_missing_turn_count` 計算缺少可信證據的輪數，
`quality_failed_turn_count` 只計算可信回應的斷言失敗，避免把執行失敗歸因於模型。
報告狀態為 `passed`／`failed`／`incomplete`；退出碼分別為 `0`／`1`／`1`，
拒絕輸入為 `2`。沒有提供的批次可以留缺；已提供的批次則必須帶齊固定三個檔案。

## 準備輸入

輸入是 `schema_version: 1` 與 `cases` 陣列；每個 case 包含唯一 `id` 與 `turns`。
一個 case 代表從新 session 開始、按順序送出的完整對話，BFF 回應的 `version`
應從 `1` 逐輪遞增。澄清與拒絕也是 HTTP 200 的一輪；服務不可用的 HTTP 錯誤
不增加版本，不能當成預期成功。每輪必須包含：

| 欄位 | 內容 |
|---|---|
| `query` | 當輪實際送出的文字，使用產品的 512 字及文字驗證；不以正規化後文字替換計畫中的原文。 |
| `expected` | 必填 `{time, formats, retain_topic}`；time 是 `{start, end}` 或 `null`，formats 是格式陣列，retain_topic 表示應保留上一輪已提交的主題。另見下表。 |
| `response` | BFF `/api/turn` 的 JSON 回應，包含 `version`、`result` 與實際 `context_used`；HTTP 錯誤也保留其回應 JSON。不要保存 Authorization、session token、cookie 或請求 headers。 |
| `source_metadata` | 以回傳 `dataset_id` 為 key，內容取自相同 catalog manifest 的 `bundle.condition_metadata`；至少包含原有的 `temporal`、`bbox`、`resource_formats`。 |

計畫的 turn 只含 `query`、`expected`；擷取時加上 `response`、`source_metadata`。
`expected` 可以補充以下斷言：

| 欄位 | 用途 |
|---|---|
| `status` | 預設 `ok`；另可指定 `clarification_required` 或 `rejected`。 |
| `reason` | 預設 `none`，核對回應原因。澄清／拒絕必填，且必須是該狀態的產品原因，例如 `ambiguous_time`／`off_topic`。 |
| `topic_changed` | 預設 `false`；明確新搜尋可設為 `true`，要求主題異於上一個已提交主題，不能與 `retain_topic` 同時使用。 |

不存在的日期使用 `invalid_date`，短年份未標示紀年使用 `ambiguous_era`，時間條件不夠
明確使用 `ambiguous_time`。這些澄清原因分別核對，不能互相替代。

成功搜尋要求非空主題。澄清／拒絕允許新 session 的空主題，但整份搜尋 state
必須等於上次已提交的 state，`search` 必須為 `null`，`source_metadata` 為 `{}`。
這些輪次不改變後續比較使用的 state；下一個成功搜尋才能提交新 state。

限制為 8 MiB、32 個 case、每個 case 8 輪、每輪 20 張卡片。來源 metadata 與實機
revision／manifest／模型身分的取證應保存在 Git 外；工具核對輸入內容，不證明
來源檔案本身的真偽或現役部署狀態。

工具要求 API revision 為 40 位小寫十六進位、catalog manifest 為 64 位小寫
十六進位，且 result／search revision 相同。同一 case 的各輪必須使用相同的
revision 與 manifest，避免把不同部署或快照混成同一個對話基準；身分會列在報告中。
澄清／拒絕雖然沒有 search manifest，仍核對 API revision，不能掩蓋中途換版。

`retain_topic` 及 `topic_changed` 只檢查主題沿用／切換，不固定模型的主題措辭，
也不能證明主題解析語意正確。日期按產品的含端點重疊語意比較，不要求日期字串
補位方式相同。固定案例暫不涵蓋相對日期與農曆；擴充時必須一起記錄實際時鐘及
該類日期的產品語意，不能把執行當天偷偷當成固定答案。
本工具不評估未套用的農曆條件；成功回應含 `unapplied_time` 時會明列
`unapplied_time_not_evaluated`，不能只核對 civil time 後便算通過。

## 閱讀結果

- 正確性：比較預期、state、parsed／applied conditions；核對來源日期與格式、
  卡片順序／重複 ID，以及已知 outside 是否混入。
- 資訊可用性：`unknown_condition_fraction` 是最終卡片的 unknown 比例。
  缺少日期不是 outside，也不會自動算成正確性錯誤。完整的 2020～2025 年資料
  與 2024 年依既有重疊契約相符；建立／更新日期不能代替資料涵蓋期間。
- 排序：保留最終 ID、標題、涵蓋期間與順序，供人工判讀主題相關性。
  沒有相關性標註時，`topical_relevance` 固定為 `not_evaluated`，不宣稱 precision、
  recall 或 nDCG，也不能以 unknown 較少就斷言搜尋較好。

`candidate_count` 與 `condition_counts` 描述重排前的候選池，不是畫面的 top-k。
工具檢查候選計數的內部一致性，不能只從回傳卡片獨立驗證整個候選池。
但每種來源確認狀態的最終卡片數不能大於候選池該狀態的數量；最終卡片或候選池
有 unknown 時，回應也必須保留資訊不足的 warning。候選為零時可以沒有卡片，
候選為正數卻回傳零張卡片則不符合目前 top-k 契約。

現有 API 沒有完整 branch 候選、RRF 候選順序／分數與 reranker 分數；報告將這些
列為 `unavailable_trace`。只有最終 top-k，無法證明 RRF 加 reranker 比 RRF 單獨
更好，也無法回推未召回資料。要做這類比較，須另保存同一份候選與完整比較條件。

相關性標註另外保存於 Git 外，依 `plan_sha256 + case ID + turn + catalog manifest`
識別問題，以 `dataset_id` 識別候選。人工檢查來源後記錄相關／部分相關／不相關、
判斷理由及是否完成 review；模型產生的候選標籤與程式 fixture 不能直接當成 gold。
不同方法的候選先合併成去重判讀池，未標註資料維持未標註；不要直接視為不相關，
也不要只標註其中一個方法的第一名。現有工具保留排序供判讀，尚不計算相關性指標。

## 完整 catalog 的資料與快取量測

先固定 metadata snapshot、CSV／PDF 資源選取、解析政策及來源／模型身分，再
量測完整 catalog。下載、解析、索引與向量完成是不同階段；CSV 欄名或 PDF 摘要
可用不代表全文已索引。分母及略過原因見 [資源摘要](RESOURCE_PROFILES.md)，
重用規則見 [catalog cache](CATALOG_CACHE.md)。

建置使用獨立且長期保存的 product state，由部署流程安排新 catalog 的消費者
驗收與切換。向量 export 只會在同一個 state 下尋找，因此新 state 的第一次
新版完整 catalog 仍要正常建立，不能假設會自動找到其他實驗的快取。

| 比較 | 應保存的證據與解讀 |
|---|---|
| 第一次完整建置 | dataset／資源／可用摘要／chunk 數、下載與解析狀態分布、各階段與外層總耗時、索引及 export 占用空間。模型是否暖機另記錄。 |
| 完全相同 catalog 再執行 | `catalog_reused` 及跳過的工作；這是整份 catalog 重用，不能當成逐文件向量命中率。 |
| 同一 state 下建立新版 catalog | `vector-reuse/complete.json` 的 reused／new documents；命中率為 reused 除以兩者總數。保持文字的 metadata 更新應沿用向量，但要核對新的 metadata、ACL 與來源。 |
| Export 可保存範圍 | `vector-cache/manifest.json` 的 `cached_count`、`skipped_size_limit_count`、`record_bytes`；略過不代表資料未索引。現有 skip 計數合併單筆與總量上限，不能聲稱已分辨原因。 |
| 實際向量工作 | 各 pass 的 before／done／pending／progress、`no_op` 與 `elapsed_seconds`。新版全部命中時仍可能有一次合法 no-op pass，不能只靠 pass 數判斷是否重新 embedding。 |

向量 pass 時間包含協調與等待，不是純 embedding 時間；CLI 的完整稽核時間也
另列，不混入 API `timing_ms`。先以完整快照量測再估交付時間，小型命中或有限
資源樣本不能保證全量能在指定時限內完成。新 catalog 仍須用相同查詢集驗收，
逐文件重用正確並不等於搜尋品質通過。

CPU 回歸測試：

```bash
python3 -m unittest tests.unit.quality.test_evaluator tests.unit.quality.test_cli \
  tests.unit.quality.test_batches tests.unit.quality.test_batches_cli -v
```
