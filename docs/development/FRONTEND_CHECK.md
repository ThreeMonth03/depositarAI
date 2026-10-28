# 現役獨立前端的唯讀檢查

在已有 Docker 權限的前端 VM 上執行：

```bash
python3 scripts/check_search_web.py --config /absolute/private/frontend/check.json
```

將 [設定範例](../../deploy/env/frontend-check.example.json) 放在 Git 外、operator 所有的
0700 目錄，設定檔與既有 frontend env file 使用 0400／0600。範例版本與 image IDs
皆是 placeholder，必須填入先前驗證的部署 receipt；不要從待檢查回應反過來接受身份。

`frontend_env_file` 指向 `search-web.sh` 已使用的六個環境設定，工具沿用 Docker Compose
解析它，不執行 shell。`frontend_revision` 是現役前端的完整 commit；`web_image` 與
`web_nginx_image` 是兩個已驗證的 immutable `sha256:` image IDs，對照實際 container
`.Image` 與 image `.Id`，不是另取 config digest。`api_revision` 與
`catalog_manifest_sha256` 則來自已驗證的私有 backend／catalog receipt。

新版配對部署使用範例中的 `schema_version: 2`，另以
`conversation_contract_version` 明確指定預定受檢的 wire 契約版本，範例使用第 `4` 版。
此值只接受 1–32 的整數，拒絕布林值、字串與缺少欄位。工具會同時核對實際
immutable BFF image 的 `io.depositar.ai.conversation.version` label，以及通過服務 key
驗證的 API `/v1/version` 回應；API 欄位也必須是完全相同的整數。
只核對 revision、manifest 或 `/healthz` 不能代替這項配對檢查。

schema 1 保留原本的欄位、legacy profile 檢查與成功收據格式，不推斷 wire 版本，
也不宣稱已驗證 API／BFF 契約相容。舊的固定部署與監控設定可以繼續使用原檔；
升級時應依配對部署證據另建 schema 2 設定，不改寫舊收據來接受新版。

工具的 checkout 可以比現役前端更新。Compose 插值明確使用指定的
`frontend_revision`，不要求重建或搬動原部署目錄，也不把舊容器的 Compose 檔案路徑
要求為工具 checkout 的位置。既有 env file、API key 與 CA mount 路徑須與現役配置相符。

檢查固定的 `depositar-search-web` project 只有 `web`、`web-nginx` 兩個容器，核對完整
container ID、指定 image ID／revision、非 root 唯讀 runtime、環境、mount、私有網路、
資源限制，以及只有 `127.0.0.1:18081` 對外綁定。讀取實際 loopback 首頁與其 JS／CSS
資源，再在現役 BFF 內使用 Python stdlib 與既有 `search.secrets.read_secret` 驗證
`WEB_UPSTREAM` 的 CA／hostname：匿名 `GET /v1/version` 須回 401，服務 key 驗證後
同一路徑須回 200，且 API revision／catalog manifest 必須吻合。最後重查 runtime。
schema 2 也會在前後兩次 runtime 檢查核對 BFF 的契約 label；成功收據使用
`schema_version: 2` 並包含已驗證的 `conversation_contract_version`。

不接受重新導向、不採 proxy 或 `SSL_CERT_*` 環境覆寫，HTTP 回應與執行時間均有限制。
API key 只在 BFF 內讀取，沒有 host key read、secret CLI argument、header／body 輸出。
失敗只回固定錯誤 JSON；成功 receipt 只含受檢版本、image／container IDs 與檢查結果。

這個命令不送 `/v1/turn`、不呼叫 `/readyz`、不建立／停止服務、不 pull／build／升級，
也不修改 env 或部署設定。`public_readiness_verified`、`model_inference_verified` 與
`deployment_performed` 固定為 `false`。通過表示這次私有前端與 TLS version 路徑符合
預期身份，不代表公眾入口、OIDC、搜尋品質或模型推論已驗收。

本機 CPU fixture 與安全回歸，無需啟動 Docker 或存取真實 API：

```bash
python3 -m unittest tests.unit.deployment.test_frontend_check -v
```

定時執行同一檢查的 systemd user timer 範例，見[前端健康檢查排程](FRONTEND_MONITORING.md)。
