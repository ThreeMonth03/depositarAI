# 搜尋正確性與排序檢視

使用 `scripts/check_search_quality.py` 離線核對已取得的搜尋回應。工具重用產品的
日期與條件判定，不呼叫 HTTP、模型或 Docker，也不建立索引。適合在修改對話、
metadata 或排序程式前，保存同一組查詢的可重跑基準。

```bash
python3 scripts/check_search_quality.py --input /private/search-capture.json
```

JSON 報告寫到 stdout。退出碼 `0` 表示檢查通過，`1` 表示有不符或無法核對項目，
`2` 表示輸入格式錯誤。每個 issue 都需要按回應、來源資料與部署版本定位，不能
只因一項失敗就斷言是搜尋演算法有 bug。

## 準備輸入

輸入是 `schema_version: 1` 與 `cases` 陣列；每個 case 包含唯一 `id` 與 `turns`。
一個 case 代表從新 session 開始、按順序送出的完整對話，BFF 回應的 `version`
應從 `1` 逐輪遞增。每輪必須包含：

| 欄位 | 內容 |
|---|---|
| `query` | 當輪實際送出的文字，最長 512 字。 |
| `expected` | `{time, formats, retain_topic}`；time 是 `{start, end}` 或 `null`，formats 是格式陣列，retain_topic 表示是否應保留上一輪主題。 |
| `response` | BFF `/api/turn` 的完整 JSON 回應，包含 `version`、`result.revision`、`result.search.revision` 與 `result.search.catalog_manifest_sha256`。不要保存 Authorization、session token、cookie 或請求 headers。 |
| `source_metadata` | 以回傳 `dataset_id` 為 key，內容取自相同 catalog manifest 的 `bundle.condition_metadata`；至少包含原有的 `temporal`、`bbox`、`resource_formats`。 |

限制為 8 MiB、32 個 case、每個 case 8 輪、每輪 20 張卡片。來源 metadata 與實機
revision／manifest／模型身分的取證應保存在 Git 外；工具核對輸入內容，不證明
來源檔案本身的真偽或現役部署狀態。

工具要求 API revision 為 40 位小寫十六進位、catalog manifest 為 64 位小寫
十六進位，且 result／search revision 相同。同一 case 的各輪必須使用相同的
revision 與 manifest，避免把不同部署或快照混成同一個對話基準；身分會列在報告中。

先涵蓋「2020～2025 氣候資料 → 2024 年 → CSV」，再加入清除條件、單側日期、
缺日期及無法判讀日期等少量案例。`retain_topic` 只檢查主題沿用，不以固定文字
強迫模型產生同一個主題措辭。

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

CPU 回歸測試：

```bash
python3 -m unittest tests.unit.operations.test_search_quality -v
```
