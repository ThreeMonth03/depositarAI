# 私有候選的有界搜尋批次

`share_search_backend.py batch` 對已登記且正在運行的 CPU 候選執行少量搜尋案例。
它集中執行批次前後的完整查核，保留每個案例的 runtime 查核及每輪的模型檢查。
它不準備或啟動候選、不切換現役入口，也不重新載入模型。

一批最多 **3 個案例、合計 8 輪**，每輪沿用產品的 512 字元與 Unicode 驗證。
每個案例建立新的 RAM session；同一案例依序保留 session/version，結束時以
`finally` 重設 session。批次持有既有候選的操作鎖，不能和另一個操作者共用它。

## 執行前

先依 [共享後端流程](SHARED_BACKEND.md) 準備並核對候選。使用同一份私有 config，
確認候選的完整 Git revision，並將輸入與輸出放在 Git 外的私有目錄。
原始查詢、回應與來源 metadata 可能含個人資訊，應依資料的使用範圍保存與分享。

案例沿用 [離線正確性工具](SEARCH_QUALITY.md) 的 `id`、ordered `turns` 與
`expected` 語意；執行前不需要填入尚未取得的 `response` 或 `source_metadata`。
批次直接重用 `search.quality.contract.validate_cases`，不另訂一套預期條件格式。
案例與預期條件由資料／搜尋品質維護者核對，批次不產生 gold labels 或評分。

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "climate-year-format",
      "turns": [
        {
          "query": "找 2020 到 2025 年的氣候資料",
          "expected": {
            "time": {"start": "2020", "end": "2025"},
            "formats": [],
            "retain_topic": false
          }
        },
        {
          "query": "我要 2024 年的 CSV",
          "expected": {
            "time": {"start": "2024", "end": "2024"},
            "formats": ["CSV"],
            "retain_topic": true
          }
        }
      ]
    }
  ]
}
```

`expected` 必填 `time`、`formats`、`retain_topic`，另可加入以下欄位：

| 欄位 | 用途 |
|---|---|
| `status` | 預設 `ok`；另可指定 `clarification_required` 或 `rejected`。 |
| `reason` | 預設 `none`。澄清／拒絕須明確指定該狀態接受的產品原因，例如 `ambiguous_time`／`off_topic`。完整清單沿用離線工具的 `OUTCOMES`。 |
| `topic_changed` | 預設 `false`；第二輪起可設為 `true`，表示成功搜尋應換成不同主題，不能同時要求 `retain_topic`。 |

第一輪的 `retain_topic` 與 `topic_changed` 都須為 `false`；澄清／拒絕不能要求
`topic_changed`。預期狀態屬於品質斷言，仍保存實際回應供離線工具判定，不因預期
澄清／拒絕而跳過該輪或停止對話。

`query` 驗證通過後，計畫、HTTP 請求與 capture 都保留原始字串，包括前後空白、
換行及全形字元；不能用 trim、Unicode 正規化或改寫後的文字替換固定計畫。
JSON 的縮排與 key 順序不影響計畫摘要，查詢字串本身的變更則會改變摘要。

將輸入存成操作帳號所有的 `0600` 檔案，其直接父目錄使用 `0700`。
`--capture` 指向**尚不存在**的新私有目錄；不要使用 repository、共享候選 state、
production state、模型或來源目錄及其子目錄。

```bash
python3 scripts/share_search_backend.py \
  --config /absolute/private/shared-backend.json \
  batch \
  --cases /absolute/private/quality/cases.json \
  --capture /absolute/private/quality/run-001 \
  --expected-revision FULL_40_HEX_CANDIDATE_COMMIT
