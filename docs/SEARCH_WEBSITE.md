# 網站服務設定與 HTTP 契約

本頁供維護 React 網站、Nginx 與 CPU 網站後端（BFF）的人查閱。使用者操作見
[搜尋指南](guide/using-search.md)；元件與資料流見[架構概覽](guide/architecture.md)，
catalog、模型及服務平面的分工見[私有 runtime](development/PRIVATE_RUNTIME.md)。
網站後端使用 Django／DRF 與 ADRF，呼叫私有 Conversation API；瀏覽器不持有
服務 key，也不直接連線模型。React 靜態檔由 Nginx 提供，BFF 只提供健康檢查與
`/api/` 端點。BFF 只有 POST 接受 body；其他方法的 body 必須為空。

## 選擇執行入口

| 用途 | 入口與前提 |
|---|---|
| 完整產品私有試用 | 主機、GPU、模型及產品設定備妥後，依[部署指南](guide/operations.md)執行 `up-private`，串接 catalog 準備與首次啟用；入口是 `127.0.0.1:18083`。 |
| 獨立 CPU 前端 | `bash scripts/search-web.sh up` 只建立網站與 Nginx，連到操作者核准的 HTTPS Conversation API；入口是 `127.0.0.1:18081`。 |

分段命令保留作診斷及明確的分段操作，前置條件見
[產品 CLI 參考](development/PRODUCT_LIFECYCLE.md)。
公開登入與額度管理由選用的[公眾入口](development/PUBLIC_ACCESS.md)負責。
獨立前端的 loopback 綁定不提供公眾登入；下列命令不表示任何主機已部署。

## 部署獨立 CPU 前端

前端主機需要 Docker／Compose、已提交且經 review 的來源，以及可存取的私有
HTTPS Conversation API。後端必須提供 `/v1/turn`、接受對應服務 key，TLS 憑證
須受前端容器信任。VPN／私網路由與 ACL 由主機或網路管理者核准；本工具不建立
VPN、不修改防火牆，也不提供外層 TLS。

複製[前端設定範本](../deploy/env/.env.search-web.example)到 Git 外的私有檔案，
再填入以下必要欄位，私有 CA 則依需要設定。預設設定檔是 checkout 根目錄的 `.env.search-web`；也可由
`SEARCH_WEB_ENV_FILE` 指定。

| 欄位 | 填寫方式 |
|---|---|
| `WEB_UID`、`WEB_GID` | 執行前端的非 root 數字 UID／GID；此使用者須可讀取服務 key。 |
| `WEB_API_KEY_FILE` | 已存在的私有服務 key 檔之絕對路徑；將 key 檔單獨唯讀掛入 BFF。 |
| `WEB_ORIGIN` | 瀏覽器實際使用的完整 origin；SSH tunnel 使用 `http://127.0.0.1:18081`。 |
| `WEB_UPSTREAM` | 操作者核准的 HTTPS API origin，例如 `https://private-api.example.org`；不可加路徑、帳密、query 或 fragment。 |
| `WEB_UPSTREAM_CA_FILE` | 選填；私有簽發者的 PEM CA certificate／bundle 絕對路徑。省略或留空時沿用公開 CA；只提供公開憑證，不放 TLS private key。 |

不需要 API env、catalog bundle、模型路徑或 GPU 設定。前端服務不分配 GPU，
也不啟動 Conversation API、Fess 或 OpenSearch。

```sh
cd /absolute/path/to/depositarAI
cp deploy/env/.env.search-web.example /absolute/path/private/search-web.env
chmod 600 /absolute/path/private/search-web.env
# 編輯上述五個欄位，並備妥可讀取的私有服務 key。
export SEARCH_WEB_ENV_FILE=/absolute/path/private/search-web.env
bash scripts/search-web.sh config
bash scripts/search-web.sh up
bash scripts/search-web.sh status
```

設定檔擁有上述欄位的值，shell 中同名環境變數不覆寫它們。
[啟動腳本](../scripts/search-web.sh)先 render Compose 並拒絕非 HTTPS 上游、
額外後端或不安全掛載，再以 `git archive HEAD` 建立兩個版本化前端映像。
Nginx 映像包含 React build，Django BFF 映像只包含網站 API；兩者使用同一個 Git
revision，並由同一份 Compose 啟動。更新或回退時一起選用同一版本的兩個映像，
期間可短暫中斷；BFF 重啟會清除 RAM 中的搜尋 session，使用者需開始新搜尋。
只有 Nginx 發布 `127.0.0.1:18081`，BFF 不發布 port。上游 client 驗證 TLS、
不跟隨 redirect，也不採用環境中的代理設定。

