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

schema 1 設定使用已接入 native gateway 的 release。`prepare` 要求新的輸出目錄，封存設定（包含兩個 server name）、TLS 材料 hash、原四個 peer、
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

## 不接入 native gateway 的候選 release

使用 [schema 2 範例](../../deploy/env/private-api-tls-isolated.example.json)，明確指定
`backend_mode: "isolated-release"`。它只接受 `start-release` 已準備的三個 CPU
服務：`web`、`conversation-api`、`filtered-embedding`，website network 必須恰好只有
這三個角色。未知 peer、重複角色、gateway peer、映像不符、服務不健康或發布 host
port 都拒絕，不會從 peer 數量猜測或自動切換模式。

這個模式封存 release Compose 的完整 bytes SHA256、三個 immutable image／container
IDs、process PID／StartedAt、network namespace、網路名稱／ID／位址，以及 API
身分。`check` 重新核對，`up` 啟動前後各重新核對；release 內容或 peer 重啟後須
安排新 plan。它不讀寫 native gateway 的 `active.conf`，也不連接、重建或停止
native gateway。schema 1 仍要求原來的四個 exact peers 與 route hash；不接受
將 schema 2 欄位塞進舊設定或改寫舊 plan。

prepare／check／up／stop 的命令相同。候選 release 可與舊 release／gateway 的容器
及網路並存；它們的 project 與網路必須不同。TLS CLI 仍使用獨立但固定的
`depositar-private-api-tls` project，兩套 outer 也不能同時佔用同一個 host address／port。
先使用原 operator 與舊 plan 執行 `stop`，保留舊 plan、來源及材料，再為新 release
建立新 plan 並啟動。`stop` 只移除已證明屬於該 plan 的 stateless TLS 容器；回退時
先停新 plan，再以舊 operator／舊 plan 重建舊代理。若舊 API process 或 network
namespace 也變了，舊 plan 已不相符，須明確重新 prepare，不能直接重用。

### 與 GPU lifecycle 的維護順序

TLS outer 的低埠／worker 權限會被 native GPU ownership preflight 視為不支援的
container device access。因此先暫停網站入口、停止 TLS，再完成
`start-inference`／`start-release` 的所有原生 GPU ownership、暖機、兩輪搜尋與
reset 驗收，最後才啟動 TLS 與對應版本的 VM 前端。以後重跑這些 lifecycle
檢查（包含 gateway preparation／activation）前，也必須先停止 TLS；TLS 開啟後的
一般 API 搜尋不會執行該 host preflight。

這不是整套產品的自動 update／rollback。停止模型容器可保留 exact container IDs，
但重啟後 PID／StartedAt 改變，原 native 完成收據不會恢復；新候選停止後也沒有
自動 resume／reseal。維護者須另外保存相容的 API／BFF／前端、修復後的模型容器
身分與驗收證據，安排人工回退，不刪除或改寫原收據。API 契約改動時，VM 前端
與 API 必須配對切換／回退，維持暫停入口至驗收完成。

server name、憑證或 backend container／process／namespace／位址改變時，不會自動 renew 或 rebind。
先 `stop --plan OLD/plan.json`，準備新憑證或核定新的 backend，再用新的私有目錄
`prepare`／`check`／`up`。保留舊材料供診斷；不要修改原 API、其網路或 active route 來配合。
這是操作時的身分檢查，不是持續監控器；backend 變動須安排停止與明確 rebind。

隔離 CPU 回歸（只使用既有不可變 images，不呼叫真實 API／模型）：

```bash
DEPOSITAR_TEST_PRIVATE_API_TLS_RUNTIME=1 \
  python3 -m unittest tests.integration.test_private_api_tls_runtime -v
```
