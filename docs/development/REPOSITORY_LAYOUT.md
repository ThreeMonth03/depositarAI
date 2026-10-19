# 程式目錄與新增檔案規範

Repository 管理一套現行搜尋產品。`main` 是開發與交付基線；短期分支用於修改與
review，合併後不留長期分歧版本。舊程式與操作紀錄透過 Git 查閱，不另設封存程式區。

## 目錄用途

```text
depositar-platform/
├── README.md                  產品範圍與閱讀入口
├── STYLE.md                   程式及文件寫法
├── Makefile                   產品、前端及檢查入口
├── deploy/
│   ├── docker/                各服務 Dockerfile 與專屬 ignore
│   ├── compose/               Catalog 配方與拆機 CPU 前端
│   ├── config/                Nginx、Fess、OpenSearch 非私密設定
│   └── env/                   產品、入口及前端設定範本
├── web/
│   ├── src/                   React 畫面、呈現元件及 HTTP client
│   │   └── search/            瀏覽器搜尋 session 與存取狀態
│   └── tests/                 Browser 行為與 HTTP 互動測試
├── search/
│   ├── http/                  Django／DRF 共用設定、ASGI 與 HTTP 支援
│   ├── api/
│   │   ├── conversation/      唯一搜尋 HTTP app、對話與條件狀態
│   │   ├── retrieval/         內部條件檢索、篩選、排序與型別
│   │   └── shared/            strict model、admission、設定與 worker IO
│   ├── web/                   CPU BFF、session 與私有 API client
│   ├── catalog/               封存資料、embedding 配方、Fess admin、匯入與向量工作
│   │   └── build/             離線 metadata 正規化、封存及 builder CLI
│   ├── planning/              條件、日期、clock 與模型規劃
│   ├── scope.py               搜尋用途分類
│   ├── model_policy.py        固定模型身分與 request 限額
│   ├── transport/             有界 HTTP transport
│   ├── serialization.py       純 strict JSON parsing
│   ├── secrets.py             私有 service-key 檔案驗證與讀取
│   ├── reranker/              獨立重排服務
│   ├── operations/
│   │   ├── cli.py             產品命令參數、dispatch 與安全診斷
│   │   ├── catalog/           索引、匯入、投影、向量與 leases
│   │   ├── deployment/        映像、模型、release、gateway、入口與打包
│   │   │   ├── gateway/       路由準備、啟用及 probes
│   │   │   ├── release/       CPU release 協調、runtime 與 probe
│   │   │   └── public_entry/  OIDC、配額與入口生命週期
│   │   ├── metadata/          公開來源、同步、快取與收據
│   │   └── shared/            私有 IO、有界 process 與 source identity
│   └── tests/                 搜尋、API、BFF 及 planning 測試
├── scripts/                   薄產品 CLI、模型準備及設定檢查工具
│   └── ci/                    可獨立執行的 CI helpers
├── tests/
│   ├── unit/                  operations、catalog、deployment 政策與失敗案例
│   └── integration/           隔離 Compose／真實 OSS 案例
├── .github/workflows/         CI、文件建置與原始碼打包
└── docs/
    ├── guide/                 使用、部署、架構與開發指南
    ├── development/           CLI、維運及程式邊界參考
    └── REFERENCE.md           技術參考導覽
```

各 domain 的短導覽見 [search](../../search/README.md)、[API](../../search/api/README.md)、
[catalog](../../search/catalog/README.md)及 [operations](../../search/operations/README.md)。部署入口只有產品 CLI 與拆機前端；
兩者用途見 [deploy](../../deploy/README.md)，不另維護單輪／planned API 部署組合。

## 新檔案放哪裡