網站更新仍使用 `up`：先建置同一 Git 版本的兩個映像，再暫停入口，交給 Compose
重建 BFF 與 Nginx，等待 API 和首頁健康檢查。更新期間會短暫無法存取。
需要回退時，選回上一個已驗證的 Git commit，以原設定重新執行 `up`；保留舊映像，
不要在故障處理前清除它們。此操作不更新私有搜尋 API、模型或索引。

私有 API 由產品部署流程管理。API 契約改動時，先暫停外部入口，再協調更新 API
與採用對應契約的 BFF；完成搜尋驗證後才恢復入口。回退同樣須配對 API 與 BFF，
不能只切回網站映像；BFF 會拒絕不符合契約的上游回應並回報 503。

若私有 HTTPS API 使用內部 CA，在上述設定檔另加：

```dotenv
WEB_UPSTREAM_CA_FILE=/absolute/path/private/search-web/upstream-ca.pem
```

啟動腳本會自動加入私有 CA 的 Compose 設定，將此檔案唯讀掛載為
`/run/certs/upstream-ca.pem`，不把憑證寫入映像，也不建立不存在的 host 路徑。
BFF 以明確的 SSLContext 將私有 CA 加入原本的公開信任庫，仍驗證憑證鏈與上游
hostname；不支援關閉 TLS 驗證。檔案不存在、無法讀取或不是有效 PEM 憑證時，BFF 啟動
失敗。`SSL_CERT_FILE`、`SSL_CERT_DIR` 與 `HTTPS_PROXY` 等環境變數不取代此設定。
CA 檔須可由 `WEB_UID` 讀取；服務 key 另須由相同 UID 擁有，權限為 `0400` 或 `0600`。
更新 CA 內容後須重新啟動 BFF，才會載入新的信任憑證。
若部署帳號使用 UID／GID `1003`，請將 `WEB_UID=1003`、`WEB_GID=1003`，
並確認服務 key 的擁有者也是 `1003`。容器 `/healthz` 成功只代表網站程序可用，仍須
另做一次實際搜尋確認私有 API 的 TLS、key 與模型連線。

後端已有受控的私有 release 時，可依[獨立 TLS 接口](development/PRIVATE_API_TLS.md)
準備提供給 VM 的 HTTPS API；其來源白名單、憑證及 backend 綁定由後端操作帳號管理。
前端設定中的 CA 只含公憑證；CA signing key 與兩張伺服器 private key 留在後端。

在自己的電腦建立 tunnel，將 `user@server` 換成前端主機：

需要多段跳板、且 VM 私鑰留在伺服器上時，可使用[私有 demo 連線工具](development/PRIVATE_DEMO.md)
統一建立通道並驗證實際部署版本；它不重新部署或送出模型查詢。

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18081:127.0.0.1:18081 user@server
```

需要跳板時加上 `-J user@jump-host`。瀏覽器開啟 `http://127.0.0.1:18081`，
使用這個完整 origin；`http://localhost:18081` 不符合上述同源設定。
loopback 仍可由伺服器本機使用者存取，SSH tunnel 不是網站帳號登入。
若使用外層 HTTPS 入口，將 `WEB_ORIGIN` 改為該入口的 origin，並另行管理反向代理。

停止前端時，使用相同設定檔執行：

```sh
bash scripts/search-web.sh down
```

此命令移除前端 project 的容器與自有網路；後端 API 與資料由其原生命週期管理。
這個入口只部署 CPU 前端，沒有 `local`、`filtered` 或 `remote` 模式參數。

日常確認部署時，使用[前端唯讀檢查](development/FRONTEND_CHECK.md)核對已記錄的
前端映像、版本、網站回應及容器內到 API 的 TLS／版本連線。檢查設定中的預期值
取自已驗證的交付收據，工具版本可以與現役前端不同。這項檢查不執行 AI 搜尋，
也不更新或切換服務；完整搜尋操作仍由下方實機驗收另行確認。

## 私有 VM 的實機驗收

維護者可透過上述 SSH tunnel 執行[獨立 browser 驗收](../web/acceptance/run-private-runtime.mjs)。
它固定只送一輪「台灣空氣品質資料」搜尋，再清除 session；沒有模型查詢重試。
此流程不加入一般 fixture CI。先在 `web/` 安裝 lockfile 指定的開發套件，準備 Chromium，
再由已核對的部署紀錄填入前端 revision、後端 revision 與 catalog manifest SHA256：

```sh
cd /absolute/path/to/depositarAI/web
export DEPOSITAR_ACCEPT_PRIVATE_RUNTIME=1
export DEPOSITAR_EXPECT_WEB_REVISION=EXPECTED_FRONTEND_COMMIT
export DEPOSITAR_EXPECT_API_REVISION=EXPECTED_API_COMMIT
export DEPOSITAR_EXPECT_CATALOG_SHA256=EXPECTED_CATALOG_MANIFEST_SHA256
acceptance_parent=$(mktemp -d /absolute/private/browser-XXXXXXXX)
export DEPOSITAR_PRIVATE_RUNTIME_OUTPUT="$acceptance_parent/result"
node acceptance/run-private-runtime.mjs
```

