# 共用現役後端的私有開發候選

這個 operator 流程讓一個已審查的候選 release，與現役 release 共用既有的
scope/planner 模型與 reranker，並可查現役或另一份已準備好的唯讀 catalog/index。
候選保有自己的 CPU API、BFF、filtered embedding 與 website network；現役入口不切換。

操作協調與 catalog 權限由 [`candidate/`](../../search/operations/deployment/candidate/controller.py)
管理；有界案例執行見[批次流程](SHARED_BATCH.md)。

```text
operator CLI → 候選 BFF → 候選 API ─┬→ 已封存 catalog/index（自己的 query-only 權限）
                                 ├→ 現役 scope／planner（相同 request policy）
現役入口 → 現役 API ────────────────┴→ 現役 reranker
```

同時最多註冊兩個完整 API client。每個 client 都要提供已由 host 核對的 source、
immutable image、lease、Compose 與實際 container/network 證據。catalog 與兩個
model networks 使用完整快照，peers 必須恰好是其服務與已註冊 API；不會先刪掉
未知 peers 再驗證。GPU ownership proof 仍只有原來三個 GPU 角色。

這適用於可信、已 review 的程式候選。網路成員與 request 相容性檢查不等於
逐一封鎖 Ollama 模型管理操作的 L7 ACL，也不是執行任意惡意程式的 sandbox。

## 先建立 canonical provider authority

後端 control 與 catalog/product state 各有自己的權威紀錄。Provider anchor
封存實際 GPU 來源、映像、模型與 instance；active／rollback BackendRecord 則綁定
各自 API 來源、catalog、key、lease、CPU runtime 與必要素材。更新 API 不會把原 GPU
映像重新標成 API 的來源版本，也不要求舊 API 一直運行。

以 [control 設定範本](../../deploy/env/provider-control.example.json) 建立 Git 外的
私有設定，指定新的 control state：

```json
{
  "schema_version": 1,
  "state_root": "/absolute/private/backend-control"
}
```

第一次採納必須由已審查、乾淨的 operator，明確指定已驗 native release 的 bootstrap
與其外部 SHA256。Bootstrap 不是正常候選設定，也不是任選 container ID 的入口：

```json
{
  "schema_version": 1,
  "profile": {
    "schema_version": 1,
    "state_root": "/absolute/private/stopped-candidate-state",
    "production_config": "/absolute/private/product.json",
    "production_source": "/absolute/frozen/provider-source",
    "tls_plan": "/absolute/private/tls/endpoint/plan.json"
  },
  "expected_revision": "EXPECTED_40_HEX_PROVIDER_REVISION",
  "release_receipt_sha256": "EXPECTED_64_HEX_RELEASE_RECEIPT_SHA256",
  "inference_receipt_sha256": "EXPECTED_64_HEX_INFERENCE_RECEIPT_SHA256",
  "tls_plan_sha256": "EXPECTED_64_HEX_TLS_PLAN_SHA256"
}
```

預期來源與摘要從既有、獨立核對過的交付紀錄取得，不能從待採納資料自行讀取後當成
信任依據。檔案使用0600、直接父目錄0700；control root 不得與原 product、模型、
source 或 candidate state 重疊。先確認原候選已停止，沒有未完成 intent。
支援的 `adopt-backend-provider`／`check-backend-provider` 操作見
[部署與維護](../guide/operations.md)。

Adopt 只在完整來源／映像／query／key／模型／TLS／namespace 驗證及退出檢查完成後，
寫入新的 immutable reference records 與 Roles epoch1：active A、rollback null、
pending null。它不啟動模型、切換入口或重寫原生完成收據。缺 anchor 時，正常候選
命令會拒絕，不會重新採納舊 API 或自動修復不明 state。

Roles 的 epoch 是正整數且小於 `2**63`。每次 durable Roles 寫入都遞增一次，CAS
同時比對原 epoch 與完整 canonical record SHA256；唯讀檢查不遞增。Pending 指向
先落盤的 immutable intent；未完成或結果不確定的操作須由所屬維護流程明確處理，
不得刪除 pending 後重試。這個控制面目前不提供切換／回復命令。

## 設定與操作

正常候選僅使用一套 schema2，從
[共享後端範本](../../deploy/env/shared-backend.example.json) 填入實際路徑：

```json
{
  "schema_version": 2,
  "state_root": "/absolute/private/shared-backend-state",
  "control_config": "/absolute/private/provider-control.json",
  "catalog_config": "/absolute/private/catalog/product.json",
  "catalog_source": "/absolute/frozen/catalog-source"
}
```

`catalog_config`／`catalog_source` 必填；查現役資料也要明確指定。Catalog 必須已完成
正常 `prepare-secrets` 與 `prepare-query`，source 能以自己的封存配方重驗資料。
Operator 不建立資料、複製秘密或補造 inference/release 收據。另選 catalog 須保持
provider 的模型目錄／GPU 設定、embedding 與 query projection 相容，並使用明確
封存的 native chunk 配方。這裡不遷移 catalog。

設定檔須為 operator 所有的0600 regular file、直接父目錄0700；candidate state
是 Git 外獨立0700目錄，不得與 control、product、模型或輸入重疊。
操作順序固定為 canonical control lock、candidate journal、product/model state 與
catalog locks。已註冊候選不能直接改 catalog 或 Roles；先處理原候選及其實際權威。

Active／rollback 的 container、network、source、build 與共享依賴受到保護，不能
透過 candidate `stop` 刪除。Active B 與 rollback A 同時存在時沒有第三 client 名額；
回退窗口後的 A 退休屬於獨立的受控 release 操作，不是一般候選清理。

