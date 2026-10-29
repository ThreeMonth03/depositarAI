# 前端主機套用已驗證的 release

[`deploy_search_web.py`](../../scripts/deploy_search_web.py) 在既有前端 VM 上套用
GitHub Release `frontend-<完整 commit SHA>` 的兩個 CPU 映像。來源簽章、repository ID、
main source ref、指定發布 workflow 與 OCI archive 由共用 artifact verifier 驗證；主機
不重建程式、不登入 container registry，也不需要 GitHub token。

此流程只管理 canonical profile 指定的前端 Compose project，沿用同一份 env、CIDR、
API 與 catalog pins。API／BFF 必須是目前的 contract4；不部署後端、模型或索引。

## 發布版本

PR 的 CI 使用 GitHub-hosted runner 與唯讀權限，不持有內網部署憑證。
main 通過必要檢查並合併後，由維護者在原始 repository 手動指定完整 commit：

```sh
gh workflow run frontend-release.yml --repo ThreeMonth03/depositarAI \
  --ref main -f revision=FULL_40_HEX_COMMIT
```

若目前 main 已改變，workflow 拒絕發布。它在隔離 runner 建置並測試兩個映像，
匯出原本通過測試的 OCI bytes，再由獨立 job 使用 GitHub 官方 attestation 簽署
涵蓋兩個 archive 的 manifest。所有資產上傳與核對完成後才公開 release。
同一 commit 的已存在 tag／release 不覆寫；伺服器沒有接受 workflow 的入站連線。

## 主機設定

主機需要 Linux、Docker 29 containerd image store、Docker Compose、Python 3，以及
已安裝並驗證來源的官方 `gh` executable。維護帳號需要既有 Docker 權限。
下載與驗簽需要對 GitHub、GitHub release asset 和 Sigstore 信任根的 HTTPS 連線。
隨附簽章 bundle 不代表可以完全離線驗證。
先以 pinned operator source 手動通過 schema3 的
[現役前端 checker](FRONTEND_CHECK.md)，並準備已運作的監控鎖。

依[設定範例](../../deploy/env/frontend-delivery.example.json)建立 Git 外的私有設定。
設定檔與 canonical `frontend-check.json` 使用 `0600`、直接父目錄 `0700`；state root
須為 Git 外已存在、由維護帳號擁有的獨立 `0700` 目錄。`gh_binary` 使用絕對路徑，
不得經過 symlink，也不得可由其他帳號寫入。

| 欄位 | 用途 |
| --- | --- |
| `schema_version` | 整數 `1`。 |
| `repository` | 固定 `ThreeMonth03/depositarAI`。 |
| `repository_id` | GitHub repository 的數字身分，目前為 `1425391257`。 |
| `state_root` | 此工具專用的私有交易目錄。 |
| `frontend_check_file` | 已驗證、現役 schema3 canonical profile 的絕對路徑。 |
| `gh_binary` | 已信任的官方 GitHub CLI executable 絕對路徑。 |
| `monitor_lock_file` | 既有監控 wrapper 使用的 `.check.lock` 絕對路徑。 |

監控繼續使用同一份 canonical profile；不用另外架設監控服務，也不在 cron 執行
自動 `git pull`。首次接管既有部署時，工具必須實際通過 checker，才將其視為可回退基準。
例如已核准的早期 contract4 部署不需要重新發行成 CI release 才能作為首次基準。

## 部署與確認

```sh
python3 -I -S -B /absolute/pinned/operator-source/scripts/deploy_search_web.py \
  --config /absolute/private/frontend-delivery.json deploy --revision FULL_40_HEX_COMMIT

python3 -I -S -B /absolute/pinned/operator-source/scripts/deploy_search_web.py \
  --config /absolute/private/frontend-delivery.json status
```

部署先下載並驗證 manifest、attestation 與兩個 OCI archives，再使用既有 checker
確認現役 API／runtime／CIDR。未通過來源驗證時不執行 Docker 命令。通過後才載入映像，
核對平台 digest、source／role label；版本化 local tag 只用來定位映像，不能代替 digest 證據。
Docker 29 不一定能直接用平台 manifest digest 當作 image locator，因此 Compose 使用
該版本的 local tags，切換前後皆重新核對平台 pins。

整個操作持有自身 state lock；只有重新確認基準、關閉 Nginx、更新容器、checker 與
發布 canonical profile 的切換期間，才持有既有 monitor lock。更新固定使用
`--no-build --pull never --wait`。新的 checker 通過後，才原子替換 canonical profile 與
current／previous 狀態。同版本再次部署只重新檢查身分，不重啟容器。

輸出只包含固定狀態與 current／previous revision，不輸出 env、key、原始子程序診斷或私有路徑。
成功代表 runtime、靜態網站、ACL 與 API TLS/version 路徑已通過；一般部署不提交模型 query，
因此完整搜尋品質與實驗室路由驗收仍應另外執行。

## 失敗與回退

```sh
python3 -I -S -B /absolute/pinned/operator-source/scripts/deploy_search_web.py \
  --config /absolute/private/frontend-delivery.json rollback
```

切換失敗會自動恢復原 profile 與已核對的映像，再驗證恢復結果；命令仍以非零退出，
並回 `frontend_delivery_cutover_failed_rolled_back`。正常 `rollback` 回到最近一次 previous，
成功後剛退下的版本成為新的 previous。

若操作中斷、主機重開或無法安全恢復，保留的 `intent.json` 會阻止新的 deploy。
`status` 回報 `recovery_required`；這時 `rollback` 只恢復 intent 記錄的切換前狀態。
若發現陌生容器、network 被替換或 canonical profile 被其他流程修改，工具拒絕盲目停止
它們。先由維護者核對來源，再使用保留的 intent 恢復，不要手動刪掉 intent 來壓掉錯誤。

State 只保留 current、最近一次 previous 及必要的中斷 intent；下載暫存資料會清除，
不保留舊 source worktrees、模型或資料。previous 的映像仍須留在 Docker 本機；共享
Docker image cache 的清理屬於主機維護，這個命令不執行全域 prune。

## CPU 回歸

```sh
python3 -m unittest tests.unit.deployment.test_frontend_delivery -v
```

測試以假的下載／Docker 邊界搭配真正的 checker，涵蓋壞 attestation、API 配對失敗、
健康檢查失敗恢復、重複部署、回退、鎖競爭與陌生容器拒絕；不操作現役服務或 GPU。
