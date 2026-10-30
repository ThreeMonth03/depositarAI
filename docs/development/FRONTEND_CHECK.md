# 現役獨立前端的唯讀檢查

在已有 Docker 權限的前端 VM 上執行：

```bash
python3 scripts/check_search_web.py --config /absolute/private/frontend/check.json
```

將[設定範例](../../deploy/env/frontend-check.example.json)放在 Git 外、operator 所有的
0700 目錄，設定檔與 frontend env file 使用 0400／0600。版本與 digest 都須取自已驗證的
部署收據，不能從待檢查服務的回應建立預期值。checker 的 checkout 必須固定在已檢閱的
revision，可以與現役 frontend revision 不同。

## 設定與配對版本

目前只接受 `schema_version: 3`，必填欄位如下：

| 欄位 | 用途 |
| --- | --- |
| `deployment_mode` | `intranet-http` 或 `private`，明確選擇同一套 checker 的部署模式。 |
| `frontend_env_file` | 現役 `search-web.sh` 使用的私有 env file 絕對路徑；交給 Compose 解析，不執行 shell。 |
| `frontend_revision` | 已建置的 frontend／BFF 完整 source commit。 |
| `web_platform_manifest` | BFF 的 immutable linux/amd64 platform manifest digest。 |
| `web_nginx_platform_manifest` | Nginx 的 immutable linux/amd64 platform manifest digest。 |
| `conversation_contract_version` | 必須與此 checkout 的 shared contract constant 相同，目前為整數 `4`。 |
| `api_revision` | 配對的現役 API 完整 source commit。 |
| `catalog_manifest_sha256` | 該 API 已驗證的 catalog manifest。 |

工具不提供舊 wire 協議轉換，也不接受 schema 1／2。需要回退舊部署時，使用已封存的
舊 operator source、profile 與配套映像，不能只改新 profile 的預期版本來消除錯誤。

## 同一套部署政策

checker 與 `validate_search_web.py` 共用
[`frontend/policy.py`](../../search/operations/deployment/frontend/policy.py)，核對完整
Compose 的 origin、project、位址／port、mount、資源與網路邊界。

- `intranet-http` 使用 `depositar-search-intranet`、指定 RFC1918 IPv4 與專用 port。
  檢查 host ACL 與容器實際讀取的 ACL 都符合明確設定的 CIDR；不寫入或重建規則。
- `private` 使用 `depositar-search-web`，固定只綁 `127.0.0.1:18081`。

兩種模式都只允許 `web`、`web-nginx` 兩個容器。工具核對容器完整 ID、非 root 唯讀
runtime、完整環境、唯讀 mount、資源限制、network 成員與對外綁定。
首頁與 JS／CSS 連到 Compose 核准的綁定位址，Host 使用實際 browser origin。
內網模式下，VM 本身發起檢查時使用的直接來源也必須已獲 CIDR 規則允許；工具不會
自動增加白名單，也不使用 `X-Forwarded-For` 繞過限制。

## 平台映像身分

Docker 必須支援 `image inspect --platform linux/amd64`，且 container inspect 提供
`ImageManifestDescriptor`。Compose 中的 source-revision tag 只用於定位映像，須同時滿足：

1. 容器 `ImageManifestDescriptor.digest` 等於 profile 中指定的 platform manifest。
2. `image inspect --platform linux/amd64` 的 `Descriptor.digest` 與 `.Id` 也等於該 digest。
3. descriptor 的平台為 linux/amd64，且 source revision、BFF contract 或 Nginx role label 正確。

OCI index 可能包含建置 attestations，與平台 manifest 是不同的身分。成功收據會分別
記錄 `container_image_index` 與 `platform_manifest`。不要求舊 index 一定仍可獨立 inspect，
也不以 tag、image config digest 或容器可覆寫的 label 代替上述平台證據。

## API 路徑與收據

工具在已核對的 BFF 內使用既有 key 與 public CA／選填 CA，驗證 `WEB_UPSTREAM` 的
TLS hostname。匿名 `GET /v1/version` 必須回 401；服務 key 驗證後必須回 200，且 API
revision、catalog manifest 與整數 contract4 全部吻合。API key 只在 BFF 內讀取。
最後重查 runtime、映像與 ACL，變動時拒絕回報成功。

不接受重新導向、不採環境 proxy 或 `SSL_CERT_*` 覆寫，HTTP 回應與執行時間均有限制。
不輸出 key、header、body 或原始 daemon 錯誤。成功 JSON 的 `status` 為 `frontend_checked`，
並包含 mode、project、origin、版本與受檢 runtime。失敗只回固定錯誤 JSON。

本命令只做 GET 與固定的容器內唯讀檢查，不送搜尋 query、不建立 session、不呼叫
`/readyz`、不操作模型、不 pull／build 或啟停服務。`public_readiness_verified`、
`model_inference_verified`、`deployment_performed` 固定為 false。通過不代表實驗室外部
路由、GPU 推論或搜尋品質已驗收；完整 browser 驗收仍須另行完成。

CPU fixture 與安全回歸：

```bash
python3 -m unittest tests.unit.deployment.frontend.test_check -v
```

定時執行同一 checker，見[前端健康檢查排程](FRONTEND_MONITORING.md)。現役部署只使用
一份 canonical profile 與一個排程；舊 profile 和收據留在回退資料中。
