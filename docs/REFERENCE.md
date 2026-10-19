# 技術參考

一般使用、部署與開發從[指南](guide/index.md)開始。以下內容供開發與維護人員
深入調整元件；每項操作與契約只有一個主要說明位置。

## 部署與資料管理

| 主題 | 文件 |
|---|---|
| 產品設定與 CLI | [產品生命週期](development/PRODUCT_LIFECYCLE.md) |
| Docker 與拆機前端 | [部署目錄](../deploy/README.md)、[網站設定](SEARCH_WEBSITE.md) |
| 前端部署檢查與私有 API TLS | [唯讀檢查](development/FRONTEND_CHECK.md)、[TLS 接口](development/PRIVATE_API_TLS.md) |
| 模型與檢索服務 | [私有 runtime](development/PRIVATE_RUNTIME.md) |
| Catalog 重用與 metadata 比較 | [Catalog cache](development/CATALOG_CACHE.md)、[Sync 與候選索引](development/PRODUCT_LIFECYCLE.md#metadata-與候選索引) |
| 向量續跑與失敗恢復 | [Vector passes](development/VECTOR_PASSES.md)、[Token 清理](CATALOG_CLEANUP_RECOVERY.md) |
| 產品包及來源交接 | [Product restore](development/PRODUCT_RESTORE.md)、[Source package](development/SOURCE_PACKAGE.md) |
| 模型與索引保存 | [儲存及復原](MODEL_STORAGE_AND_RECOVERY.md) |
| 登入與公眾入口 | [Public access](development/PUBLIC_ACCESS.md) |

## API 與程式維護

| 主題 | 文件 |
|---|---|
| 私有搜尋 HTTP 契約 | [Conversation API](API.md) |
| Browser session 與 BFF | [Search website](SEARCH_WEBSITE.md) |
| 程式責任與依賴方向 | [Search](../search/README.md)、[API](../search/api/README.md)、[Catalog](../search/catalog/README.md)、[Operations](../search/operations/README.md) |
| 共用模組與封裝 | [模組邊界](development/SOURCE_REFACTOR.md)、[目錄規範](development/REPOSITORY_LAYOUT.md) |
| 程式及文件寫法 | [STYLE.md](../STYLE.md) |
| CI 操作與耗時排查 | [CI performance](development/CI_PERFORMANCE.md) |

## 產品行為與設計

| 主題 | 文件 |
|---|---|
| 介面與視覺 | [互動設計](SEARCH_WEBSITE_UX.md)、[品牌](SEARCH_WEBSITE_BRANDING.md) |
| 日期與曆法 | [紀年規則](SEARCH_WEBSITE_CALENDAR.md)、[相對時間](SEARCH_RELATIVE_TIME.md)、[農曆文字搜尋](SEARCH_LUNAR_TEXT_SEARCH.md) |

現行取捨與限制寫在對應契約或架構頁。舊實作、實驗與量測歷程透過 Git／PR
查閱，不另外保留一套部署或實驗入口。
