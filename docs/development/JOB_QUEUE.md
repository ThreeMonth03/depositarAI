# 共用單機工作佇列

`search_jobs.py` 讓多個 agent 直接送件、查進度及讀取結果。所有 worktree、
branch 與已登記的資源摘要 state 共用同一個 Pueue `heavy` group，一次執行
一件工作；一般程式編修與 hosted CPU CI 可繼續平行進行。

三種固定工作使用同一個入口：

| 類型 | 固定工作量 | 主機期限 |
|---|---|---|
| `resource-pass` | 已準備的 CSV／PDF 計畫，最多20份，不重試失敗項目 | 3,600秒 |
| `quality-batch` | 已運行候選的完整子計畫，最多3個案例、合計8輪 | 3,600秒 |
| `vector-pass` | 已準備 catalog 的最多1個 pass，原生 wall budget 3,600秒 | 4,200秒，保留原生返回及清理時間 |

入口不建立計畫、不啟停服務或模型。先完成各原生流程的準備：
[資源摘要](RESOURCE_PROFILES.md)、[搜尋批次](SHARED_BATCH.md)、
[向量續跑](VECTOR_PASSES.md)。

```text
agent A ── submit ──┐
                   ├── heavy [1] ── 已登記的固定工作 ── 私有結果
agent B ── submit ──┘
         status / result ◀──────────────────────────────────┘
```

## 維護者設定一次

