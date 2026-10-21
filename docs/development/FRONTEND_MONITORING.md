# 私有前端定時健康檢查

這個方案以 systemd user timer 定時執行既有的
[`check_search_web.py`](../../scripts/check_search_web.py)，不增加監控服務或資料庫。
範例檔不會自行安裝、啟用或修改現役部署。它適用於獨立的
`depositar-search-web` 前端，從前端 VM 本機檢查 `127.0.0.1:18081`，不需要維持筆電 SSH tunnel。
公眾 APISIX／Anubis 入口有不同的拓樸，不能直接拿這個 checker 當作其完整驗收。

檢查核對容器的 image／revision、安全設定與網路邊界、首頁及 JS／CSS，再從現役
BFF 內驗證上游 HTTPS：匿名 `/v1/version` 須回 401，使用既有服務 key 後須回 200，
且 API revision 與 catalog manifest 符合指定部署收據。key 只在 BFF 內讀取，
不放入 unit、命令列或 journal。詳細邊界見[唯讀檢查說明](FRONTEND_CHECK.md)。

這些檢查不送搜尋 query、不建立搜尋 session、不呼叫 `/readyz` 或模型，也不重啟、
重建或更新服務。通過只能證明當下私有前端與 TLS version 路徑正常，不能證明 GPU 推論、
模型常駐 VRAM、搜尋品質、OIDC、實際防爬挑戰或外部使用者的網路路徑正常。

## 先手動核對

操作帳號需要既有 Docker 權限。將 checker 放在已檢閱、固定 Git revision 的 operator
source 目錄；不要讓 unit 指向會被其他開發流程改寫的 checkout，也不要排程 `git pull`。
operator source 的 revision 可以與現役前端不同；受檢版本與 image IDs 仍須來自已驗證的
部署收據，不能從當下服務回應反過來接受新身分。

依[設定範例](../../deploy/env/frontend-check.example.json)建立 Git 外的 check 設定。
設定檔與既有 frontend env file 應由操作帳號擁有，權限使用 0400／0600，直接父目錄 0700。
先替換下列絕對路徑，手動執行：

```bash
/usr/bin/python3 -I -B /absolute/pinned/operator-source/scripts/check_search_web.py \
  --config /absolute/private/frontend/check.json
```

成功會輸出固定結構的 JSON，`status` 為 `frontend_checked`，退出碼為 0；失敗退出碼為 1，
並回固定錯誤 JSON。先處理失敗原因，再排程；不要修改預期 revision、image 或 manifest
來消除警示。此步只執行唯讀 checker，不會送 query。

## 安裝 user timer

使用範例前先確認 VM 有可用的 user service manager：

```bash
systemctl --user is-system-running
loginctl show-user "$(id -u)" --property=Linger
```

`is-system-running` 若回 `degraded`，先確認既有失敗服務是否影響這次檢查；若無法連線到
user manager，先處理 session／主機支援，勿略過錯誤宣稱 timer 已運作。user manager 通常
在登入後啟動；未啟用 linger 時，登出後可能停止。啟用 linger 才能讓 user manager 在開機時
啟動並在登出後繼續執行，實際權限由主機管理政策決定。需要管理員協助時，請由管理員設定；
此方案不修改系統設定，也不保證目前環境重開後一定執行。
參考 [systemd timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
及 [loginctl linger](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html)。

複製兩個範例到自己的 user units 目錄：

```bash
install -d -m 700 "$HOME/.config/systemd/user"
install -m 600 \
  /absolute/pinned/operator-source/deploy/systemd/depositarai-frontend-check.service.example \
  "$HOME/.config/systemd/user/depositarai-frontend-check.service"
install -m 600 \
  /absolute/pinned/operator-source/deploy/systemd/depositarai-frontend-check.timer.example \
  "$HOME/.config/systemd/user/depositarai-frontend-check.timer"
```

編輯剛複製的 `.service`，將 `WorkingDirectory`、`ExecStart` 中的 operator source 與
private config 路徑改為前一步手動驗證成功的絕對路徑。不要填入 API key、env 內容或通知憑證。
保留 `Type=oneshot`、`TimeoutStartSec=120`、`UMask=0077` 與 `NoNewPrivileges=yes`。
`RuntimeMaxSec` 對 oneshot 不提供執行時限；不能用它取代 `TimeoutStartSec`。
不要加入 `RemainAfterExit=yes`，否則服務成功後維持 active，timer 不會再次執行。
這些行為見 [systemd service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。

驗證檔案，再只執行一次 service：

```bash
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/depositarai-frontend-check.service" \
  "$HOME/.config/systemd/user/depositarai-frontend-check.timer"
systemctl --user daemon-reload
systemctl --user start depositarai-frontend-check.service
systemctl --user show depositarai-frontend-check.service \
  --property=Result --property=ExecMainCode --property=ExecMainStatus
journalctl --user -u depositarai-frontend-check.service --no-pager -n 20
```

確認結果成功後再啟用 timer：

```bash
systemctl --user enable --now depositarai-frontend-check.timer
systemctl --user list-timers --all depositarai-frontend-check.timer
```

timer 在 user manager 啟動約兩分鐘後可執行，之後約每五分鐘一次，另有最多 30 秒的隨機延遲。
若第一次啟用時啟動時間已經過去，可能立即執行。相同 service 尚未結束時不會疊加新實例；
service 的 120 秒時限限制這次檢查，逾時屬失敗，不代表應重新啟動應用服務。

## 查看結果與接入既有告警

checker 的 JSON 與 systemd 執行結果記入 journal。用上面的 `show` 指令區分
`Result=success`、`exit-code` 或 `timeout`，並核對日誌時間與 timer 的最近／下次執行時間。
單看 timer 為 active 或舊的成功紀錄，不能判斷現在健康。journal 的保留期限與重開後是否保留，
依主機 journald 設定；本方案未另建長期儲存或遠端備份。

**這裡沒有外部通知通道，寫入 journal 不等於已發出告警。**
若機構已有監控，可由它接收這個 user unit 的 failed 狀態，另檢查是否長時間沒有新結果。
若之後要使用 email 或 webhook，需另行指定接收對象、通道與授權，並規劃重複失敗抑制及恢復通知；
不要把通知 secret 放入 unit 或 Git。本方案未寄信、傳送訊息或安裝通知工具。

VM 關機、user manager 停止、timer 被停用或主機網路完全中斷時，本機 checker 不能自行
通知外界；需要其他主機或機構監控觀察這種失聯。Docker 內建 healthcheck 與產品 CLI
`status` 也不能替代這個檢查：後者是本地部署紀錄，固定回報 `runtime_verified=false`。

## 停用

```bash
systemctl --user disable --now depositarai-frontend-check.timer
systemctl --user stop depositarai-frontend-check.service
systemctl --user list-timers --all depositarai-frontend-check.timer
```

停用只影響排程與正在執行的 checker，不會停止應用容器或刪除部署資料。
後續更換 operator source 或部署身分時，先停用、手動驗證新組合，再更新路徑與設定、
`daemon-reload` 並重新啟用；不要自動改寫原部署收據。
