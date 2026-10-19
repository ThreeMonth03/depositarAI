# 原始碼打包與交接

`Source package` workflow 交付固定 commit 的原始碼 archive 和可離線還原的 Git
bundle。它適用於 public／private repository，不發布 GitHub Release、不 push image
registry，也不部署網站。完整產品的模型、metadata、映像與安裝流程見
[產品生命週期](PRODUCT_LIFECYCLE.md)；素材包的驗證見
[產品包還原](PRODUCT_RESTORE.md)。

## 原始碼與私有資料的界線

public repository 的 source artifact 應視為公開內容，包含選定 commit 的 **完整
reachable Git history**。刪除目前檔案不會刪除歷史版本；刪除 branch 也不會移除
仍可從打包 commit 到達的歷史。公開前必須整理來源及其歷史，不能把舊 private
repository 的 bundle 當作公開交付物。

source package 只從 Git 讀取 committed bytes，不複製未追蹤檔案、working-tree
快取、模型或 runtime 資料。真實 `.env`、API keys、憑證、內部位址、operator 設定、
metadata 原始資料和索引應留在 repository 外；範例只放可公開的佔位設定。
秘密掃描是額外檢查，不能取代人工檢查內部資訊、資料權限及授權。

`package-product` 產生的完整素材包另含模型、metadata 與映像，應由 operator
存放在受保護位置，不上傳至這個 workflow 的 artifact。source package 成功也不代表
完整 installer、資料移轉或 runtime 已可使用。

## 在 GitHub Actions 打包

選擇 `Source package` workflow 與預定 branch／tag。所有 jobs 明確 checkout
該次 `github.sha`，打包期間不追最新 `main`：

```text
同一個 commit
  ├─ validate：search/API、operator unit、Compose、image import、lint
  └─ private-website：backend、TypeScript、build、OSS runtime、HTTP browser tests
                 ↓ 兩者成功
固定 binary 秘密掃描 → source archive／Git bundle → source restore → source artifact
```

任一測試、秘密掃描、digest 或還原失敗，都不上傳 package。所有 jobs 只有
`contents: read`，checkout 不保留 token；只在手動執行時打包。

執行後核對 run 的 SHA、三個 jobs 的結果和 artifact 名稱。`pull_request` CI 的
`github.sha` 是 PR merge revision；手動 workflow 是所選 ref 的當次 revision。
因此 PR 綠燈不能取代打包 SHA 的檢查，也不能當成現役 runtime 的版本證明。
事件語意見 [GitHub 文件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。

## 秘密掃描與審核例外

CI 沿用開源 Gitleaks **8.30.1 Linux x64** binary，不使用需要授權的 action。
安裝器驗證固定 archive SHA256、版本及合成正／負 control；任一步失敗都不提供
scanner。archive SHA256 為
`551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`，
來源為[官方 checksum](https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt)。

掃描範圍是選定 commit 的完整 reachable history，與 bundle 一致，不使用 `--all`
加入其他分支。工具使用內建預設 rules，移除 caller 的 `GITLEAKS_*` 設定，停用
`gitleaks:allow` 註解的略過效果，並遮蔽診斷中的秘密。參數見
[官方使用說明](https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md)。

`.github/security/gitleaks-reviewed.json` 的 `entries` 為空時，**沒有任何掃描例外**。
若出現誤報，只能人工審核 selected history 中的 exact fingerprint，再以
`fingerprint`／`reasonCode` 記錄；工具驗證 commit、path、line、ancestor、唯一性
及固定 reason code。不得複製另一段歷史的 fingerprint、自動建立 baseline、停用
rule、略過目錄或允許所有 HEX64。政策 digest、source revision 與例外數量會寫入
打包結果；新 commit 中同 path／line 的內容不會匹配舊例外。

## artifact 內容

artifact 名稱為 `depositar-ai-source-<40字commit>`，只保留 **7 天**：

| 檔案 | 用途 |
|---|---|
| `depositar-ai-<commit>.tar.gz` | `depositar-ai/` 前綴的 tracked source，不含 `.git` |
| `depositar-ai-<commit>.bundle` | 選定 commit 及 reachable history，供離線 Git restore |
| `SOURCE_REVISION` | 精確 commit，必須與可信 workflow 的 SHA 一致 |
| `SHA256SUMS` | 上述三個檔案的 SHA256 |

這些 digest 不是數位簽章。預期 commit 應從可信 repository 的成功 workflow 或
已審核 commit 取得，不能只相信 package 自報的版本。需要長期保存時另行備份。

## 在本機打包與還原

需要 Python 3、Git、Bash；打包另需已驗證的 Gitleaks binary，不需要 Docker／GPU。
來源必須是 clean checkout、完整非 shallow history，以及精確 expected commit。
tracked docs／tests 的未提交修改也拒絕打包，未追蹤 CI helpers 不能冒充該 commit。

```bash
bash scripts/ci/source-package.sh \
  /absolute/private/new-source-package \
  <trusted-40-character-source-commit> \
  /absolute/private/verified-gitleaks

bash scripts/ci/source-restore.sh \
  /absolute/private/new-source-package \
  <trusted-40-character-source-commit> \
  /absolute/private/new-checkout
```

輸出路徑須尚未存在，parent 由 operator 擁有且不可 group/world 寫入，不得與
checkout 或輸入包重疊。工具使用 umask 077、新目錄為 0700；失敗不清空既有目錄。

還原會驗證固定 inventory、三個 digest、bundle HEAD、archive 的 Git blob identity
和 exact tree，再 clone 至新目錄、detached checkout、檢查 clean source 及 CLI help。
它不將 tar 任意解開，也拒絕 traversal、links、special files、重複／未知檔案及超限
內容。Git hooks 不執行；下載產物若未保留 mode，先在 operator 私有位置調整權限。

成功結果包含 `runtime_restored=false`、`deployment_performed=false`。工具不讀
product config、不啟動 Docker、不載入模型、不恢復索引。部署步驟見
[部署與維護](../guide/operations.md)。

每個 archive／bundle 上限 512 MiB，單一 source file 64 MiB，source 總量 1 GiB、
20,000 files；command output 4 MiB、Git 操作 120 秒、CLI help 30 秒、history scan
330 秒。超出時需審查 policy，不自動放寬或重試。
