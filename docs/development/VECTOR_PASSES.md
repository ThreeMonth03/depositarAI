# 有界 embedding 建置與續跑

這是產品候選 catalog 的建置工具，不是已完成的網站 `install`／`update`。
處理 sealed catalog 中的 metadata 與已選取的資源文字；不下載 PDF／CSV、不公開 port、不分配 GPU、不更動 driver。
操作順序、配置與退出碼見[產品 CLI 參考](PRODUCT_LIFECYCLE.md)，首次部署見
[部署與維護](../guide/operations.md)。

## 分工

| 模組 | 責任 |
|---|---|
| `search/operations/catalog/backend.py` | 從 clean Git commit 封存 CPU catalog 與固定設定 |
| `search/catalog/admin.py`、`vectors.py` | Token 生命週期、有界 scheduler／job，逐文件 done／pending |
| `search/catalog/journal.py`、`search/vector_journal.py` | Durable event 介面、host/helper 的 hash chain 與 report schema |
| `search/operations/catalog/vectors.py` | 有界多輪、checkpoint、helper 移除與精確 ID 不存在證據 |
| `search/operations/shared/private_io.py` | 私有檔案、原子寫入與 fsync |
| `search/operations/catalog/projection.py`、`access.py` | 時間／格式投影、metadata／vectors 核對、query-only binding 與權限檢查 |

表內 `catalog/`、`shared/` 位於 `search/operations/`。
`prepare-query` 已串接 projection 與 query-only access：要求完整 catalog／vectors，
核對實際索引與權限後才建立 query authority。它不啟動網站；API 不取得 admin
secrets，也不匯入這些離線 administration 元件。

## 指令與固定預算

先完成候選資料的同步、catalog 準備、啟動與匯入；不確定的 import 必須先調查，
適用時顯式執行 `reconcile-index`。續跑使用目前選定的 sealed candidate、資料包與
原 `catalog_revision`，不修改收據或重標資料來源。

目前 clean 的應用程式來源可以較新，但須通過資料來源閉包及完整 `_recipe` AST
相容性檢查，再核對原 plan、映像、安全素材及 runtime。詳見
[Catalog cache](CATALOG_CACHE.md)。`stage-catalog` 自動選用舊 slot 只限已完整完成的
catalog；不能期待換版後自動選中舊 partial slot。明確選用原 catalog revision 也
不會解除不確定操作或 quarantine。

```bash
python3 scripts/depositarai.py --config /absolute/private/product.json build-vectors

# 明確限制本次工作量；不啟用網站。
python3 scripts/depositarai.py --config /absolute/private/product.json build-vectors \
  --max-passes 2 --wall-seconds 4800
```

| 項目 | 固定／預設值 |
|---|---|
| embedding | IBM Granite Embedding 278M，既有 digest pin，CPU |
| dimensions | 768 |
| 文件上限 | 每 pass 256 |
| concurrency／bulk | 2／100 |
| pass 工作 deadline | 1,200 秒 |
| token | 35 分鐘，不因 timeout 延長 |
| host helper timeout | 2,400 秒，含 helper 內清理，不是完成承諾 |
| invocation passes | 預設 16，範圍 1–40 |
| invocation wall budget | 預設 7,200 秒，範圍 2,400–36,000 |
| vector 歷史 | 最多 128 passes，不自動刪除證據 |

剩餘時間須足以容納完整 helper 才開始下一輪；`--max-passes` 是上限，不保證本次
會執行指定輪數。產品 profile 固定為每 pass 256，不是已實測最佳吞吐量；
向量命令不自動設定 cron。

`2,400` 秒是接受的最小 wall budget，**不保證開始任何 pass**。目前 preflight
（model、image 與歷史證據檢查）消耗本次 wall budget；開工前仍須有完整
`2,400` 秒 helper 額度。因此使用最小值可能在開工前就回傳 zero-pass checkpoint。
這不會延長單 pass 工作 deadline 或 token 有效期，調高 invocation budget 也不改
固定的 pass profile。

## 結果、續跑與失敗

- `vectors-complete`／exit 0：全文件 done、helper-side 清理證據成立、helper 已退出、
  一般 `docker rm` 成功且精確 ID 不存在，才封存完成。不是搜尋品質或網站部署驗收；
  768D 向量內容另由 projection 檢驗。
- `checkpoint`／exit 2：本次額度用完但全量尚未完成；在相同資料與相容來源下，
  下一次可從已核對的 clean checkpoint 續跑，不以 exit 0 假裝更新成功。
- 其餘錯誤：停止並保留 started／container／events／failure，不自動重送 start、
  force-remove、清 quarantine 或覆寫舊證據。沒有自動 timeout recovery 指令。

零輪有兩種不同結果：

- 未完成 catalog 因 preflight 後額度不足而回 `checkpoint`：
  `passes_this_invocation=0`、`host_cleanup_verified=false`。尚無歷史 pass 時，
  done／pending counts 是 `null`；有已驗證歷史時只回傳舊 counts，不能視為本次完成。
- 已有 `complete-clean` 歷史，重新核對 runtime 與完整 journals 後回
  `vectors-complete`：`passes_this_invocation=0`、`host_cleanup_verified=true`。
  沒有新 helper、admin token 或 vector journal；查詢準備仍須另掃實際索引與向量。

續跑傳前輪 `after_state_sha256`，在 scheduler enable／start **之前**比對本輪
`before_state_sha256`。
done 只接受 `done`，pending 只接受缺欄位／null；`fail`、`skipped` 或其他字串不猜
為可續跑。inventory 漂移、done 回退、仍有 pending 卻零進度、start response 遺失、
無法確認 token 撤銷、journal 寫入或 helper 移除，均阻擋續跑。

事件在候選 work root 的 `vector-operations/<run-id>/events/`，目錄 0700、檔案 0600，
不記 token、cookie、密碼。host 完成收據與 helper 證據分開，helper 不能授予自己
「host cleanup 成功」的權限。

保留全部事件與 host 收據，不手動刪除 journal、改寫 counts 或使用 `docker rm -f`
來繞過檢查。128 passes 是保留上限，不是到期刪除政策；達上限時先停止新增工作，
由操作人員規劃保留與後續處理。完整故障分類見[產品 CLI 參考](PRODUCT_LIFECYCLE.md#退出碼與故障處理)。

## 維護與驗證入口

固定 profile 與 host 續跑邏輯在 [vectors.py](../../search/operations/catalog/vectors.py)，
事件與狀態契約在 [vector_journal.py](../../search/vector_journal.py)。修改這些契約時，
先看[host 續跑測試](../../tests/unit/catalog/test_vectors.py)與
[pass 測試](../../search/tests/test_vector_pass.py)；檢查方式見[開發指南](../guide/development.md)。