輸出父目錄須由執行帳號擁有且為 0700，`result` 不可已存在。使用 launcher 可在
Playwright 清理目錄之前拒絕舊輸出，保留原有證據。驗收核對實際版本、非空結果卡片、
桌面與手機版、清除 session 及瀏覽器錯誤；私有輸出含截圖與不含 token／header 的 proof。
前端來源還須搭配映像及 committed-source 建置紀錄核對，不能只憑 session 回報的 revision。

## 網站後端設定

設定由 [WebSettings](../search/web/upstream.py)驗證；正常產品 release 由生命週期
建立設定；獨立前端則由 Compose 傳入。

| 設定 | 契約 |
|---|---|
| `WEB_ORIGIN` | 完整且精確的 origin，不能有帳密、路徑、query 或 fragment。HTTPS 可用於外層入口；HTTP 只接受主機名 `127.0.0.1`。 |
| `WEB_UPSTREAM` | 固定的私有 API origin，不接受路徑、帳密、query 或 fragment。HTTP 唯一例外是同 Docker 網路的 `http://conversation-api:8000`；其他來源必須 HTTPS。 |
| `WEB_UPSTREAM_CA_FILE` | 選填的容器內 CA 檔絕對路徑，僅供 HTTPS 上游使用；獨立前端由啟動腳本固定為 `/run/certs/upstream-ca.pem`。不影響瀏覽器端或 OIDC 的憑證。 |
| `/run/secrets/api_key` | BFF 呼叫 `/v1/turn` 的服務 key，獨立前端由 `WEB_API_KEY_FILE` 唯讀掛入；遵守[私有 key 檔驗證](../search/secrets.py)，不得交給瀏覽器。 |
| `SOURCE_REVISION` | 啟動腳本與映像記錄來源版本；建立 session 時回傳 `revision`，供辨識正在使用的網站版本。 |
| `WEB_EDGE_SECRET_FILE` | 選用公眾入口的身分驗證設定。啟用後核對入口 subject 與 edge key，session 綁定登入身分；由公眾入口流程管理。 |

模型分工由 runtime 管理。對話 planner 的參數主要定義於
[LocalPlanner](../search/planning/model.py)：`num_ctx=16384`、`num_predict=2048`、
`temperature=0`、`seed=11`；[Conversation worker](../search/api/conversation/worker.py)
指定 `think=low` 與模型常駐。調整參數應修改其 owner，不在網站另建一套設定。

## 瀏覽器 HTTP 契約

網站 API 由 [BFF app](../search/web/app.py)提供；私有 `POST /v1/turn` 使用服務 key，
不經網站 Nginx 公開。網站 session 的 Bearer token 與這個 key 是兩種不同憑證。

| 請求 | 輸入與回應 |
|---|---|
| `GET /api/access` | 回報 `auth_required`、`authenticated`，供介面決定是否引導登入。 |
| `POST /api/session` | body 必須是 `{}`；回傳臨時 `token`、初始 `version=0`、`expires_in`、`max_turns` 與 `revision`。 |
| `POST /api/turn` | 帶臨時 Bearer token 與下方 JSON；回傳下一個 `version`、`result` 及實際 `context_used`。 |
| `DELETE /api/session` | 帶臨時 Bearer token，清除本次搜尋；不需 JSON body。 |

```json
{"query":"河川水質觀測資料","version":0,"top_k":5,"timezone":"Asia/Taipei"}
```

- `/api/*` 不接受 query string；重複的 Origin、Host、Authorization、Content-Length
  或 Content-Type 會被拒絕。Origin 須精確符合 `WEB_ORIGIN`，唯一例外是唯讀的
  `GET /api/access` 可省略 Origin；有帶錯誤 Origin 仍會拒絕。
- POST 需 `application/json`、正確的 `Content-Length`，不接受 Content-Encoding。
  body 最多 4,096 bytes、讀取上限 5 秒；拒絕重複 JSON key 與 NaN／Infinity 常數。
- `query` 是 1–512 字元的單一搜尋問題，另經查詢字元驗證；`top_k` 是整數 1–20，
  預設 5；`version` 是整數 0–20；`timezone` 最多 64 字元，預設 `Asia/Taipei`，須通過 IANA 時區驗證。
  未知欄位一律拒絕，不能由請求選擇模型、工具、上游網址或生成參數。
- BFF 加入當下 UTC 參考時間；回應 `context_used` 含實際參考日期與時區。
  型別見[前端 API 型別](../web/src/api.ts)及[私有對話契約](API.md)。

