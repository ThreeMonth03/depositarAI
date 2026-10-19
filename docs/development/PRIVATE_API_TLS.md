# 私有 Conversation API 的獨立 TLS 接口

這是供核准的 VM BFF 使用的服務接口，不是公眾網站或整套產品 installer。
既有 `18083`、catalog、模型與 `active.conf` 保持原狀；公眾登入與配額仍須在網站入口處理。

```text
VM BFF → 主機 IPv4:443（outer，來源 /32 白名單）
       → 已驗 CA/SAN 的 API-IP:8443（inner，共用指定 API 容器的 network namespace）
       → 127.0.0.1:8000（原 Conversation API，仍驗證服務 Bearer key）
```

Outer 使用 host network，不產生 Docker 443 DNAT，主機來源規則仍須由管理員確認。
Inner 不新增 website network peer，因此保留現役 gateway 的四個 peer 契約。
API 位址被重用時，outer 先驗 inner TLS 身分，驗證失敗不轉送 HTTP Authorization。
兩個代理都不持有 API key、Docker socket 或 CA signing key。

Inner 以 operator UID/GID、無 capabilities 執行。Outer 採標準 Nginx root master，
worker 降為 image 的 `nginx` 使用者；唯讀 rootfs、獨立 PID namespace、
`no-new-privileges` 與 `cap_drop: ALL` 保留。只加 SETUID／SETGID（worker 降權）、
DAC_OVERRIDE（讀 operator 0600 的唯讀設定與 leaf key）、CHOWN（初始化 worker temp
目錄），以及低埠需要的 NET_BIND_SERVICE。隔離實測已重現 non-root exec 只保留
capability bounding set、無法綁 443，因此不能把 `cap_add` 當作非 root 低埠保證；
未修改主機 sysctl，也不增加 SYS_ADMIN／NET_ADMIN 或 privileged。

將 [設定範例](../../deploy/env/private-api-tls.example.json) 複製到 Git 外的私有目錄，填入
現役 release compose、完整 API container ID、固定 Nginx image、兩個 TLS server name
與五個 TLS 檔案路徑。範例 IP 與網域僅供說明，須改成部署環境的值。
設定、compose、TLS 檔與 `active.conf` 要求 operator 所有、0400/0600，直接父目錄 0700；
不可使用 symlink。TLS public certificate 也採此主機材料權限，傳給 VM 的 CA 公憑證可另存。
`outer_server_name` 是 VM 驗證的入口名稱，`inner_server_name` 是 outer 驗證的內層名稱；
兩者須不同，且為至少兩個 label、總長不超過 253 字元的小寫 ASCII DNS 名稱，單一 label
最多 63 字元，只允許英數字與中間的連字號。不接受 IP、wildcard、尾端句點或 URL。
兩張 leaf 的 DNS SAN 必須各自明確含對應設定值，例如 `api.example.test` 與
`api-inner.example.test`；不以 CN 或 wildcard 代替，並各用不同 private key。
共用 CA 公憑證，CA private key 由 operator 另外保管。

```bash
python3 scripts/private_api_tls.py prepare \
  --config /absolute/private/endpoint.json \
  --output /absolute/private/endpoint-v1
python3 scripts/private_api_tls.py check --plan /absolute/private/endpoint-v1/plan.json
python3 scripts/private_api_tls.py up --plan /absolute/private/endpoint-v1/plan.json
python3 scripts/private_api_tls.py stop --plan /absolute/private/endpoint-v1/plan.json
```

`prepare` 要求新的輸出目錄，封存設定（包含兩個 server name）、TLS 材料 hash、原四個 peer、
API process／namespace 與 route hash，不讀 API key bytes。`check` 重新核對這些條件與憑證鏈、hostname、key 配對，
且 leaf 至少還有 24 小時有效期。`up` 僅啟動本工具兩個服務，前後核對相同 backend，
失敗清理與 `stop` 只停止／移除此 plan 已證明擁有的完整 container IDs。
未知同名 project、錯 image、mount、command、network mode 或 privilege 會拒絕操作。
`stop` 不要求 backend 健康或憑證仍有效，因此可在故障時使用；設定與輸出材料須完整。

只提供 `POST /v1/turn` 與 `GET /v1/version`；HEAD、其他方法、其他路徑與 query 都拒絕。
保留 8 KiB body、有限連線、5 秒 header/body timeout、不重試與不記 access logs。
Bearer key 只由 VM BFF 送出，仍由原 API 驗證。VM 必須明確信任 CA 並驗證 hostname；
不得用 `verify=False` 或 `curl -k` 代替。瀏覽器到 VM 的 HTTPS 是另一條信任鏈。

`nginx -t` healthcheck 只證明配置與材料可載入，`up` 回覆不宣稱 HTTP、模型或公眾驗收。
需從核准 VM 以 CA 驗證 TLS，先測無 key 回 401、錯路徑拒絕，再依授權做服務驗收。
請不要對同一 endpoint 同時操作多個 plan。

server name、憑證或 backend container／process／namespace／位址改變時，不會自動 renew 或 rebind。
先 `stop --plan OLD/plan.json`，準備新憑證或核定新的 backend，再用新的私有目錄
`prepare`／`check`／`up`。保留舊材料供診斷；不要修改原 API、其網路或 active route 來配合。
這是操作時的身分檢查，不是持續監控器；backend 變動須安排停止與明確 rebind。

隔離 CPU 回歸（只使用既有不可變 images，不呼叫真實 API／模型）：

```bash
DEPOSITAR_TEST_PRIVATE_API_TLS_RUNTIME=1 \
  python3 -m unittest tests.integration.test_private_api_tls_runtime -v
```