候選來源必須是乾淨、已提交的 Git checkout。從包含此 operator 的已審查版本執行：

```bash
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json \
  prepare --source /absolute/clean-candidate-source
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json start
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json status
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json \
  query --stdin < /absolute/private/query.txt
python3 scripts/share_search_backend.py --config /absolute/private/shared-backend.json stop
```

`prepare` 在 `state_root/sources/<revision>` 封存候選來源，於
`builds/<revision>` 準備 CPU 映像，並核對 canonical provider／Roles 與 request policy。
`prepared` 只在完整來源、模型與最後的 file descriptor/path 檢查成功後發布。
`start` 啟動候選 CPU 服務並驗證完整雙 client runtime；已在 running 的同版候選
只重新驗證，不重新建立服務。

running 狀態的 `status` 重新核對身分、唯讀 query authority、GPU ownership 與
現有模型駐留；檢查使用已驗 active API namespace，不要求最初的 API-A 存活。
它不發送使用者的 `/api/turn`，也不暖機。其他狀態只回報 journal phase，例如 empty、
prepared 或 failed，並不表示服務健康。`query` 要求 running，並在查詢前後驗證同一組
基準；只有查詢與最後驗證均成功才回覆結果。

單筆查詢由 UTF-8 標準輸入讀到 EOF；先在私有檔案填入查詢，再以重導向傳入，
避免把內容放進 shell 歷史或程序參數。輸入檔設為 `0600`，並放在 Git checkout 之外。
輸入須在 30 秒內結束、最多 4096 bytes，
並沿用 512 字元及字元正規化限制。檔尾可有一個 LF 或 CRLF 換行，不計入查詢
字元數。終端直接輸入與 `--query` 參數不接受。
查詢資料只經標準輸入傳給固定的 namespace helper；
互動結果仍會顯示於 stdout，需另行保護儲存的輸出。

`stop` 只移除這次登記且仍能證明擁有的候選資源與其來源/build cache。
無法證明 ownership 時應保留 journal 並調查，不以全域 prune 或模糊名稱清除。
prepare/start 中斷或失敗時，可先查 `status`，再以 `stop` 清理已封存的候選，
完成後重新 prepare。這不會以重啟現役模型代替修復候選。

## 舊 catalog 的來源稽核

既有 catalog 的資料配方屬於建立它的應用程式版本。新版 operator 可能為新 catalog
增加 recipe inputs，因此不能用新版 `DATA_FILES` 清單重新定義已封存 catalog 的身分。
本流程從各 catalog 指定的 source 執行固定、唯讀的 context/query audit，核對原版
sealed recipe、catalog 記錄、Compose 與 query authority；不呼叫舊版 lifecycle 命令。
每份 source 在 audit 前後都必須保持原 revision 與乾淨狀態。

原版 audit 收到完整、已驗證的 runtime snapshot；新版 operator 在 audit 前後另行
驗證完整雙 client、immutable images、mounts、resources、network IDs 與精確 peers。
兩次 catalog snapshot 也必須一致。這保留舊資料的原版語意，同時不放寬新 catalog
的相容規則，也不省略目前實際 runtime 的檢查。

## Request 相容性與資源競爭

`validate_shared_request_policy(provider_source, candidate_source)` 純讀取固定來源集合，
不 import 或執行候選。caller 先把兩個 source root 綁定至乾淨的 committed
source maps 與已驗映像，再比較固定模型名稱/digest、scope context/predict/採樣、
planner OPTIONS、stream/think、provider `keep_alive=-1`，以及固定 reranker model／
wire limits／deadline、請求與回傳順序的傳送規則。
目前 scope 為 4096/64，planner 為 16384/2048，provider planner 使用 `think="low"`。
prompt、schema 與搜尋流程可以改；未知或動態改寫的 policy 結構會拒絕。

重用現役 `OLLAMA_MAX_QUEUE=1`、`OLLAMA_NUM_PARALLEL=1` 與 reranker 的既有單鎖，
不增加 broker。少量候選查詢會競爭同一組資源，可能令現役請求變慢或收到 busy；
這是此開發模式已接受的取捨，不要求每次查詢另排維護時段。
若要改模型、context、常駐策略或 GPU 分配，需另行驗證部署，不能藉此流程直接套用。

## 駐留模型的驗證紀錄

第一次 explicit adopt 完整讀取並驗證固定模型內容，可能需要較長時間。成功後 anchor
與候選 journal 分別綁定 `model_file_identity_sha256` 與 GPU compute UUID/PID 組合的
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

`verify_residency(docker, api_container_id, policy)` 只在已驗證的 active API namespace
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
query 文字經有界標準輸入傳入固定腳本的 Docker exec，不寫入 argv 或 environment；
操作 receipt 只保留來源、身分、policy 與操作狀態，
不保存 raw query、cards、token 或 HTTP headers。請不要把互動 query 輸出當成交接 receipt。

probe 的內嵌腳本僅使用 Python stdlib，現役舊映像不需安裝新的 operator 模組。
此流程不呼叫模型暖機，也不使用 `verify_private_release(search=True)` 代替單次查詢。
start 的 readiness 檢查使用 `search=False`；status 與 query 的駐留檢查只做 GET。
檢查通過表示當下 runtime、駐留與這次操作可用，不代表搜尋品質或公眾入口已驗收。

需要保存少量、多輪案例供離線品質檢視時，使用
[有界搜尋批次](SHARED_BATCH.md)。它沿用同一份候選設定與操作鎖，將 raw
queries、responses 與來源 metadata 只寫入明確選取的新私有 capture。
