# 公眾入口：登入、使用額度與部署維護

公眾入口是選用的產品服務：首頁與使用說明公開，AI 搜尋需要免費 OIDC 登入。
入口使用 Apache APISIX 與持久化 Redis；專案只補上可信身分與暫時搜尋 session
的銜接，不自行維護密碼、註冊、忘記密碼或 OAuth 協定實作。

這個入口是另一組 CPU 服務，沿用已啟動的私有 conversation API。既有私有試用
`127.0.0.1:18083`、模型、索引與 active release 不因準備入口而切換。
**入口準備完成或離線測試通過，都不等於已對公眾開放**：正式網域、HTTPS 憑證、上游入口
及真實 OIDC client 仍須由部署者配置並驗收。這不是整套產品的新主機 installer。

| 操作者要做的事 | 閱讀位置 |
|---|---|
| 確認登入、配額與網路前提 | [元件與網路邊界](#元件與網路邊界)、[控制範圍與配額語意](#控制範圍與配額語意) |
| 第一次設定與啟動 | [同主機準備與啟動](#同主機準備與啟動) |
| 更新或回退網站版本 | [更新與人工回退](#更新與人工回退)、[私有版本與狀態](#私有版本與狀態) |
| 檢查可否放行流量或人工暫停 | [驗收與暫停](#驗收與暫停) |

## 元件與網路邊界

```mermaid
flowchart LR
    Browser[公眾瀏覽器] --> TLS[既有 HTTPS 對外入口]
    TLS --> APISIX[APISIX：主機 loopback 18084]
    APISIX --> Frontend[Nginx public-frontend]
    Frontend --> Web[Django public-web]
    APISIX --> Redis[私有 Redis 使用額度]
    APISIX --> OIDC[外部 OIDC 登入服務]
    Web --> Conversation[既有私有 conversation API]
    Conversation --> Search[私有模型與搜尋索引]
```

APISIX 採檔案設定的 standalone 模式，無需 etcd，停用 Admin API 與 control API。
這些是 [APISIX 部署模式](https://apisix.apache.org/docs/apisix/deployment-modes/)
及開源 [OIDC plugin](https://apisix.apache.org/docs/apisix/plugins/openid-connect/)
提供的功能。採用版本與映像 digest 固定在
[profile.py](../../search/operations/deployment/public_entry/profile.py)。

Redis 使用固定的 8.4.2 映像，選用其 AGPLv3 開源授權；Redis 8 同時提供其他授權
選項，不能把每個 Redis 版本都當作同一授權。
參考 [Redis 授權說明](https://redis.io/legal/licenses/)。

公眾只接觸 HTTPS 對外入口；APISIX 預設僅發布 `127.0.0.1:18084`。
`public-web`、Redis、GPU 服務、OpenSearch、Fess 與管理介面都不發布主機 port。
新的 `public-web` 接入既有 release 的私有 website network，使用該 release 的
conversation API key。它不重啟模型、不重新 embedding，也不修改既有私有網站。

如果前端日後搬到另一台 server，仍應由對外入口把 `/api/*`、`/auth/*` 代理至受保護的
服務，維持同一個網站 origin。單純把畫面移走，不會替 API 增加存取控制。
目前準備工具以同主機的既有 release 為前提，尚未驗收跨機部署。

## 控制範圍與配額語意

| 項目 | 預設與行為 |
|---|---|
| 公開瀏覽 | 首頁、靜態素材與說明可讀；開首頁只查 `/api/access`，不建立搜尋 session |
| AI 搜尋 | `/api/access` 與 session／turn API 驗證登入；未登入回傳 `401 authentication_required` |
| 每個帳號 | 每個 86,400 秒視窗最多 10 次 turn 提交 |
| 公眾入口全站 | 每個 86,400 秒視窗最多 100 次 turn 提交；多帳號仍共用此上限 |
| session 建立 | 每個帳號每 3,600 秒最多 10 次建立；全站最多 100 次建立 |
| 暫存 session | 每個帳號最多 2 個同時存活的搜尋 session；既有 30 分鐘期限與 20 輪上限保留 |
| 請求限速 | 每個可識別來源 IP 每秒 3 次，burst 12；來源 IP 必須由可信代理提供 |
| 計數故障 | Redis 不可用時不降級為無限使用；新的受配額控制請求被拒絕 |

這是從該計數器第一個請求開始的固定視窗，**不是每天午夜重置**。
每人與全站計數器各有自己的視窗起點。登入、重新整理或換 IP 不會重置相同帳號的
配額；帳號鍵來自已驗證的 issuer 與 `sub`，不採信訪客提供的身分標頭。
固定視窗的邊界附近可能接連使用前後兩個視窗的額度，短期併發與逾時限制仍須保留。
計數規則使用 APISIX 的 [limit-count plugin](https://apisix.apache.org/docs/apisix/plugins/limit-count/)。
既有私有試用仍由可信操作者存取，不納入這個公眾入口的計數，也不能再直接代理到外網。

配額計的是 HTTP turn 提交請求，**不是實測 GPU 秒數、模型呼叫數或成功搜尋數**。
請求通過入口計數後，下游驗證錯誤、忙碌、逾時或手動重試可能仍消耗額度；多個
計數規則也不是會自動回滾的交易。第一版採保守計數，不自動補回額度，前端不自動重試。
`quota_exceeded`、`rate_limited`、`session_capacity` 與 `busy` 有各自的使用提示。

Redis 使用私有 bind mount、AOF、`appendfsync always` 與 `noeviction`，讓正常重啟沿用
計數；每次寫入同步到磁碟會增加延遲，仍需保留底層儲存的可靠性。
不可刪除 `redis-data`、執行清空資料或改用空目錄來更新入口，否則配額狀態會遺失。
磁碟損壞、遺失及人為清空仍須有備份／復原安排；持久化不等於異地備援。
參考 [Redis 持久化與 fsync 語意](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/)。

## 身分、session 與惡意輸入

APISIX 處理 OIDC authorization code flow、state／nonce／PKCE 與安全登入 cookie，並驗證
issuer／audience。小段入口銜接程式只從驗證完成的 claims 取得 issuer 與 `sub`，產生穩定的
雜湊帳號鍵，清除外來身分標頭，再加入受保護的 subject 與入口金鑰。
CPU BFF 的 [identity.py](../../search/web/identity.py) 驗證這兩項資料，將搜尋 session
綁定該帳號；其他帳號即使取得搜尋 token，也不能讀取、使用或刪除這個 session。

OIDC 登入 cookie 與搜尋 session bearer 各自處理；入口會保留 `/api/turn` 的
`Authorization: Bearer <search-session>`，不以 provider token 覆蓋。
前端只將搜尋 token 與對話留在記憶體，不寫入 localStorage／sessionStorage。
登入失效清除搜尋狀態並提示重新登入；搜尋 session 本身失效則提示「新搜尋」。

登入提供穩定的配額歸屬，**不證明是真人，也不防止多帳號、prompt injection 或大量
連線攻擊**。全站上限降低多帳號的運算消耗，原有輸入長度、嚴格 schema、固定上游、
最小索引權限、單一併發與逾時限制繼續提供程式邊界。這些限制不依賴模型遵守指令。

Anubis 尚未整合、打包或預設啟用。它可作為後續提高爬取成本的選用元件，但先要測
登入回跳、手機、輔助工具及誤擋。其[官方政策文件](https://github.com/TecharoHQ/anubis/blob/main/docs/docs/admin/policies.mdx)
也提醒有些機器人能通過挑戰，不能替代配額與全站上限。
大量流量若塞滿對外頻寬，同主機的 APISIX 無法自行解決；正式公開前須確認機構或
上游對外入口的 DDoS 處理能力與聯絡方式。

## 同主機準備與啟動

### 1. 準備外部登入與 HTTPS

沒有可用 SSO 時，可以先註冊 Google 的 **Web application** OAuth client；這是外部
身分服務，並非專案自行架設的開源帳號系統。到 Google Cloud 的 OAuth 設定建立
client，填入應用程式名稱、聯絡資訊與同意畫面，並把公開服務的完整 callback 註冊為：

```text
https://YOUR_PUBLIC_HOST/auth/callback
```

redirect URI 必須與入口設定完全相同，包括 scheme、host、port 與 path。
Google 的 discovery 是 `https://accounts.google.com/.well-known/openid-configuration`，
issuer 是 `https://accounts.google.com`。入口只要求 `openid` scope，使用穩定的 `sub`，
不需要 Google Drive 等 API 權限，也不依賴 email 作帳號鍵。
在測試狀態先加入測試帳號；對公眾推出前，完成 provider 要求的正式發布設定。
參考 [Google OpenID Connect 設定](https://developers.google.com/identity/openid-connect/openid-connect)。

對外 HTTPS 代理必須將網站路徑轉送至 `http://127.0.0.1:18084`，保留登入 cookie 與
`Authorization`，以設定好的 host 轉送；覆寫 `X-Real-IP` 為實際訪客 IP，設定 HTTPS 的
forwarded scheme／port，清除訪客提供的 Forwarded／X-Forwarded 身分資訊。
`trusted_proxy_cidrs` 只填 APISIX 實際看到的代理來源，不能使用 `0.0.0.0/0`。
若空白，入口不信任轉送 IP；所有請求可能共用代理 IP 的限速桶。
實際代理來源可能是 Docker bridge 位址，不能單憑代理的 loopback 目標猜測。

本工具不綁定主機 443、不申請憑證、不修改 DNS 或機構防火牆。
缺少以上條件時只完成私有準備，不宣稱已能安全公開。

### 2. 建立私有設定與 secret files

從 [public-entry.example.json](../../deploy/env/public-entry.example.json) 建立 Git 外的
`/absolute/private/public.json`，填入真實 `public_origin`、client ID、四個 secret file
路徑及 state root。所有路徑使用絕對路徑，不能使用 symlink 或與 repository 重疊；
設定檔與 secret files 應為操作帳號擁有的普通檔案，權限 `0600`（亦接受 `0400`），
其直接父目錄與 state directory 使用 `0700`。四個 secret file 路徑必須彼此不同。
不要將 secret 放進 Git、shell 命令參數、`.env` 範例或文件。

| Secret file 欄位 | 內容 |
|---|---|
| `oidc_client_secret_file` | Provider 發出的 client secret，單行、不含空白的 1–4,096 個可列印 ASCII 字元 |
| `oidc_session_secret_file` | 新產生的 64 個小寫十六進位字元，用於登入 session |
| `edge_secret_file` | 另一組獨立的 64 個小寫十六進位字元，保護入口至 BFF 的身分 |
| `redis_password_file` | 另一組獨立的 64 個小寫十六進位字元，供 Redis ACL 使用 |

三個隨機 secret 可各自使用 Python `secrets.token_hex(32)` 產生，存檔時採 `0600`，
不要列印到日誌或覆寫既有檔案。Provider client secret 另行填入。
`oidc_ca_file` 僅供需要私有 CA 的 provider；未設定時使用映像內建系統 CA，TLS 驗證不關閉。

若 Docker 預設位址池不足，可用選用的 `network_subnets` 明確指定三個小型私有
IPv4 網段；以下只是設定範例：

```json
{
  "network_subnets": {
    "website": "10.253.250.0/28",
    "quota": "10.253.250.16/28",
    "outbound": "10.253.250.32/28"
  }
}
```

使用前確認不與現有 Docker networks、主機路由及機構網段重疊；範例不是已保留的機構
配額。設定支援 `/24` 至 `/28` 的私有 IPv4 網段，三者不可互相重疊。
這個方法不清除既有 networks、不修改 Docker daemon，也不搬動現役 catalog。

### 3. 建置同一版本的前端與 BFF

網站由兩個映像組成：`web-nginx` 內含 React 靜態檔；`web` 提供 Django BFF。
兩者使用同一個已提交的 Git revision，共同構成一版網站；不分開發布或猜測 mutable tag。

```bash
revision="$(git rev-parse HEAD)"
for role in web web-nginx; do
  git archive "$revision" | docker build \
    --build-arg "SOURCE_REVISION=$revision" \
    -f "deploy/docker/$role/Dockerfile" -t "depositar-public-$role:$revision" -
done
docker image inspect --format '{{.Id}}' "depositar-public-web:$revision"
docker image inspect --format '{{.Id}}' "depositar-public-web-nginx:$revision"
```

記下兩個 `sha256:<64 hex>` image ID。BFF 必須有
`io.depositar.ai.public-entry.version=1`，前端必須有
`io.depositar.ai.frontend.version=1`，兩者來源 revision label 必須相同。
這些建置不下載模型、不重建索引。

### 4. 準備並啟動入口

使用既有私有 release 的 `compose.json`。入口設定獨立於產品 `--config`：

```bash
python3 scripts/depositarai.py prepare-public-entry \
  --entry-config /absolute/private/public.json \
  --release-compose /absolute/private/release/compose.json \
  --web-image sha256:YOUR_BFF_IMAGE_ID \
  --web-nginx-image sha256:YOUR_FRONTEND_IMAGE_ID

python3 scripts/depositarai.py up-public-entry \
  --entry-config /absolute/private/public.json \
  --release-compose /absolute/private/release/compose.json \
  --web-image sha256:YOUR_BFF_IMAGE_ID \
  --web-nginx-image sha256:YOUR_FRONTEND_IMAGE_ID
```

準備命令只驗證映像與設定、產生 Compose，不啟動服務。啟動前會核對私有 backend
仍在指定的內部 network、健康、使用指定映像與 API key，且沒有對外發布 port。
第一次啟動拒絕接管既有同 project 容器或非空 Redis 資料目錄。

Compose 負責建立 APISIX、Redis、Nginx 與 BFF。Nginx 只連入口的 `website` network，
沒有 API key、edge secret、GPU 或私有 backend network。BFF 在此網路使用 `web`
alias，另經指定的私有 release network 呼叫 conversation API。
APISIX 仍處理所有公開路由的登入、配額、身分清理與限速，再送往 Nginx。

## 更新與人工回退

第一版接受短暫維護中斷。更新會先關閉 APISIX 入口，再以 Compose 更新 Nginx/BFF
這兩個服務，等待 BFF 與前端首頁健康後重開入口，再檢查 loopback 首頁、health 與 JS／CSS 素材。
更新不重新建立 Redis、不移動其資料，也不更新私有 API、catalog 或 GPU 服務。
搜尋 session 留在 BFF 記憶體，網站更新或回退後需要重新搜尋；OIDC 與配額另由入口保存。

```bash
python3 scripts/depositarai.py update-public-entry \
  --entry-config /absolute/private/public.json \
  --release-compose /absolute/private/release/compose.json \
  --web-image sha256:YOUR_NEW_BFF_IMAGE_ID \
  --web-nginx-image sha256:YOUR_NEW_FRONTEND_IMAGE_ID

python3 scripts/depositarai.py rollback-public-entry \
  --entry-config /absolute/private/public.json
```

更新僅允許網站映像與 revision 改變。OIDC、配額、Redis、secret 或私有 backend
設定若有變更，先安排獨立維護；不要把它混成網站更新。以相同版本執行
`update-public-entry` 只核對設定、私有 backend 與 HTTP，不重建網站服務。
首次部署後重跑 `up-public-entry` 仍可能暫停入口並重新執行同一組 Compose，保留 Redis；
不要將它當成無變更的狀態查詢。回退選擇先前保存的整組映像，不允許只換回其中一個。

更新失敗回報 `deployment_failed`、退出碼 1，工具會盡力停止入口，**不自動回退**。
先查看 Compose 狀態與已保存的 `pending.json`，確認私有 backend 和磁碟正常，再執行
上面的人工 rollback。若工具無法成功送出停止命令，回報的
`entrance_stop_requested` 為 false，需由維護者立即確認對外入口已停止。

程序被強制終止後可能留下部分已更新的容器；Compose 更新本來就不是跨容器交易。
保留 `pending.json` 與所有版本資料，人工 rollback 會選擇這次更新前的版本。
不提供自動 recovery 或自動接管未知容器。首次啟動沒有前版可回退時，先修正故障，再以相同設定與兩個 image ID
重新執行 `up-public-entry`。它只接受已封存的同一個候選版本，Redis 使用
`--no-recreate` 保留原容器與資料；不接受趁重試更換映像或 secret。不要刪除
Redis 資料來重試。

## 私有版本與狀態

`state_root/public-entry/` 中的設定與版本資料都是私有檔案，不上傳 Git：

| 路徑 | 用途 |
|---|---|
| `compose.json`、`prepared.json`、入口配置 | 初始固定拓樸與配置的核對基準 |
| `versions/<generation>/compose.json`、`snapshot.json` | 一版網站的 Git revision、兩個 immutable image ID、Compose 與設定 digest |
| `active.json` | 最近成功完成 HTTP 檢查的版本及其前版 |
| `pending.json` | 尚未完成的候選版本及更新前版本；供人工回退選版 |
| `redis-data/` | 配額持久化資料；更新與回退保留原目錄 |

部署命令使用同一把檔案鎖，防止兩個維護程序同時更新。版本資料可核對設定及來源，
不能取代線上狀態觀察；磁碟或設定損壞時先停止入口與調查，不修改 digest 假裝成功。
保留可回退的映像與版本檔，不要只刪其中一項。

## 驗收與暫停

loopback HTTP 檢查不代替正式 HTTPS/OIDC 驗收。開放流量前，從真正的外網入口檢查：

- 首頁、JavaScript、CSS 與圖片能載入，瀏覽器沒有 CSP 或 MIME 錯誤。
- 未登入不能使用 AI；登入、callback、登出與重新登入正常。
- 搜尋、追問、重設與帳號間 session 隔離正常。
- 額度用完、Redis 不可用、私有 backend 異常時，不會放行無限 AI 請求。
- 只公開核准的 HTTPS 入口，沒有旁路可直接存取私有網站或管理介面。

需要人工暫停時，使用目前版本的 Compose：

```bash
docker compose -f /absolute/private/public-entry/versions/GENERATION/compose.json \
  stop public-entry
```

停止入口不刪除 Redis 或版本資料。恢復之前先處理失敗原因；有 pending 版本時，
優先執行人工 rollback。不要使用 `down -v`、清空 Redis 或重建空的資料目錄。