| 內容 | 位置與要求 |
|---|---|
| 畫面與呈現元件 | `web/src/`；browser tests 放 `web/tests/` |
| 瀏覽器搜尋 session、訊息與存取狀態 | `web/src/search/session.ts`；不依賴呈現套件 |
| 瀏覽器 HTTP 型別、傳輸與錯誤分類 | `web/src/api.ts` |
| BFF session、caller identity 與私有 upstream client | `search/web/` |
| Django／DRF 設定、HTTP 生命週期與共用 view 支援 | `search/http/`；不匯入搜尋或維運 domain |
| 對話與 HTTP 契約 | `search/api/conversation/` |
| OpenSearch 條件檢索、Haystack RRF 與重排 | `search/api/retrieval/`；純條件／時間規則放 `search/planning/` |
| Embedding 配方、Fess data／admin 與向量工作 | `search/catalog/`；與操作主機的 catalog orchestration 分開 |
| 離線 metadata 正規化與 bundle 建置 | `search/catalog/build/`；下載、TTL cache 與 diff 放 `search/operations/metadata/` |
| API 共用邊界 | `search/api/shared/`；不讓 BFF／readiness 反向依賴 engine |
| 產品 CLI 參數、dispatch 與退出碼 | `search/operations/cli.py`；`scripts/depositarai.py` 為薄執行入口 |
| 資料準備及部署政策 | 所屬 operations domain |
| 產品映像及 ignore | `deploy/docker/<service>/`，明確指定 repository-root build context |
| 產品非私密設定 | `deploy/config/`、`deploy/env/`；runtime Compose 由工具產生 |
| 文件 | 操作／使用指南放 `docs/guide/`，詳細參考由 `docs/REFERENCE.md` 導覽 |
| 原始量測及操作收據 | Git 外的私有儲存；保留來源版本、資料及比較條件 |

目錄表達 domain，檔名表達責任，不重複父目錄前綴。測試目錄對應來源，保留
`test_`；`Dockerfile`、`__init__.py` 及 lock 等工具規定名稱也保留。
平面 CLI 檔名要能辨認操作對象。完整規則見 [STYLE.md](../../STYLE.md)。

## Import 與封裝

使用現行模組的明確 import，搬移時同步更新呼叫端並刪除舊路徑，不新增轉接包或
`sys.path` 猜測。Catalog 與 operations initializer 只放文件；配置直接匯入
`search.operations.config`。其他既有小型靜態介面也不載入 app、讀 key、
啟動 Docker 或初始化模型。

App、operator 與 tests 的封裝邊界見 [共用模組](SOURCE_REFACTOR.md)。
搬移 runtime 來源時，一起檢查 source inputs、Docker COPY／ignore、CLI、CI 及
image 內 import。Catalog 資料配方另做[相容性核對](CATALOG_CACHE.md)；
保持原始資料 provenance，不以刪除校驗或改寫收據來避免重建。

## 分支、交付與實驗

- `main` 維護現行版本；功能與修正使用短期分支，完成 review 及必要 CI 後合併。
- Runtime 以固定 commit／image identity 部署；Git push 不直接等於服務已切換。
- 真實模型實驗先固定問題、metadata 與主要假設，再比較解析、召回、排序、延遲
  及資源成本。Fixture 成功不代表模型品質；失敗與澄清也要納入分母。
- 可公開的檢查結果留在 PR／CI；原始量測與操作收據放在 Git 外的私有儲存。
  操作文件描述目前怎麼做，不堆疊逐輪驗收紀錄。

CI 入口與選取見 [CI performance](CI_PERFORMANCE.md)。原始碼交接使用
[source package](SOURCE_PACKAGE.md)；模型／metadata／映像素材由
[產品包](PRODUCT_RESTORE.md)管理，完整產品更新及回退的範圍見
[產品 CLI](PRODUCT_LIFECYCLE.md)。

## Git 外的資料

模型、索引、metadata 快照、私密 env、key、runtime journal、備份及原始執行結果
放在專用外部儲存，不加入 Git。設定範本可以提交；`.gitignore` 不能取代內容審查。
來源整理不隨意搬動 live data、mount 或 runtime 目錄。
