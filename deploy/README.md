# 部署資產

完整產品使用 [部署指南](../docs/guide/operations.md) 的 `scripts/depositarai.py`。
維護者提供一份 `product.json`，工具依 catalog、inference、release 和 gateway 的責任
產生 Compose；不用自行挑選或拼接 YAML。

網站放到另一台 CPU 主機時，使用 [拆機前端指南](../docs/SEARCH_WEBSITE.md) 的
`scripts/search-web.sh`。它只啟動 BFF 與 Nginx，透過核准的私有 HTTPS 連線呼叫後端。

## 映像的責任

| Dockerfile 目錄 | 用途 |
|---|---|
| `api/` | 對話搜尋 API；唯一 API recipe，`production` 是預設 target，`tests` 供 CI 使用 |
| `web/` | Django BFF，提供網站 API 與暫存搜尋 session |
| `web-nginx/` | 建置及提供 React 靜態檔，代理網站 API 與發布 gateway |
| `reranker/` | GPU 重排服務 |
| `fess/`、`opensearch/` | Catalog 文字與向量索引服務 |
| `importer/` | 一次性的 catalog 匯入工作 |

這些是同一產品的服務分工。API、網站、資料庫與模型分開配置資源及憑證；
搜尋只使用 Conversation API；條件檢索是它的內部模組。

## 檔案放置

- `docker/`：上述七種映像及各自的 build-context allowlist。
- `compose/compose.search-canary.yaml`：產品 catalog 準備流程仍使用的索引配置。
- `compose/compose.search-web.yaml`：拆機 CPU 前端。
- `env/`：產品、公眾入口、catalog 與拆機前端的非私密範本。
- `config/`：Nginx、Fess、OpenSearch 的設定。

產品 runtime 由工具產生，不另維護單輪 API 或 local／filtered 網站組合。
真實憑證、模型、索引與 runtime 資料保存在原始碼目錄之外。
