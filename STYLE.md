# 程式與文件維護規範

本規範以降低單人維護及交接成本為目的。機械式規則以根目錄 `ruff.toml`、
固定版本的工具及 CI 為準；責任邊界、命名與說明是否清楚，由 review 判斷。
修改時先讀附近程式，規範改善逐批進行，不順便重排無關檔案。

## 檔案與目錄命名

- 目錄負責 domain，檔名描述該目錄內的責任，不重複父目錄前綴。
  例如 `deployment/public_entry/profile.py`、`host.py`、`preflight.py`、`updates.py`。
- 測試目錄對應來源 domain，保留 discovery 使用的 `test_` 前綴，例如
  `tests/unit/deployment/public_entry/test_profile.py`；共用 fixtures 依用途命名。
- 保留工具規定的檔名，例如 `__init__.py`、`Dockerfile`、配對的
  `Dockerfile.dockerignore` 與依賴 lock。不要為縮短名稱破壞工具的尋找規則。
- 平面的 `scripts/` CLI 保留可直接辨識用途的名稱，例如 `prepare_search_models.py`；
  從命令列呼叫時仍須能辨認操作對象。改名時同步處理 imports、測試入口與來源封裝。

## Python

- 使用四個空白縮排、UTF-8 及 LF。行寬沿用現有 Ruff 的 88 字元目標，
  字串使用雙引號；formatter 不保證每一種字串或註解都能自動縮到目標寬度。
- 函式與變數使用 `snake_case`，類別使用 `CamelCase`，常數使用 `UPPER_CASE`。
  名稱應讓讀者辨識用途，不用註解彌補含糊命名。
- import 應能看出依賴來源，避免 wildcard import。跨責任邊界使用明確路徑；
  package initializer 保持輕量，不在 import 時讀秘密、啟動模型或操作 Docker。
  搬移模組時同步更新呼叫端並刪除舊入口，不保留舊路徑轉接或測試專用 alias。
- 同一個步驟保持緊湊；較長函式以一個空行分隔不同步驟。
  當判斷、驗證、狀態轉換能獨立理解與測試時，將它們從 IO orchestration 抽出。
  不為每個 helper 新增 class、檔案或泛用 utils。
- 型別優先補在 HTTP 契約、跨模組介面與重要狀態。舊型別寫法在相關修改時整理，
  不為形式一致而一次要求整個專案通過 strict type checking。

Python lint 使用 `search/api/requirements-test.lock` 固定的 Ruff 版本。
`ruff.toml` 明列目前已使用的 `E4`、`E7`、`E9`、`F`，目前未啟用命名、import
排序或 docstring lint。新增規則須另提變更，說明實際收益。

在 repository root、已有對應依賴的開發環境中執行：

```sh
ruff check search scripts tests deploy/docker
ruff format --check search scripts tests deploy/docker
```

需要修正格式時，對本次修改的檔案執行 `ruff format`，再檢查 diff。
CI 使用相同根目錄設定；只掛載部分來源的網站與曆法檢查也須掛載 `ruff.toml`。

未使用的 import 與區域變數由 Ruff 的 `F401`、`F841` 檢查。
`bash scripts/ci/python-unused.sh` 使用固定版本的 Vulture，阻擋信心為100的
未使用參數與無法執行的程式。標準函式庫要求保留的 override／callback 參數，
例外僅限腳本列出的個別檔案與名稱；新增例外時須說明對應介面，不能全域略過。
低信心結果可供人工整理，不能直接作為刪除依據。工具只看靜態名稱引用，
整個功能是否仍被產品使用，須另查入口、動態載入與部署來源。

## 註解與 docstring

程式名稱與結構描述做了什麼；註解補上原因、不變量或無法由型別表達的限制。
不替明顯的呼叫寫步驟旁白，也不寫對話、任務或 review 歷程。

重要公開介面使用 docstring，摘要用一句完整的動作說明；多行說明在摘要後空一行。
明顯易懂的函式可以省略，避免只為滿足形式而重複函式名稱。
必要時交代參數、回傳值、錯誤、單位、所有權、資料形狀及副作用。
參數需要逐項說明時使用 Sphinx 的 `:param name:`、`:return:`、`:rtype:` 欄位，
不在同一介面混用不同 docstring 格式。實作內的 `#` 註解解釋選擇與限制。

安全檢查的理由應留在對應程式附近。抽出共用驗證前，先確認不同呼叫端具有相同
信任邊界；保留拒絕條件、錯誤分類與失敗恢復契約。

## 文件

- 先說明讀者要完成的事情，再提供必要的背景與步驟。README 只保留產品介紹、
  使用範圍與閱讀入口；操作指南說明前置條件、指令、預期結果與故障處理。
- 產品手冊放在 `docs/guide/`，依使用、部署、架構與開發組織。
  其餘 API 細節、維運設定與設計背景由 [技術參考](docs/REFERENCE.md) 導覽。
- 架構文件描述現行元件責任、資料流與採用原因；未採用的提案留在 issue／PR。
  一項操作或介面只有一個主要說明位置，其他頁面提供連結。
- 測試結果、commit SHA、驗收清單與開發進度留在 PR／CI，產品指南不寫工作日誌。
  只在會影響讀者決策或操作時說明限制；不要把每項功能改寫成「哪些已驗證」。
- 文件只描述現行操作、契約與有用的設計取捨；退役入口與驗收歷程透過 Git 查閱。
  版本化量測 JSON 保留原始來源與比較條件，不把它們複製成另一份工作日誌。
- 保留 Markdown 與必要的中文內容。文件入口見 [docs/README.md](docs/README.md)，
  目錄與 package 邊界見 [repository layout](docs/development/REPOSITORY_LAYOUT.md)。

目前前端由 `web/package.json` 的 Prettier 檢查格式，TypeScript 與 Vite 檢查建置。
`web/tsconfig.json` 在產品 `src/` 啟用 `noUnusedLocals` 與 `noUnusedParameters`，
阻擋未使用的本機宣告與參數；仍被匯出的入口是否有用，須由呼叫端及 review 判斷。
產品手冊使用 MkDocs 建置，來源只取 `docs/guide/`。
修改手冊時一併確認導覽、連結與本機預覽；編輯方式見開發指南。

## 提交與驗證

純格式、結構 refactor、行為修正分開提交。每批說明修改目的與已執行的驗證；
日常先跑受影響的測試，合併前完成必要完整檢查。`make search-ci` 對應核心 `validate`
gate；網站的 OSS／browser 分流及彙整另行確認，入口與選取規則以
[CI workflow](.github/workflows/ci.yml) 為準，不能用單一核心 gate 代替整套 CI。
測試應能抓到有意義的錯誤，保護行為、契約或證據，不固定無關段落的排版與複製份數。

新增產品 module 或搬檔時，須同時檢查來源 allowlist、Docker COPY 與 image import
驗證。GPU 實驗依推論、模型、檢索或效能改動決定，不把純格式與文件整理綁到全量重建。

可讀性與說明原則參考 [SOLVCON STYLE.md](https://github.com/solvcon/solvcon/blob/da2315ab2d37becd7d4f2124ae6f3857f0f508a2/STYLE.md)。
本專案的實際工具與驗證範圍以本檔、`ruff.toml` 及 CI 為準。
