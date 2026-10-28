# 私有 demo 連線

本工具將既有 OpenSSH 跳板串成一個前景連線，讓另一台電腦以
`http://127.0.0.1:18081` 展示已部署的獨立前端。它不部署網站、不修改
SSH 授權／防火牆，也不啟動 Docker、搜尋 session 或模型推論。

## 準備一次私有設定

執行工具的電腦需要 Python 3、OpenSSH 與本 repo checkout；目前支援 Linux／macOS
等 POSIX 主機。每一段 SSH 連線須已經能以非互動方式通過驗證，並有可信的
host key。工具強制驗證 known_hosts；不提供略過驗證或自動接受未知 host key 的模式。

```sh
cp deploy/env/demo-connection.example.json /absolute/private/demo.json
chmod 600 /absolute/private/demo.json
```

範本是待填設定，不代表任何站點已部署或已授權。檔案須由目前使用者擁有、
權限為 `0400`／`0600`、不是 symlink，且不超過 16 KiB。填寫下列欄位：

| 欄位 | 用途 |
| --- | --- |
| `browser_origin` | 第一版固定 `http://127.0.0.1:18081`，與既有 BFF `WEB_ORIGIN` 配對。 |
| `remote_port` | 前端 VM 的 loopback port，固定 `18081`。 |
| `hops` | 依序列出一至三個 SSH 目的地；可以是 SSH alias 或 `user@host`。 |
| `hops[].ssh_config` | 該段 SSH client 所在主機上的設定檔絕對路徑；`null` 使用該主機的預設設定。 |
| `hops[].local_forward_port` | 在該段 SSH client 所在主機建立的 loopback port；第一段固定 `18081`，其他段為未占用的非特權 port。 |
| `remote_checker` | 最後一台主機上已驗證版本的 `scripts/check_search_web.py` 絕對路徑。 |
| `remote_profile` | 最後一台主機上的正式 `frontend-check.json` 絕對路徑。 |
| `expected` | 從已完成部署的收據取得 frontend/API 完整 Git SHA、contract integer `4` 與 catalog manifest SHA256；不以網站自行回報的值建立預期值。 |

`hops` 的第二段 SSH client 在第一段目的地主機上執行，第三段則在第二段目的地主機
執行。因此 VM key 可以留在原本的伺服器上；不用複製私鑰到展示電腦，也不使用
SSH agent forwarding。各主機的 SSH config 是操作者信任的執行設定，須妥善管理其
`IdentityFile`、`ProxyJump`／`ProxyCommand` 與 known_hosts。只設定 `ProxyJump`
不會讓本機自動取得遠端主機上的私鑰。

例如範本的兩段設定代表：

```text
展示電腦 :18081 → demo-gpu 上的 :38081 → demo-vm 的 :18081
```

第一段 `/absolute/private/client-ssh.conf` 屬於展示電腦；第二段
`/absolute/private/server-vm-ssh.conf` 屬於 `demo-gpu`。若連線還需要登入跳板，
可使用已核准的 SSH alias 設定，或增加一段 hop。

## 檢查與開啟

```sh
python3 scripts/demo_connect.py check --config /absolute/private/demo.json
python3 scripts/demo_connect.py connect --config /absolute/private/demo.json
```

`check` 建立自己的暫時通道，完成檢查後關閉；`connect` 完成相同檢查後顯示
`demo_ready` 與網址，保持終端機開啟，使用 Ctrl+C 結束。連線中斷會退出，重新執行
相同指令即可；工具不在背景偷偷重試或採用已存在的 tunnel。

兩者都會先透過不配置 TTY 的 SSH chain 執行既有唯讀 checker，核對實際容器、
frontend/API revision、catalog、contract、上游 TLS 與授權狀態。之後建立自己的
loopback tunnel，僅送出首頁、`/healthz` 與帶精確 Origin 的 `/api/access` GET。
`/healthz` 與 `/api/access` 本身不證明版本；成功結果必須來自本次遠端 checker，
不能沿用歷史檢查檔案。

中間 hop 的 tunnel 使用強制 TTY，使外層 SSH 結束時透過 HUP 清理遠端子連線；
最末段使用 `-N -T`。所有段均使用嚴格 host key 驗證、keepalive、forward failure
退出與停用 agent forwarding／ControlMaster。工具只清理自己建立的本機 process
group，不使用 `pkill ssh`，也不關閉別人的連線。Ctrl+C、SIGTERM 與 SIGHUP
都會先清理自己建立的連線再退出。

工具等待最末段 SSH client 完成 forwarding 後，透過固定的 OpenSSH `LocalCommand`
輸出本次隨機 nonce，再開始本機 HTTP 檢查。這個就緒訊號來自同一串連線；不會因
中間 port 已有舊 listener 而誤認建立成功。只有最末段允許工具內建的固定 `printf`
命令，profile 不接受自訂 LocalCommand；其他段仍停用 LocalCommand。

## 常見失敗

| stage／code | 檢查方向 |
| --- | --- |
| `profile / profile_permissions_invalid` | 設定擁有者、權限與檔案大小。 |
| `remote_check / remote_check_failed` | SSH alias、金鑰、可信 host key、既有 bridge，以及最後一台主機的 checker／profile。 |
| `remote_check / deployment_identity_mismatch` | 連到舊網站，或部署已更新但私有 demo profile 尚未同步。先核對收據。 |
| `tunnel / local_port_in_use` | 本機 `18081` 已占用；工具保留原 listener，請由操作者確認用途。 |
| `tunnel / tunnel_closed` | 某段 SSH 連線結束，或中間 forward port 已占用。 |
| `http / origin_denied` | BFF 的 `WEB_ORIGIN` 與 canonical demo origin 不符。 |
| `http / tunnel_not_reachable` | SSH 已建立，但通道末端 HTTP 服務無法連線。 |

HTTP 不跟隨 redirect、不採用環境 proxy，且限制總時間與回應大小。SSH 的擷取輸出
也有大小／時間上限；錯誤 JSON 不輸出原始 SSH 訊息、HTTP body、服務金鑰或 session token。
成功只證明本次部署身分與 HTTP 連線，不表示搜尋品質或 GPU 推論已重新驗收。

## 橋接本身的生命週期

本工具重用既有 bridge；它不保證 VM 與 GPU 原本就有直連路由，也不管理橋接
本身的啟停。本版本沒有安裝常駐服務或新增金鑰；原橋接需要保持連線。
