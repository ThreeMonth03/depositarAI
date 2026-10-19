# 開發與文件

從 repository 根目錄執行以下命令。修改前先確認責任所在，完成小範圍檢查後，
再跑受影響的完整 gate；不需要為每次文字或結構整理重新啟動模型。

## 要改什麼，從哪裡找

| 修改內容 | 主要來源 | 對應測試 |
|---|---|---|
| 搜尋頁面、呈現與文案 | `web/src/main.tsx`、結果與進度元件 | `web/tests/` 的 Playwright 案例 |
| 瀏覽器存取確認、搜尋 session 與錯誤恢復 | `web/src/search/session.ts` | `web/tests/` 的 Playwright 案例 |
| BFF session、身分與私有 API client | `search/web/` | `search/tests/test_search_web*.py` |
| Django／DRF 設定、非同步 HTTP 與服務生命週期 | `search/http/` | `search/tests/test_http_application.py` |
| 多輪搜尋、時間與曆法 | `search/api/conversation/`、`search/planning/` | `search/tests/test_conversation*.py` |
| OpenSearch 條件檢索、Haystack RRF 與重排 | `search/api/retrieval/` | `search/tests/` 的 retrieval 案例 |
| API 共用設定、admission 與 worker 邊界 | `search/api/shared/` | `search/tests/` 的 API／worker 案例 |
| 離線 metadata 正規化與 bundle 建置 | `search/catalog/build/` | `search/tests/` 的 bundle 案例 |
| 封存資料、embedding 配方、Fess 管理與匯入／向量 | `search/catalog/` | `search/tests/` 的 catalog 案例 |
| 產品 CLI、參數與退出碼 | `search/operations/cli.py` | `tests/unit/operations/` 的 CLI 案例 |
| Metadata、catalog 與部署操作 | `search/operations/` | `tests/unit/{operations,catalog,deployment}/` |
| Compose、映像與版本化設定範本 | `deploy/` | `tests/unit/deployment/`、`tests/integration/` |
| 開發及維護人員閱讀的指南 | `docs/guide/` | 下方 MkDocs strict build |

`scripts/depositarai.py` 是產品命令入口；參數解析、dispatch、診斷與退出碼由
`search.operations.cli` 負責。CLI 測試直接匯入這個模組。
命令選定後才載入對應 domain；`--help` 不載入產品配置、Fess 或部署控制器。
配置、資料準備與部署政策放在所屬 operations domain。

Metadata builder 以 `python3 -m search.catalog.build` 執行，從既有快照建立封存資料；
正規化、snapshot IO、檔案發佈與來源身分各有明確 owner。下載、TTL cache 與差異報告
留在 `search/operations/metadata/`，封存資料 reader 留在 `search/catalog/bundle.py`。

前端的 `useSearchSession` 集中管理存取確認、session 版本、搜尋訊息、到期與重設。
`main.tsx` 負責畫面及 assistant-ui 訊息轉換；HTTP 型別、傳輸與錯誤分類留在
`web/src/api.ts`。修改互動時沿用這些分工，不在元件中另存一份 session 狀態。

Runtime 請求不匯入操作控制器；純 HTTP／JSON 等共用模組不依賴
app、模型或部署控制器。`__init__.py` 只放文件，不讀秘密或啟動服務。
搬移時更新所有呼叫端並刪除舊入口，不新增舊路徑相容包。

HTTP view 使用 Django REST Framework，透過 ADRF 呼叫非同步服務；共用 HTTP
設定放在 `search/http/`。沿用既有 Pydantic 契約，不以 DRF Serializer 另維護
一套相同的輸入規則。網站登入由入口服務處理，搜尋 session 留在 BFF 記憶體；目前
不需要 Django 的帳號資料表、ORM 或資料庫 migration。

## 修改與檢查

先跑能直接涵蓋修改的測試。例如調整 import 邊界時，可以使用不啟動服務的檢查：

```sh
python3 -B -m unittest tests.unit.deployment.test_product_module_boundaries -v
```

Python 使用四個空白、雙引號與 Ruff 的88字元目標。需要 formatter 時只格式化本次
修改的檔案；Ruff 版本以 `search/api/requirements-test.lock` 為準。已有對應依賴的
開發環境可直接執行 `ruff check` 與 `ruff format --check`；完整 gate 使用固定依賴。

Ruff 會檢查未使用的 import 與區域變數。另以 Vulture 檢查高信心的未使用參數
與無法執行的程式；已有 test lock 依賴的環境可執行
`bash scripts/ci/python-unused.sh`，完整 CI 也會執行。工具無法判斷所有跨模組入口、
動態註冊或僅被測試引用的功能。刪除整個模組前，仍須查明產品呼叫端、CLI、
Docker 與操作文件，不能只憑靜態掃描結果自動刪除。

網站的本機格式與建置檢查使用 Node24：

```sh
cd web
npm ci --ignore-scripts --no-audit --no-fund
npm run format:check
npm run build
cd ..
```

需要完整範圍時，在已備好 Docker／Compose 的環境執行：

```sh
make search-ci
make search-web-ci
```

`search-ci` 包含核心 Python、部署政策、來源封裝及 API 檢查；`search-web-ci` 包含
網站 build、真實 OSS 與 Nginx／BFF／API browser 流程。兩者用途不同，不能互相
替代。對話、日期與曆法回歸也在這兩條產品流程內，不需另一個研究環境。
隔離測試不等於產品服務已部署。

新增或搬移 runtime 模組時，一起核對 source inputs、Docker COPY、專屬 ignore
與 image-owned import；host operator 與 test helpers 不應因此擴張線上映像。
更改 catalog 的資料格式、投影或向量 recipe，則另評估舊資料是否仍可重用。
Builder 來源及 package 邊界也納入 manifest 與資料配方；改動這些輸入時，即使
搜尋文件內容相同，仍須準備新 catalog，重新匯入並建立向量。

## 編輯與預覽手冊

這個手冊使用 MkDocs，來源僅限 `docs/guide/`。先建立獨立環境並安裝固定依賴：

```sh
python3 -m venv .venv-docs
.venv-docs/bin/python -m pip install --require-hashes -r docs/requirements.lock
.venv-docs/bin/python -m mkdocs serve --dev-addr 127.0.0.1:8000
```

在瀏覽器開啟 `http://127.0.0.1:8000`；修改 Markdown 後會自動重新載入。
若透過 SSH 開發，請轉送遠端8000埠，保持預覽服務只綁定 loopback。

完成後做嚴格建置，確認頁面、設定及連結沒有警告：

```sh
.venv-docs/bin/python -m mkdocs build --strict
```

輸出位於 `site/`，與 `.venv-docs/` 一樣不提交。預覽與建置不會發布網站。
新增頁面時同步調整 `mkdocs.yml` 導覽；頁間使用 guide 內的相對連結，避免依賴
私有 checkout 路徑或 repo 外檔案。

## 文件寫給誰

手冊先說明讀者要做的事、必要條件與下一步。操作頁給可執行指令與失敗處理，
架構頁解釋責任與取捨；不要把任務進度、測試統計或版本驗收流水帳放進正文。
技術契約與內部 runbooks 由 `docs/REFERENCE.md` 導覽；退役操作與開發歷程透過 Git 查閱。
原始量測、模型實驗結果及操作收據放在 Git 外的私有儲存；測試使用可公開的合成 fixture。
真實設定、key、模型與索引不提交到 Git，也不放進文件輸出。

產品部署請看[部署與維護](operations.md)，系統分工請看[架構概覽](architecture.md)，
使用者操作請看[搜尋資料](using-search.md)。