先備妥 Pueue **4.0.4** 的 client 與 daemon、Python、Git、Docker 和既有抽取
helper 的建置需求。Pueue 可放在帳號專用目錄，不必安裝系統套件；從
[固定 release](https://github.com/Nukesor/pueue/releases/tag/v4.0.4)
取得二進位檔並核對 SHA256，client 與 daemon 使用同一版。
這個 CLI 不安裝、升級或啟動 daemon。

登記檔固定放在執行帳號的 passwd home 下
`.config/depositarai/jobs.json`，父目錄為 0700、檔案為 0600。
CLI 沒有 registry、config、group、shell 或環境變數的覆寫選項。
不要為每個 agent、worktree 或 product state 另建 daemon。

以下是登記檔結構；將路徑與摘要換成已檢查的實際值。所有路徑使用絕對路徑，
queue root 與 runtime root 都使用 Git 外、目前帳號持有的專用 0700 目錄。
`runtime_root` 放在這台主機的本機檔案系統，供 Unix socket 使用。

```json
{
  "schema_version": 1,
  "root": "/absolute/private/job-queue",
  "runtime_root": "/absolute/local/private/job-runtime",
  "pueue": "/absolute/private/tools/pueue",
  "operator_source": "/absolute/private/frozen-operator",
  "operator_revision": "OPERATOR_COMMIT_40_HEX",
  "machine_id_sha256": "THIS_HOST_MACHINE_ID_SHA256_64_HEX",
  "profiles": {
    "catalog-profiles": {
      "type": "resource-pass",
      "bundle": "/absolute/private/catalog/sha256-BUNDLE_ID",
      "state_root": "/absolute/private/prepared-resource-state",
      "plan_sha256": "PREPARED_PLAN_SHA256_64_HEX",
      "manifest_sha256": "CATALOG_MANIFEST_SHA256_64_HEX"
    }
  }
}
```

`operator_source` 是維護者保存的乾淨 committed source，且 HEAD 必須等於
`operator_revision`。`machine_id_sha256` 綁定本機 `/etc/machine-id` 的 SHA256，
避免同一份共享家目錄的設定被帶到另一台主機使用。不要把 registry、machine ID、
原始 metadata、私有結果或實際主機路徑提交 Git。

`bundle`、`state_root` 與兩個 SHA256 須對應同一份已 `prepare` 的摘要計畫。
增加可用計畫時由維護者登記新的 profile；送件者不能提交任意 command、argv、
環境、Docker 參數、資料路徑或輸出位置。
每件已接受工作保留送件時的 profile 定義；之後新增或修改登記不會改寫它，也不會
取消它。queue root、operator、主機與 socket 等執行身分仍須保持一致。

Pueue 使用 queue root 下的 `pueue.yml`，內容必須符合程式產生的固定模板：
Unix socket 位於 `runtime_root/depositarai-heavy.socket`，資料與紀錄位於
queue root 下，shell 固定為 `/bin/sh -c`，不設 callback 或環境 hook，並啟用
`pause_group_on_failure`；aliases 固定為空，避免沿用帳號其他 Pueue 別名。
在乾淨的 operator checkout 中建立設定檔：

```python
from pathlib import Path
import os
from search.operations.jobs import pueue, queue
from search.operations.shared import private_io

registry = queue.load_registry()
fd = private_io.open_directory(Path(registry["root"]))
try:
    private_io.write_file(fd, "pueue.yml", pueue.configuration(registry), max_bytes=16384)
    private_io.write_file(fd, "aliases.yml", b"{}\n", max_bytes=64)
    child = private_io.open_child_directory(fd, "pueue", create=True)
    os.close(child)
finally:
    os.close(fd)
```

此程式只供初次設定；檔案已存在時拒絕覆寫。將 client 設為帳號所有的 0700
檔案，直接父目錄也使用 0700。選定的 daemon 必須是同一固定版本，以同一份
config 在獨立終端啟動，再建立 `heavy` group：

```sh
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /absolute/private/tools/pueued \
  --config /absolute/private/job-queue/pueue.yml

# 另一個終端；同一份明確指定的 config。
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 /absolute/private/tools/pueue \
  --config /absolute/private/job-queue/pueue.yml group add heavy --parallel 1
```

保持 concurrency 為 **1**，default group 保持空白。請勿直接用 Pueue 的任意命令、
force-start 或 restart 送產品工作。入口只放行固定 profile；送件先保持 stashed，
核對並移除已保存環境中的非允許欄位，再放入排程。

只開 Unix socket，不開 TCP listener。設定完成後先用隔離 fixture 工作檢查
送件及故障處理，再由主控安排少量真實資源驗收。請勿把公眾 PR 交給這個主機執行。

## 登記品質與向量工作

這兩類的 `source_revision` 必須等於送件 `source` checkout 的 HEAD。原生身分
摘要也須由這份乾淨 committed source 取得；不要用 queue operator 的版本代替。
它們可以是不同 worktree，但送件來源仍須與 operator 共用同一 repository。
執行時使用凍結的送件來源，並把摘要交給原生 CLI 在既有 domain locks 內比較，
不是先查一次 status 後就假設目標不變。

### 品質批次

先在授權的硬體時段，用將來的送件來源查取完整 runtime identity 的摘要：

```sh
git -C /absolute/private/native-source rev-parse HEAD
python3 -I -S -B /absolute/private/native-source/scripts/share_search_backend.py \
  --config /absolute/private/candidate.json status --batch-identity
```

將上面的 Git SHA 記為 `source_revision`，回傳的 `revision` 記為
`candidate_revision`，`batch_identity_sha256` 記為 `expected_identity_sha256`。
status 輸出沒有獨立的 operator SHA 欄位；完整摘要包含執行來源，因此必須使用
同一個來源取得摘要及執行 batch。來源不同時，原生檢查會在第一個 query 前拒絕。

```json
{
  "type": "quality-batch",
  "config": "/absolute/private/candidate.json",
  "config_sha256": "RAW_CONFIG_BYTES_SHA256",
  "source_revision": "EXECUTING_SOURCE_COMMIT_40_HEX",
  "candidate_revision": "RUNNING_CANDIDATE_COMMIT_40_HEX",
  "cases": "/absolute/private/quality/subplan.json",
  "cases_sha256": "RAW_CASE_FILE_BYTES_SHA256",
  "plan_sha256": "QUALITY_CONTRACT_PLAN_SHA256",
  "expected_identity_sha256": "FULL_BATCH_IDENTITY_SHA256"
}
```

`config` 上限16KiB、`cases` 上限64KiB；兩者使用0700父目錄及0600檔案。
`config_sha256`、`cases_sha256` 是原始 bytes 的 SHA256；`plan_sha256` 使用
`search.quality.contract.plan_digest()`，不是原始 JSON 檔的 SHA。
凍結保留原 query、expected、案例及輪次順序，不刪漏輪次或改寫完整分母。

### 向量 pass

同樣從將來的送件來源取得原生輸入摘要：

```sh
python3 -I -S -B /absolute/private/native-source/scripts/depositarai.py \
  --config /absolute/private/product.json vector-inputs
```

```json
{
  "type": "vector-pass",
  "config": "/absolute/private/product.json",
  "config_sha256": "RAW_CONFIG_BYTES_SHA256",
  "source_revision": "EXECUTING_SOURCE_COMMIT_40_HEX",
  "catalog_revision": "SEALED_CATALOG_COMMIT_40_HEX",
  "bundle_id": "SEALED_BUNDLE_ID_SHA256",
  "manifest_sha256": "CATALOG_MANIFEST_SHA256",
  "expected_inputs_sha256": "FULL_VECTOR_INPUTS_SHA256"
}
```

以上身分欄位取自 `vector-inputs`，將 `inputs_sha256` 填入
`expected_inputs_sha256`。`source_revision` 是執行 CLI 的來源；相容的
`catalog_revision` 可以較舊，不能用它取代送件來源。完整輸入摘要綁定原生
plan、recipe、模型及 runtime/image 身分，不挑選少數已知欄位重算。

提交只讀取並凍結已登記的輸入，不自行執行以上身分查核、探測 runtime、
重新取得摘要或啟動模型。config 原始 bytes 與絕對路徑保持不變；原生 config
仍須符合既有的 private path／state 規則。

## Agent 送件與讀取結果

將請求存成 Git 外的 0600 JSON，父目錄為 0700。`profile` 必須是已登記的名稱；
`source` 是乾淨 checkout，`revision` 必須是該 checkout 的完整 HEAD commit。
它必須與 operator 共用同一 Git repository；不同 branch／worktree 可以使用，
另一份 clone 或任意本機 repo 不接受。commit 須由可信任的維護流程審視，這個
檢查不會把未知 PR 程式變成安全工作。
Issue number 只作追蹤，不會讀取或執行 Issue 文字。

```json
{
  "schema_version": 1,
  "issue": 123,
  "profile": "catalog-profiles",
  "source": "/absolute/private/agent-checkout",
  "revision": "SOURCE_COMMIT_40_HEX"
}
```

在含有這個入口的 checkout 執行：

```sh
python3 -I -S -B scripts/search_jobs.py submit \
  --request /absolute/private/resource-job.json
```

保存回傳的 32 碼十六進位 job ID，即可查詢及取回摘要，不需由使用者傳話：

```sh
python3 -I -S -B scripts/search_jobs.py status
python3 -I -S -B scripts/search_jobs.py status --job JOB_ID
python3 -I -S -B scripts/search_jobs.py result --job JOB_ID
```

送件時保存原始請求、固定來源及計畫身分；之後修改原 checkout，不會改變已送出的
工作。開始執行時仍會核對計畫、catalog 與抽取器身分，漂移或損毀就拒絕執行。
第一次下載外部檔案時，內容以來源當時回傳的 bytes 為準，並記錄內容 SHA256；
這不保證外部 URL 的檔案在排隊期間保持不變。

一般輸出只包含狀態、來源 revision 及摘要。私有 job 目錄位於 queue root 的
`jobs/JOB_ID`，保存 `request.json`、`queue.json`、`result.json`、`operation.json`、
固定來源與非空的 `stdout.bin`／`stderr.bin`；空 stream 以收據中的零 bytes 記錄。
品質／向量另保留 `native-config.json`；品質另有 `quality-cases.json` 及
`capture/run.json`、`capture/capture.json`、`capture/outcomes.json`。
結果讀取會重驗這些凍結輸入及原生 artifact，不只相信 stdout 摘要。
中斷時若未取得完整 pipe 輸出，`log_status` 明確標為 `unavailable`，不當成完整 log。
主控從這些檔案及原資源 state 調查；不要把原始輸出貼到公眾 Issue 或 CI log。

## 結果與下一批

| 狀態 | 操作者動作 |
|---|---|
| `queued` | 等待同一個 heavy group 的先前工作結束。 |
| `running` | 用 `status` 查看；不要另外手動執行同一資源計畫。 |
| `checkpoint` | 資源／向量的有界工作已返回，尚未宣告全量完成；需要繼續時明確送新工作。 |
| `completed` | 該類原生工作完成：資源無待辦、品質收集完整或向量完成；不是搜尋相關性通過。 |
| `partial` | 嘗試已結束但有失敗資源；檢查既有摘要紀錄，佇列不自動重試。 |
| `blocked` | 檢查資源、登記檔或輸入身分；修正後由主控確認再送件。 |
| `cancelled` | 尚未開始的工作已移出佇列，保留送件與取消紀錄。 |
| `interrupted`／`recovery_required` | 先核對私有紀錄及 helper 狀態，再決定後續操作。 |

底層 `profile-resources` 的 exit 2 是有效 `checkpoint`，exit 1 也可能是已保存
失敗項目的 `partial`。請看結構化結果，不能把每個非零 exit 都當成未清理的崩潰。
正常完成一次 pass 也不表示整份 catalog 已建立、切換或通過搜尋品質評估。
品質批次只在完整 identity、全部案例／輪次與原生 capture 驗證後標為 `completed`；
failed／quarantined保留不確定 intent。向量合法的零新pass checkpoint仍是
`checkpoint`，不能當成有嘗試新helper或全量完成；已完成catalog的零pass唯讀
複核則須有完整原生清理證據才是 `completed`。

尚未開始的工作可取消：

```sh
python3 -I -S -B scripts/search_jobs.py cancel --job JOB_ID
```

`cancel` 只移除仍在排隊的項目，不 kill 執行中的工作。工作開始後取消會拒絕，
避免把停止主機 process 誤當成 Docker helper 已停止。
第一版取消也使用全域執行鎖；另一件重型工作執行中時會回 `job_busy`，須在其結束後
重試取消。它不為了取消排隊工作而暫停或中斷正在執行的項目。

## 中斷與復原

Pueue 的佇列狀態不是操作或清理證據。runner 在開始前保存 durable `active.json`；
無法確認完成或清理時保留這份 intent，阻止後續重型工作。重新連線、daemon 重啟
或手動 `pueue start` 都不能代替復原。

遇到 timeout、強制終止、遺失結果或未知 helper 時，主控先核對該 job 的來源、
原始 operation 結果及資源 state 中的 helper 名稱，再依
[資源摘要的中斷處理](RESOURCE_PROFILES.md#中斷重試與快取)處理那一筆工作。
尚未確認時維持 `recovery_required`，不要清除 `active.json`、重寫舊 digest、
對整批工作執行 `pueue restart`，或強刪不明容器。

目前沒有通用 force-clear、重試或一鍵復原入口；主控須檢查原本的資源/helper 證據。
保留仍被工作引用的來源與結果。清理只限已確認屬於自己、且不再被引用的工作資產，
不能用整機 prune 或只按檔案年齡刪除。

## 權限與操作範圍

這是同帳號可信任 agent 的協作排程，不是權限隔離；同一個 Unix 帳號仍能繞過
佇列或改動檔案。既有 product、catalog、profile locks 與 leases 繼續生效。
沒有足夠資源時工作停止等待，不會驅逐現役模型、切換服務或代替使用者授權。

搜尋品質及向量工作只接受完整 expected identity，不能省略原生前置條件。
只有結果後驗不能證明事前使用了預期資料。本入口不處理訓練、部署、公開 PR 執行
或雲端 GPU；實際機器的排程與操作授權仍由維護者管理。
