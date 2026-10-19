# CI 操作與耗時排查

日常先執行直接涵蓋修改的檢查，合併前再確認該版本的必要 CI。
網站與核心政策分開檢查；快取只節省下載，不決定是否執行驗證。

`validate` 先以固定版本的 Gitleaks 掃描該 commit 的完整歷史，並驗證原始碼
打包，再執行核心測試。這份暫存包不會上傳；交付 artifact 使用
[Source package](SOURCE_PACKAGE.md) workflow。

## 選擇檢查入口

| Gate | 用途與本機入口 |
|---|---|
| `validate` | 核心 Python、部署政策、來源封裝與 API；`make search-ci`。 |
| `private-website-oss` | 網站共同檢查、真實入口／更新及私有 TLS runtime；`bash scripts/test-search-web.sh --suite oss`。 |
| `private-website-browser` | 網站共同檢查、native gateway bootstrap 與真實 HTTP browser 流程；`bash scripts/test-search-web.sh --suite browser`。 |
| `private-website` | 彙整兩個網站 job 的結果、scope 與來源，不是另一組測試。 |

`make search-web-ci` 執行完整網站流程，等同 `--suite all`；source package workflow
也使用完整流程。OSS 與 browser 分流各自在獨立 runner 建置 API／web 映像，
執行共同的契約、Python、Ruff、npm、TypeScript／Vite 及 image-owned import 檢查。
日期、曆法與正式 OpenSearch／Haystack 行為仍由產品測試保護。分流縮短等待關鍵路徑，
但會重複準備工作，不能把 runner 時間總和當成等待時間。

正式映像先執行不掛入原始碼的 import 檢查。HTTP 測試使用繼承相同 runtime
來源層的 `tests` target，僅額外安裝測試依賴；HTTPX 與 ASGI Lifespan 負責測試
請求及 startup／shutdown，不將測試 client 或其生命週期工具加入正式映像。

只有 PR 可以由 [changed-scope.py](../../scripts/ci/changed-scope.py)略過確定無關的
網站 suite。`main` push 與手動執行維持完整檢查；未知路徑、缺少 base、
來源不符或無法讀取 diff 時也完整執行。Core `validate` 始終執行。
不要手動修改 scope output 來讓失敗的 suite 顯示 skipped。

文件另有 `Documentation` workflow。它只在 `docs/guide/`、`docs/requirements.lock`、
`mkdocs.yml` 或文件 workflow 改動時做 MkDocs strict build；技術參考或程式變更
未觸發這個 workflow，是正常的路徑選取。文件編輯入口見[開發指南](../guide/development.md)。

## 先確認正在看哪個版本

PR CI checkout 是 GitHub 的整合 merge revision，與 PR head 不同；`main` CI
checkout 則是接受的 merge commit。重跑或新增提交後，先核對 run 的 event、head
與 attempt，再讀取該次 job log，避免用前一輪結果判斷新程式。

網站 job 的 `CI_SOURCE sha=... tree=...` 必須與當次 checkout 一致，兩個分流與
彙整也必須相同。文件 workflow 使用 `DOCS_SOURCE`。彙整缺少來源或結果、兩邊
scope 不一致、任一 suite 失敗或 workflow 取消，都不能視為成功。

## 判斷時間花在哪裡

先區分等待 runner 的排隊時間、checkout／下載等環境準備，以及實際測試。
網站 script 的紀錄格式如下：

```text
CI_SUITE suite=oss
CI_PHASE phase=public-entry-runtime seconds=... exit=...
CI_PHASE phase=browser-image seconds=... exit=...
CI_PHASE phase=browser-image-wait seconds=... exit=...
CI_TOTAL seconds=... exit=...
```

`CI_SUITE` 標明驗證範圍；只記錄實際執行的 phases，不把未跑項目填零秒。
`CI_TOTAL` 是 script 的時間，不含 workflow checkout、cache 或 runner 準備。
先比相同 suite 的總時間，再找最長的前景 phase；平行 jobs 不相加當成使用者等待。

Playwright image 缺少時會背景下載，與前景工作重疊。`browser-image` 記下載時間，
`browser-image-wait` 才是 browser 仍被下載阻塞的時間，兩者不能重複加總。
已有 pinned image 時下載 phase 為零；下載失敗或逾時仍會令 gate 失敗。
npm cache 只保存下載內容，仍執行 locked install、格式與建置檢查。

Browser 使用單一 worker，直接連到使用正常 image entrypoint 的隔離 Nginx。
首頁、assets 與 API 請求都經過正式網站設定；案例不重啟 gateway，也沒有額外控制通道。
並發資源載入案例核對靜態檔不會被共享代理 IP 誤擋，其他案例驗證 HTTP、MIME、
安全標頭、缺檔、路徑限制及 API 路由隔離。

公眾頻率、帳號與額度限制由 APISIX 負責，在 OSS runtime suite 使用實際入口驗證。
內層 Nginx 不重複以代理 IP 計數，避免所有使用者共用一個限流預算。

## 失敗時從哪裡查

| 現象 | 先查什麼 |
|---|---|
| Job 還沒開始 | Actions queue／runner 狀態，先分清排隊與測試耗時。 |
| Image 或依賴下載失敗 | 失敗 step、pin、checksum、cache 與 registry 連線；不要略過完整性核對。 |
| `python-images`／`web-image` 慢 | Docker build 的 cache miss 與變更輸入，避免把無關來源放大成整包重建。 |
| Unit 或契約失敗 | 第一個 traceback、失敗案例及該 phase exit code；先在相同來源重現最小範圍。 |
| OSS readiness／bootstrap 失敗 | 該次隔離 fixture 的狀態與啟動 log，不延長所有 timeout 掩蓋真正錯誤。 |
| Browser 429 或偶發互相影響 | 每例 gateway reset／ready、worker 數與 fixture 殘留；保留正式限流。 |
| 分流成功但彙整失敗 | 兩邊 SHA／tree、scope、suite outcome 與取消狀態。 |
| 中斷後仍有背景工作 | 該次 image pull、process group 與 fixture cleanup；只清理已辨識的本輪資源。 |

只在確認為暫時的 runner／網路問題後重跑，保留原失敗與新 attempt，避免把重跑
成功當成問題已修正。若是確定的程式 race，修正實際競爭條件並保留相關回歸案例。

評估加速時，記錄比較的來源、suite、cache 狀況、wall time 與 phases，放在 PR／CI
中供 review。優先處理關鍵路徑上的下載、重複建置或昂貴 fixture；不要為了更漂亮
的秒數少跑案例、放寬資源／安全限制，或新增另一套 CI 管理工具。