瀏覽器會在採用成功回應前驗證 session、版本及畫面實際使用的結果欄位。
HTTP 200 與 TypeScript 型別宣告不能取代這項執行期檢查；格式錯誤的回應不提交
session 或版本，也不渲染損壞的結果。新增而未使用的回應欄位可保留，避免把
後端的相容擴充當作錯誤。這是前端的消費邊界，完整業務驗證仍由 API 負責。

## 暫存狀態與錯誤處理

[Sessions](../search/web/sessions.py)使用 RAM，不設聊天資料庫。預設為
30 分鐘絕對有效期、不因追問續期，最多 20 輪；每個 BFF process 最多 100 個 session。
啟用入口身分時，每個 owner 最多 2 個 session。狀態只含主題／時間／格式等條件
與最多一筆待澄清輸入。BFF 重啟會清空；前端對話及結果也只放在頁面記憶體，
不寫 localStorage／sessionStorage，重新整理便開始新搜尋。

瀏覽器的存取確認、搜尋訊息、session 版本、到期與重設統一由
[`useSearchSession`](../web/src/search/session.ts)管理；畫面只負責呈現及
assistant-ui 訊息轉換。到期會清除本次搜尋與參考時間；先前請求的延遲成功或
失敗都不再改寫搜尋狀態，也不觸發重送。

每輪帶上前次回傳的 `version`。同 session 的競爭請求或舊版本不能覆寫新狀態；
正常回應（包括拒絕或澄清）會前進版本，拒絕不污染既有條件。
503／上游失敗不提交條件或版本。新搜尋會刪除舊 session，到期狀態另由定期清理回收。

| HTTP／情況 | 客戶端處理 |
|---|---|
| 401 `authentication_required` | 到設定的登入入口完成登入。 |
| `/api/access` 的其他 401 | 無法確認存取狀態；顯示不可用，由使用者重新確認。 |
| 搜尋請求的其他 401、409、410 | session 缺失、狀態衝突或已到期，開始新搜尋。 |
| 429 | 可能是忙碌、session 容量、頻率或額度限制；顯示原因，由使用者稍候再送出。 |
| 400／411／413／415／422 | 修正請求格式、長度、欄位或時區。 |
| 503 | 顯示暫時不可用；本輪條件未提交。 |
| 網路中斷或無法解析回應 | 本輪是否完成不確定，開始新搜尋，避免沿用錯誤版本。 |

[前端錯誤策略](../web/src/api.ts)不自動重送請求。每個 BFF process 同時只處理一輪工作，
上游請求總期限 125 秒、回應最多 256 KiB；Nginx 等待上游 130 秒，
瀏覽器請求期限 135 秒。縮短外層期限時，要考慮本輪完成狀態不確定的處理。

## 呈現、資源與維護

資料集文字以純文字呈現；連結只接受 `https://data.depositar.io/dataset/` 下的網址，
拒絕帳密與非預設 port。網站的 CSP 限定同源 script／connect、禁止嵌入，並設定
`no-store`、`nosniff`、`no-referrer` 與停用地理位置等權限；style 仍允許 inline。
Nginx 只接受靜態檔的 GET／HEAD，提供正確 MIME；缺檔回 404，其他方法回 405，
不將未知 `/api/` 路徑改送首頁，也不公開隱藏檔或靜態根目錄外的檔案。
[Nginx 設定](../deploy/config/web-nginx/website.conf)拒絕 `/v1/` 並限制請求大小；公眾頻率與額度由 APISIX 管理。
access log 只記 request ID、method、status 與耗時，不記 IP、輸入、URL arguments、
Authorization 或 session token。Uvicorn 不開 access log；新增記錄時須保留這個界線。

| 獨立前端服務 | GPU | CPU 上限 | RAM 上限 |
|---|---:|---:|---:|
| Django BFF | 0 | 1 核 | 512 MiB |
| 網站 Nginx 與 React 靜態檔 | 0 | 0.5 核 | 64 MiB |

這些是前端 Compose 容器上限；模型與檢索資源由後端 runtime 管理。網站不提供
任意 tool／SQL、PDF／CSV 內文問答或空間距離篩選。時間與格式條件在 OpenSearch
檢索候選前套用，回報 `filter_scope=index_prefilter`；沒有條件時為 `none`。
未知 metadata 可能保留並標記；不能把空結果解讀為整個資料庫
沒有符合資料。條件及候選範圍見[私有 API 契約](API.md)。

修改網站後使用 `make search-web-ci` 檢查 HTTP 契約、後端、格式、建置與真實
Nginx／BFF／API browser 流程；預設執行全部網站套件。模型品質需另以實際模型評估，
fixture 的成功不能代替準確度。使用範圍與日期細節由[搜尋指南](guide/using-search.md)
說明，網站體驗提案另見[UX 設計](SEARCH_WEBSITE_UX.md)。