```

`--expected-revision` 必須與目前登記的 running 候選完全一致。命令不會為了執行
案例而接管別人的候選、更新映像、切換 catalog 或重試搜尋。

## 固定題組分批

[固定查詢集](../../tests/fixtures/search-quality.json) 可以超過單批的 3 個案例或
合計 8 輪。先凍結完整計畫並以離線工具驗證，再複製成 Git 外的私有子計畫，
每批選取完整案例，保留原有 case ID、案例與輪次順序、原始 query 及 expected。
不要把同一案例拆到兩個 session，也不要刪輪、改預期或縮短查詢來符合單批上限。
若單一案例本身超過 8 輪，須由品質維護者重新審核計畫，不能直接拆成獨立對話。

每個子計畫各用新的 `--capture` 目錄執行。`run.json.plan_sha256` 由現有
`search.quality.contract.plan_digest` 計算，綁定**這一批子計畫**的 case ID、原始查詢、預期條件及
順序；不是案例檔案的原始 bytes SHA、capture 摘要或完整固定題組的摘要。
完整計畫的 `plan_sha256` 另外由離線工具的 `--plan` 取得；各批子計畫摘要通常
不同，應與完整計畫及拆批對應一起保存。

跨批彙整與評分由資料／搜尋品質維護者（agent2）處理：核對各批已驗證的來源、
catalog、模型與 request policy 身分，再以凍結的完整計畫核對案例、輪次及查詢／
預期。漏跑案例或尾端輪次須保留在完整分母中，不能只加總各批成功項目或直接
平均百分比；失敗或隔離的批次也不能從執行紀錄中消失。此命令不自動合併或評分。

## 查核與失敗邊界

批次前後各核對完整的原生 catalog/query authority、runtime、GPU ownership、
模型駐留與來源。每個案例開始前重新核對完整 runtime 與 BFF 位址；每輪送出前
檢查固定模型、駐留與 compute process 身分。HTTP 只連 host 已認證的 BFF 私有
IPv4 與固定 port，不接受案例提供的 URL、Host、Authorization 或服務 key。

所有案例共用既有 Docker client 的 command/time budget。最後完整查核保留獨立
時間，不會為了跑滿案例而放寬 timeout 或跳過查核。每個案例另有輪數乘以 130 秒
的期限，保留重設 session 的時間。busy、timeout、身分改變、checkpoint 或 reset
失敗都會停止後續案例與輪次，沒有自動重送。

完整的最後查核與 retained source/model FD 檢查退出成功後，capture 才能標示
身分已驗證。查核失敗時保留明確的部分結果與隔離狀態，不能當成完整、可信的
品質基準。不要手動修改狀態、刪除收據或重用同一輸出目錄來掩蓋失敗。

## 私有輸出與耗時

capture 只寫入明確指定的私有位置；正常 stdout 與操作摘要不包含 raw query、
卡片、context、cookie、Authorization 或 session token。token 僅留在 HTTP client
記憶體，不進 stdout、持久檔案或命令列參數。

| 檔案 | 內容 |
|---|---|
| `capture.json` | 原查詢、預期條件、完整 BFF response，以及按實際回傳 ID 從同一 catalog bundle 取得的 condition metadata。 |
| `outcomes.json` | 各案例／輪次狀態、固定錯誤碼、session reset 與耗時；未執行項目標示 `not_run`。 |
| `run.json` | 批次狀態、`plan_sha256`、已驗來源／catalog／模型／request policy 身分、完成數與查核耗時。 |

`request_seconds` 只量一次 `/api/turn` HTTP 請求；`wall_seconds` 包含該輪前置
checkpoint。回應內原有 `timing_ms` 保留原值，描述 API 提供的階段耗時。
`operator_seconds` 與 `audit_timing` 另外描述批次執行及完整查核成本，不能把它們
當成模型推論或使用者搜尋 latency。

先確認 `run.json` 為 `complete` 且 `identity_verified` 為 `true`，並核對
`artifacts` 中的 `capture`／`outcomes` 檔名、bytes 與 SHA256。它們綁定本次實際
發佈的檔案；人工修改預期或回應後，原收據就不能再作為該檔案的證據。
核對後再交給資料／搜尋品質維護者檢視或使用離線工具：

```bash
python3 scripts/check_search_quality.py \
  --plan /absolute/private/quality/cases.json \
  --input /absolute/private/quality/run-001/capture.json
```

這裡的 `--plan` 是該批凍結的子計畫，避免改寫預期或漏掉輪次後仍被當成完整。
單批報告不代表整套固定題組已完成；完整題組仍需依上節規則核對與彙整。

`complete` 表示這次有界收集完成，不表示搜尋品質通過。資料缺日期、條件標籤、
topic retention、主題相關性與人工標註各自評估。沒有提供的 branch/RRF/reranker
分數或完整候選軌跡維持 `unavailable`；不從最終 top-k 補造分數或推算 recall。

## 新 catalog 的後續消費流程

這個批次仍使用共享後端 config 已封存的現役唯讀 catalog，不能更換一個 bundle
路徑便宣稱已驗收新 catalog。完整資料建置應使用專用、長期保留的 product state，
保留 catalog slots、Docker volume、query receipts 與 vector exports；後續版本沿用
同一份 state。第一版正常建置，完成 `prepare-query` 後才有可供後續使用的 export。
私人 A/B 實驗的 exports 不會自動成為另一個 product state 的 seed。

資料維護者先凍結 snapshot、resource scope、解析／chunk/model profile，分開記錄
dataset、檔案與 chunk 數、usable/skipped 原因、儲存量、cache reuse 及建置耗時。
參考 [catalog cache 契約](CATALOG_CACHE.md)。

新 catalog 的 consumer 需另行實作與驗收：各自認證原、新 catalog 的 core runtime、
query-only 權限與 client lease，並保留同一組 GPU 身分與 request policy。之後才可
對新 manifest 執行品質批次。API、BFF、TLS 及前端 backend binding 的切換與回退
屬於另一個部署步驟；驗收期間保留原 state、volume、入口與指標。
