# depositar AI

用自然語言找尋 depositar 資料集，並在對話中逐步調整主題、時間與檔案格式。

搜尋結果以資料集卡片呈現，包含說明、資源格式與原始頁面連結。你可以接著說
「改查 2020 到 2024 年」或「只要 CSV」，沿用目前條件繼續搜尋。

目前搜尋範圍是資料集的 metadata。PDF／CSV 檔案內文搜尋與永久對話記憶尚未提供。

以 [depositar](https://data.depositar.io) 公開 metadata 為資料來源；本專案為獨立的搜尋介面，
不代表 depositar 官方產品或官方認可。資料使用條件請以原始資料集頁面為準。

模型權重不包含於此 repository；部署前須另行取得固定版本，並遵守各模型的
[上游授權與使用條款](docs/MODEL_STORAGE_AND_RECOVERY.md#模型取得)。

## 從這裡開始

| 你想做什麼 | 閱讀入口 |
|---|---|
| 了解產品與使用方式 | [產品手冊](docs/guide/index.md)、[搜尋指南](docs/guide/using-search.md) |
| 在自己的主機執行 | [部署與維運](docs/guide/operations.md) |
| 了解各元件如何合作 | [系統架構](docs/guide/architecture.md) |
| 修改程式或文件 | [參與開發](docs/guide/development.md) |

網站與搜尋後端可分開部署；後端經由受限的私有連線提供 API。試用網址由維運者
提供。自行部署使用單一[產品設定範本](deploy/env/product.example.json)，
OpenSearch 索引使用 Docker local volume；主機條件與啟動步驟請從部署指南開始。

## 本機預覽文件

在專案根目錄執行：

```sh
python3 -m venv .venv-docs
.venv-docs/bin/python -m pip install --require-hashes -r docs/requirements.lock
.venv-docs/bin/python -m mkdocs serve --dev-addr 127.0.0.1:8000
```

開啟 <http://127.0.0.1:8000>。文件站的來源放在 `docs/guide/`；
也可以直接在 GitHub 閱讀 [文件入口](docs/README.md)。

## 原始碼

`web/` 是 React／TypeScript 前端，`search/api/` 與 `search/web/` 使用
Django／Django REST Framework 提供搜尋 API 和網站服務，
`search/operations/` 負責資料準備與部署工具，[deploy/](deploy/README.md) 放置部署資產。

詳細責任分工見 [程式目錄導覽](search/README.md)，程式與文件寫法見
[維護規範](STYLE.md)。需要深入查看 API、維運設定或研究設計時，
再查 [技術參考](docs/REFERENCE.md)。
