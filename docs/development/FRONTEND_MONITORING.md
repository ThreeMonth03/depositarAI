# 私有前端定時健康檢查

這個方案以 systemd user timer 或既有 cron 定時執行
[`check_search_web.py`](../../scripts/check_search_web.py)，不增加監控服務或資料庫。
範例檔不會自行安裝、啟用或修改現役部署。它沿用 schema3 profile 明確選擇
`private` 或 `intranet-http`，從前端 VM 連到已驗證 Compose 的綁定位址，不需要筆電 SSH tunnel。
內網模式也會核對 CIDR 規則與容器實際掛載內容。
公眾 APISIX／Anubis 入口有不同的拓樸，不能直接拿這個 checker 當作其完整驗收。

檢查核對容器的 image／revision、安全設定與網路邊界、首頁及 JS／CSS，再從現役
BFF 內驗證上游 HTTPS：匿名 `/v1/version` 須回 401，使用既有服務 key 後須回 200，
且 API revision、catalog manifest、contract4 與平台映像身分符合指定部署收據。key 只在 BFF 內讀取，
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
/usr/bin/python3 -I -S -B /absolute/pinned/operator-source/scripts/check_search_web.py \
  --config /absolute/private/frontend/check.json
```

成功會輸出固定結構的 JSON，`status` 為 `frontend_checked`，退出碼為 0；失敗退出碼為 1，
並回固定錯誤 JSON。先處理失敗原因，再排程；不要修改預期 revision、image 或 manifest
來消除警示。此步只執行唯讀 checker，不會送 query。

## 選擇排程方式

先確認目前 SSH shell 與 user manager 的群組是否一致：

```bash
id
systemd-run --user --wait --pipe --collect /usr/bin/id
```

若 SSH 的 `id` 有 `docker` 群組，user manager 的輸出卻沒有，代表它可能仍保留加入群組前的
身分。新登入的 shell 能使用 Docker，不能證明 user service 也有同樣權限。不建議為了這項
監控重啟整個 user manager，以免中斷其他 user services。若 user manager 群組未更新，或
`Linger=no` 而機構政策不允許自行啟用，可用下方 cron 方案，並實際驗證 cron 的執行結果。
同一個 checker 選一種排程即可，避免 timer 與 cron 重複執行。
現役入口只保留一份 canonical profile；替換入口時先保存舊設定，再更新這個排程的
operator source／profile 路徑。已退役 demo 的排程須一併移除，不能以它的成功紀錄代表新入口。

## 使用既有 cron

此方案需要主機既有的 cron daemon 正常運作，以及操作帳號已有 Docker 權限；不需要
更改系統權限或 user manager。採用
[`depositarai-frontend-check.sh.example`](../../deploy/cron/depositarai-frontend-check.sh.example)，
只執行既有 checker，不呼叫產品 `status`、`doctor`、模型或搜尋 API。

先確認私有目錄位置；若目錄尚不存在，由操作帳號建立，權限 0700。若已存在，先核對其擁有者、
權限及用途，不要直接修改不明目錄。wrapper 要求目錄已存在、屬於執行帳號、權限恰為 0700，
且本身不是 symlink；它不會自行建立目錄。

```bash
mkdir -m 700 /absolute/private/frontend-monitor
stat -c '%U %a %F' /absolute/private/frontend-monitor
install -m 700 \
  /absolute/pinned/operator-source/deploy/cron/depositarai-frontend-check.sh.example \
  /absolute/private/frontend-monitor/check-frontend.sh
```

編輯剛複製的 wrapper，只替換三個固定絕對路徑：`operator_source`、`frontend_config`、
`monitor_root`。operator source 必須固定在已檢閱的 revision，private config 必須是前面
手動驗證成功的設定。不要加入 API key、env 內容或通知憑證。

```bash
sh -n /absolute/private/frontend-monitor/check-frontend.sh
/absolute/private/frontend-monitor/check-frontend.sh
cat /absolute/private/frontend-monitor/last-run.json
cat /absolute/private/frontend-monitor/last-check.json
```

wrapper 使用 `umask 077` 與非阻塞 `flock`（fd 9）避免重疊。每次 checker 最長執行 120 秒，
逾時先送 TERM，另給 10 秒後才強制結束。正常逾時退出碼為 124；若必須強制 KILL，可能是 137，
會保留實際非零退出碼。stdout／stderr 僅存私有暫存檔，完成後以同目錄 `mv` 取代
`last-check.json`；另一份原子更新的 `last-run.json` 記錄 UTC `checked_at` 與 `exit_code`。
兩份檔案分開更新，檢查途中或寫入失敗時可能暫時不同步。wrapper 退出時清除自己的暫存檔。
`last-check.json` 保留原始輸出；逾時或程序異常時可能是空檔或不完整 JSON，不能只靠它解析健康。

排程前先備份目前 crontab，備份也須放在私有目錄，因為其他 jobs 可能含機敏參數：

```bash
umask 077
cron_backup=/absolute/private/frontend-monitor/crontab.before.$(date -u +%Y%m%dT%H%M%SZ).txt
crontab -l > "$cron_backup"
```

確認備份成功後才編輯。若原本沒有 crontab，`crontab -l` 會回報沒有排程並退出 1，備份可為空；
若是其他錯誤，先處理，不能直接覆蓋。用 `crontab -e` 保留所有原有內容，只新增這一條：

```cron
*/5 * * * * /absolute/private/frontend-monitor/check-frontend.sh
```

再以 `crontab -l` 確認只新增一次。wrapper 已將其他 stdout／stderr 丟棄以避免 cron 寄信，
只透過 `logger` 寫固定摘要與退出碼，不把 checker 輸出、環境變數或私有路徑送入系統日誌。
這不是外部通知通道。

等下一個五分鐘刻度後驗證：

```bash
date -u '+%Y-%m-%dT%H:%M:%SZ'
cat /absolute/private/frontend-monitor/last-run.json
cat /absolute/private/frontend-monitor/last-check.json
stat -c '%a %n' /absolute/private/frontend-monitor/last-run.json \
  /absolute/private/frontend-monitor/last-check.json
journalctl -t depositarai-frontend-check --no-pager -n 10
```

正常時 `exit_code` 為 0、兩份結果權限為 600，`checked_at` 必須是排程新增後的新時間；
視排程刻度與檢查時間，最多先等一個五分鐘週期加約 130 秒。能讀到舊成功檔案不能證明 cron
正在運作。日誌讀取權限及 `logger` 的儲存位置由主機政策決定；以私有結果檔及時間作為本機驗證依據。
本方案不另外輪替歷史 checker 輸出，只保留最近一次結果。

VM 關機或 cron 停止時，不會產生新的 `last-run.json`。需要另一台主機或機構監控檢查
`checked_at` 是否超過容許時間，例如 15 分鐘；本機檔案本身無法對失聯發出告警。
停用時用 `crontab -e` **只移除這一條 job**，再用 `crontab -l` 確認，勿使用 `crontab -r`
或覆蓋其他人的排程。若已有一次 checker 正在執行，移除 job 不會中止該次執行，也不影響應用容器。

## 使用 user timer

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
