# 共用現役後端的私有開發候選

這個 operator 流程讓一個已審查的候選 release，與現役 release 共用既有的
唯讀 catalog/index、scope/planner 模型與 reranker。候選保有自己的 CPU API、
BFF、filtered embedding 與 website network；現役入口不切換。

```text
operator CLI → 候選 BFF → 候選 API ─┬→ 現役 catalog/index（既有 query-only 權限）
                                 ├→ 現役 scope／planner（相同 request policy）
現役入口 → 現役 API ────────────────┴→ 現役 reranker
```

同時最多註冊兩個完整 API client。每個 client 都要提供已由 host 核對的 source、
immutable image、lease、Compose 與實際 container/network 證據。catalog 與兩個
model networks 使用完整快照，peers 必須恰好是其服務與已註冊 API；不會先刪掉
未知 peers 再驗證。GPU ownership proof 仍只有原來三個 GPU 角色。

這適用於可信、已 review 的程式候選。網路成員與 request 相容性檢查不等於
逐一封鎖 Ollama 模型管理操作的 L7 ACL，也不是執行任意惡意程式的 sandbox。

## 設定與操作

使用目前完整、已驗收的 production config、凍結 production source 與現役 TLS plan。
`production_source` 必須是乾淨的 committed checkout，其 revision 要對上現役
release receipt；只有新版 operator checkout 仍不足以驗證舊 catalog。
不要以舊的 demo port、未完成的候選 receipt 或另一份 mutable checkout 代替。
設定檔必須是 operator 所有的 0600 regular file、直接父目錄 0700；`state_root`
必須是 Git 外、operator 所有的獨立 0700 目錄，且不能與 production state、
模型目錄或設定/source/TLS 輸入重疊。

可複製[設定範本](../../deploy/env/shared-backend.example.json)，再替換成主機上
已有的實際路徑：

```json
{
  "schema_version": 1,
  "state_root": "/absolute/private/shared-backend-state",
  "production_config": "/absolute/private/production/product.json",
  "production_source": "/absolute/frozen-production-source",
  "tls_plan": "/absolute/private/api-tls/plan.json"
}
```

候選來源必須是乾淨、已提交的 Git checkout。從包含此 operator 的已審查版本執行：

```bash
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json \
  prepare --source /absolute/clean-candidate-source
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json start
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json status
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json \
  query --query '找河川水質觀測資料'
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json stop
```

`prepare` 在 `state_root/sources/<revision>` 封存候選來源，於
`builds/<revision>` 準備映像，並核對現役基準與 request policy。
`prepared` 只在完整來源、模型與最後的 file descriptor/path 檢查成功後發布。
`start` 啟動候選 CPU 服務並驗證完整雙 client runtime；已在 running 的同版候選
只重新驗證，不重新建立服務。

running 狀態的 `status` 重新核對身分、唯讀 query authority、GPU ownership 與
現有模型駐留；它不發送使用者的 `/api/turn`，也不暖機。其他狀態只回報 journal phase，例如 empty、
prepared 或 failed，並不表示服務健康。`query` 要求 running，並在查詢前後驗證同一組
基準；只有查詢與最後驗證均成功才回覆結果。

`stop` 只移除這次登記且仍能證明擁有的候選資源與其來源/build cache。
無法證明 ownership 時應保留 journal 並調查，不以全域 prune 或模糊名稱清除。
prepare/start 中斷或失敗時，可先查 `status`，再以 `stop` 清理已封存的候選，
完成後重新 prepare。這不會以重啟現役模型代替修復候選。

## 舊 catalog 的來源稽核

既有 catalog 的資料配方屬於建立它的應用程式版本。新版 operator 可能為新 catalog
增加 recipe inputs，因此不能用新版 `DATA_FILES` 清單重新定義已封存 catalog 的身分。
本流程從指定的 production source 執行固定、唯讀的 context/query audit，核對原版
sealed recipe、catalog 記錄、Compose 與 query authority；不呼叫舊版 lifecycle 命令。
production source 在 audit 前後都必須保持原 revision 與乾淨狀態。

原版 audit 收到完整、已驗證的 runtime snapshot；新版 operator 在 audit 前後另行
驗證完整雙 client、immutable images、mounts、resources、network IDs 與精確 peers。
兩次 catalog snapshot 也必須一致。這保留舊資料的原版語意，同時不放寬新 catalog
的相容規則，也不省略目前實際 runtime 的檢查。

## Request 相容性與資源競爭

