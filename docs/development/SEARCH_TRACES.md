# 自願搜尋測試紀錄

在需要核對多輪搜尋時，先由維護者配置私有保存目錄，再由測試者於新搜尋前明確選擇
提供測試紀錄。未配置目錄或未選擇時，搜尋照常執行，不保存提問、state 或結果內容。
此功能使用現有搜尋入口；SQL 事件目前僅有人工契約 fixture，沒有 SQL 執行入口。

## 告知與選擇

頁面會先顯示實際保存期限與是否開放擷取。選擇只適用於全新的搜尋，不會補抓先前未
同意的對話；重設、登出或搜尋到期會清除瀏覽器記憶體中的選擇。沒有 localStorage
或以測試 ID 取代身分驗證的查詢入口。

紀錄包含原提問、前後搜尋條件與待澄清文字、實際 parsed／applied 條件、卡片 ID
與順序、卡片標題／格式／涵蓋日期、警告、狀態／原因、時間脈絡、耗時及可取得的
來源版本。每個測試 ID 另行隨機產生，與登入憑證和搜尋 token 無關；它是關聯標籤，
不代表完全匿名，也不賦予遠端讀取或刪除權限。

不擷取 Cookie、token、認證 header、秘密配置、任意連線資訊或模型隱藏推理。
原提問本身仍可能含敏感內容，測試者應避免輸入秘密；功能不承諾自動辨識並清除任意
原文中的秘密。BFF 取不到的模型／runtime 配置身分與中間分數會明列 unavailable，
不以本機常數推測遠端模型身分。`development` 等非 commit 標籤會記為未知來源；
離線品質核對需要 BFF 與 backend 都提供可用的 40 碼來源 revision。

## 配置私有輸出

使用獨立前端的專用 env file，加入下列設定；路徑為合成範例：

```dotenv
WEB_TRACE_DIRECTORY=/absolute/private/search-traces
```

目錄須事先存在，由 `WEB_UID` 對應帳號持有並使用 `0700`，紀錄檔為 `0600`。
選擇專用目錄，避免與 API key、CA、source checkout、模型、catalog 或備份來源重疊。
工具不自動建立 bind 來源，不替既有檔案放寬權限。

先只驗證設定：

```bash
SEARCH_WEB_ENV_FILE=/absolute/private/frontend.env \
  bash scripts/search-web.sh config
```

wrapper 只讀專用 env file 的設定，不採外層 shell 的 `WEB_TRACE_*` 覆寫。
配置目錄時才加入 `compose.search-web.traces.yaml`，容器內固定掛載到
`/var/lib/depositar/traces`。這是唯一新增的可寫 bind；容器根目錄、API key、選填 CA
與其他輸入維持唯讀。移除 `WEB_TRACE_DIRECTORY` 即恢復關閉擷取的設定。
設定驗證不啟停服務；映像更新仍依既有[前端交付流程](FRONTEND_DELIVERY.md)執行。
新來源合併不代表現役服務已啟用。

| env file 選項 | 預設值 | 預算 |
|---|---:|---|
| `WEB_TRACE_TTL_SECONDS` | 604800 | 七天保存期限 |
| `WEB_TRACE_MAX_TURNS` | 20 | 每個測試 session 的 admitted attempts |
| `WEB_TRACE_MAX_EVENT_BYTES` | 1048576 | 每輪 1 MiB |
| `WEB_TRACE_MAX_SESSION_BYTES` | 8388608 | 每個 session 8 MiB |
| `WEB_TRACE_MAX_TOTAL_BYTES` | 104857600 | 保存目錄 100 MiB |
| `WEB_TRACE_QUEUE_ITEMS` | 16 | 背景 writer 排隊數 |
| `WEB_TRACE_QUEUE_BYTES` | 16777216 | 排隊資料 16 MiB |
| `WEB_TRACE_MAX_SESSIONS` | 100 | 有限 session 數 |
| `WEB_TRACE_SHUTDOWN_SECONDS` | 1 | writer 正常停止的秒數預算 |

所有預算必須有限且大於零；停止預算至多十秒，turns 至多 100、sessions 至多
10000，其餘整數至多 1 GiB。提高 trace turns 不會改變既有搜尋的 20 輪上限。
TTL 於 writer 啟動與定期維護時清除；服務停止期間仍應由私有維護排程執行 prune。

## 擷取狀態與撤回

搜尋只投遞到有項目數與 byte 上限的記憶體 queue。`queued` 表示已排隊，尚未宣稱
寫入磁碟；滿載、超限、無法寫入或資料過大時，頁面顯示固定的擷取診斷，搜尋結果與
state 提交語意不因擷取失敗而改變。資料不會靜默截斷後標記為完整。

撤回會先停止後續擷取，更新 epoch 並排除舊的排隊項目。已進入 publication 的單輪
可能仍在 IO 中，撤回不等待該 IO；這時刪除顯示 `pending`，待 writer 完成後清除。
只有佇列、進行中的寫入與既有紀錄都移除後，才顯示 `complete`；sink 故障時不會
假稱刪除完成，可重試撤回以查核狀態。重設請求失敗也不表示紀錄已刪除。
搜尋到期後（預設 30 分鐘），瀏覽器 token 不再能呼叫撤回；保存期限內可由具私有
目錄權限的維護者以 CLI 刪除。測試 ID 本身不能用來取得遠端刪除權限。

執行中 raw export 的 `complete` 為 false。writer 排空事件與缺漏後，才在停止
預算內啟動這次 session 的完成封存。完成需要 manifest 與私有未完成標記都一致；
卡在 manifest 寫入、逾時或撤回時保留未完成標記，quality 匯出拒絕這些資料。
最後移除標記一旦進入核心 IO，無法取消；期限之後不會再啟動完成發布，已進入的
發布仍屬進行中。先於已授權的維護時段正常停止 writer，再做需要完整證據的離線
匯出；不要為了匯出擅自重啟現役服務。

## 私有維護命令

在有 OS 私有目錄存取權的維護環境，使用相同版本來源及其 Python 依賴執行。
下例 ID 與 manifest 為合成佔位值，實際使用取得的測試 ID 與封存的 catalog digest。
測試 ID 僅用來選取已驗證的 trace-owned 檔案；CLI 不接受任意待刪除路徑或跟隨
symlink。命令只輸出狀態、隨機測試 ID 與計數等有限中繼資訊；原文與結果寫入
明確指定的新私有輸出目錄。

```bash
python3 scripts/search_traces.py inspect --root /private/search-traces
python3 scripts/search_traces.py inspect --root /private/search-traces \
  --session 00000000000000000000000000000000
python3 scripts/search_traces.py export --root /private/search-traces \
  --session 00000000000000000000000000000000 --output /private/raw-export
python3 scripts/search_traces.py delete --root /private/search-traces \
  --session 00000000000000000000000000000000
python3 scripts/search_traces.py prune --root /private/search-traces
```

若部署使用自訂預算，為各子命令加上 `--limits /private/trace-limits.json`，使用
相容的讀取上限。此檔須位於 `0700` 目錄並使用 `0600`，內容只含 `TraceLimits`
的數值欄位，例如 `{"max_total_bytes":209715200}`；欄位名稱為上表選項去除
`WEB_TRACE_` 後的小寫名稱。未列欄位使用預設值，不把含秘密的完整 env file 當作
limits JSON。

輸出目錄必須不存在，匯出建立 `0700` 目錄及 `0600` 的 `manifest.json` 與
`turn-NNN.json` 逐輪檔案。它可以保存完整 20 輪 raw 證據；匯出仍受 TTL、完整性
及容量檢查約束。若目錄忙碌、權限不符、檔案
遭替換或存在不屬於 trace 的內容，先保留私有現場查核，不以刪鎖或放寬權限處理。
備份及匯出副本仍含原文，須另按相同私有存取與刪除政策管理。匯出重用既有私有
capture 工具，每個輸出 JSON 檔上限為 8 MiB；預設單輪上限為 1 MiB。若另行放大
設定而超過匯出檔上限，命令明確失敗，不截斷資料。

## 離線核對 legacy 品質

先獨立寫出查詢及 expected 計畫，再明確提供與紀錄中的 catalog manifest 相符的
sealed bundle。adapter 只從 bundle 的 `condition_metadata` 取來源證據，不拿
actual 卡片回填 expected，也不以 bundle 的欄位取代 actual 回應。CLI 產生
`capture.json` 與 `report.json`，重用[既有 evaluator](SEARCH_QUALITY.md)。

```bash
python3 scripts/search_traces.py quality --root /private/search-traces \
  --session 00000000000000000000000000000000 --output /private/quality-export \
  --plan /private/independent-plan.json --case example-two-turns \
  --bundle /private/sealed-bundle \
  --manifest 0000000000000000000000000000000000000000000000000000000000000000
```

quality 只接受從第 1 輪開始、最多 8 輪的完整連續前綴，以及對應的獨立計畫。
有 unavailable、漏輪、版本不連續、缺漏或未 sealed 的紀錄會被拒絕；不會跳過 503
重編輪次，也不把 20 輪任意拆開稱為同一段連續評測。每輪的 manifest 都必須配對。
人工 SQL fixture 只驗共用 schema，不進 legacy evaluator；這些工具不建立 SQL
executor，也不證明任意 SQL 語意、真模型品質或現役部署已驗收。