`validate_shared_request_policy(production_source, candidate_source)` 純讀取六份
來源檔，不 import 或執行候選。caller 先把兩個 source root 綁定至乾淨的 committed
source maps 與已驗映像，再比較固定模型名稱/digest、scope context/predict/採樣、
planner OPTIONS、stream/think，以及 production `keep_alive=-1` 的傳送規則。
目前 scope 為 4096/64，planner 為 16384/2048，production planner 使用 `think="low"`。
prompt、schema 與搜尋流程可以改；未知或動態改寫的 policy 結構會拒絕。

重用現役 `OLLAMA_MAX_QUEUE=1`、`OLLAMA_NUM_PARALLEL=1` 與 reranker 的既有單鎖，
不增加 broker。少量候選查詢會競爭同一組資源，可能令現役請求變慢或收到 busy；
這是此開發模式已接受的取捨，不要求每次查詢另排維護時段。
若要改模型、context、常駐策略或 GPU 分配，需另行驗證部署，不能藉此流程直接套用。

## 駐留模型的驗證紀錄

第一次 `prepare` 仍完整讀取並驗證固定模型內容，可能需要較長時間。成功後私有
journal 封存 `model_file_identity_sha256` 與 GPU compute UUID/PID 組合的
`compute_sha256`；前者不是略過模型 pin 的授權，也不單獨證明模型仍駐留。

同一候選後續的 start/status/query 可使用這份紀錄，省去重讀大於 32 MiB 的權重檔。
仍會重驗固定 manifest 與小檔內容、預期 digest、角色、權限、device/inode、大小、
mtime/ctime、父目錄及保留的 file descriptor/path 身分；操作結束時再檢查一次。
修改同 inode 的檔案再還原 mtime，或以相同 bytes 置換 inode，仍會使紀錄失效。

每次還須獨立核對三個 GPU 服務的 runtime/ownership、兩個模型的 digest/context 與
完整 GPU 駐留，以及原有四個 GPU compute UUID/PID 組合。模型程序改變、檔案異動
或駐留消失都會拒絕繼續。一般模型 startup/activation 仍走全量內容驗證；這份紀錄
不允許自動 load、reload、採納陌生模型程序或切換模型配方。

## 不暖機的模型檢查與單次查詢

`verify_residency(docker, api_container_id, policy)` 只在已驗證的現役 API namespace
以 GET 讀取 `scope-model:11434/api/ps`、`answer-model:11434/api/ps`。每個服務須恰有
預期模型，其 digest、context 相符，且 `size_vram == size > 0`。回覆只含兩個角色的
公開身分、context 與大小摘要；模型未駐留、CPU offload 或身分改變均停止操作，
不補送 POST、chat、load、warmup 或 stop。

`query(docker, bff_container_id, text, revision)` 在候選 BFF namespace 連線至
`127.0.0.1:8000`，固定使用 `Host: 127.0.0.1:18083` 與
`Origin: http://127.0.0.1:18083`。18083 是 BFF 的同源契約值，不是新增的 host port。
它建立 RAM session，先核對 BFF revision，再發送一個 `/api/turn`，核對回覆與
search revision，並在 `finally` 以 DELETE 重設 session。沒有第二次搜尋或自動重試。

query 沿用既有 512 字元與 Unicode 驗證，輸出實際 `result` 和 `context_used`，
適合直接查看公開卡片、涵蓋期間與查詢 clock。整次 Docker probe 上限 130 秒；
busy、timeout、身分不符或 reset 失敗回固定診斷，不反射 HTTP error body。
CLI 另有查詢前後的來源、catalog 與 runtime 稽核時間，130 秒是單次查詢 probe 的上限。
session token 與服務 credential 不離開 namespace process RAM，也不寫入 argv 或 stdout。
query 文字會以參數傳入 Docker exec；操作 receipt 只保留來源、身分、policy 與操作狀態，
不保存 raw query、cards、token 或 HTTP headers。請不要把互動 query 輸出當成交接 receipt。

probe 的內嵌腳本僅使用 Python stdlib，現役舊映像不需安裝新的 operator 模組。
此流程不呼叫模型暖機，也不使用 `verify_private_release(search=True)` 代替單次查詢。
start 的 readiness 檢查使用 `search=False`；status 與 query 的駐留檢查只做 GET。
檢查通過表示當下 runtime、駐留與這次操作可用，不代表搜尋品質或公眾入口已驗收。

需要保存少量、多輪案例供離線品質檢視時，使用
[有界搜尋批次](SHARED_BATCH.md)。它沿用同一份候選設定與操作鎖，將 raw
queries、responses 與來源 metadata 只寫入明確選取的新私有 capture。
