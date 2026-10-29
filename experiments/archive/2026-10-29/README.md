# 2026-10-29 實驗歷史摘要

本目錄保留四組既有調查的精簡結果、限制及來源版本，方便清理私人實驗目錄後
仍能理解當時做過什麼。本次只整理既有證據，沒有重跑下載、模型、搜尋或文件
轉換，也沒有修改產品程式、部署、索引或配額。

封存分支以 `415db0fc272cb62e14424a5afb1a570bb27df25e` 為基準；以下狀態均是
歷史實驗狀態，不是目前服務、硬體或資料來源的健康證明。

| 調查 | 已完成的範圍 | 未完成或不可推論的範圍 |
| --- | --- | --- |
| 資源盤點與 CSV 開頭 | 封存 metadata 盤點、8 份有界 CSV 開頭與離線快取重播 | 正式欄位索引、檢索品質、全站下載成功率 |
| Reranker 對照 | 前置檢查及 8 個候選案例提案 | 尚未執行對照、未標註相關性，沒有品質差值 |
| 搜尋正確性 | 既有服務的同 session 三輪搜尋、條件一致性重播、來源與 CI 檢查 | 未部署新來源；沒有完整 RRF／reranker 中間序列或相關性評分 |
| 文件抽取 | 20 個選樣／控制案例的 Fess、Docling CPU 比較與針對性追測 | 不是全站正確率、OCR 準確率或正式索引驗收 |

## 保留的檔案

- `resource-readiness.json`：3,618 個資料集、14,683 個資源的選定統計；CSV／PDF
  宣告大小、bbox／日期缺漏、有界 prefix 及快取重播結果。
- `rerank-proposal.json`：8 個未執行的案例提案；沒有把候選目標當成相關性標註。
- `search-quality.json`：三輪狀態承接、資訊不足結果、歷史 CI 與來源版本。
- `document-extraction.json`：共同樣本範圍、主要發現與原始交付界線。
- `fess-extraction-summary.json`、`docling-extraction-summary.json`：固定工具版本、
  成功／失敗／不支援狀態及資源量測摘要。
- `provenance.json`：原始證據檔的相對名稱、大小與 SHA256；沒有私人目錄位置。

## 結果的解讀

### CSV／PDF 準備度

CSV 宣告資源共 658 筆，分布於 193 個資料集；PDF 共 8,744 筆，分布於 2,447 個
資料集。宣告的正數大小分別合計約 3.40 GiB 與 34.98 GiB，但含重複 URL 且有
大小缺漏，並未計入文字、向量及備份，不能直接當成儲存配置需求。

首次 urllib 探測有 8 次網路錯誤；後續 curl 有界探測成功取得 8 份開頭，總共
233,711 bytes，其中 5 份完整、3 份部分。快取重播沒有新增網路請求。
兩種 transport 行為差異沒有完成根因定位。86 個新增字面欄名不等於新增語意
召回，也沒有證明最終排名改善。標頭只經助理複核，並非使用者標註。

### 搜尋正確性與排序

歷史查詢依序是「找 2020 到 2025 年的氣候資料」、「我要2024年的」、「只要 CSV」。
三輪使用既有 API `4a43cddaeb1a527f4b3d0c5a6f128fd94a2ea0c2`，並非 PR #9 新建
映像。年份替換與 CSV 條件承接正常；原始案例的資料涵蓋期間缺失，結果保留為
`unknown`，不能用 metadata 建立年份斷言它是 2020 年觀測資料或符合 2024 年。

各輪 20 個候選、5 個結果，均有一筆資訊不足。原始案例排名為 4、1、1，但缺少
完整中間候選與分數，無法據此判斷哪個排序階段提升了它，更不能宣稱 reranker
優於 RRF。另一組 reranker 實驗只有提案，尚未執行。

### 文件抽取

兩套工具都符合 8 份選定真實 CSV 的預期第一列。Fess 的明確 CP950／無標頭政策
案例可用；本次固定 Docling 版本的對應設定不支援，且 Big5 案例失敗。兩者均
可能直接接受 HTML 錯標資源，所以 parser 成功不能替代格式辨識。

Fess 的一般 PDF 抽取是純文字且未啟用 OCR。Docling fast 模式在單一控制表格
錯置值與欄位；accurate 追測符合該表格的 9 個 cell。中文掃描件三頁 OCR 產生
774 字元，但未驗證逐字與表格準確率。Docling report 的 `success` 包含空內容
或結構不足的可能性，不能直接當成成功索引。

量測保留設定、程序高水位與工具版本。純文字抽取、版面／表格重建、OCR 的工作
不同，不計算兩套工具的速度比，也不將小樣本結果外推到全站。

## 可取得的實驗程式

相關程式已存在於以下公開 Git commit，本次不再複製私人路徑綁定的調查腳本：

- 搜尋正確性 checker 與測試：
  [`9584a60c`](https://github.com/ThreeMonth03/depositarAI/commit/9584a60c43db2526ac8fe0019b7fc43206571ec6)，
  已由 [PR #9](https://github.com/ThreeMonth03/depositarAI/pull/9) 合併成
  `415db0fc272cb62e14424a5afb1a570bb27df25e`。
  入口是 `scripts/check_search_quality.py`，使用自備的相容 capture；工具測試入口為
  `python3 -B -m unittest tests.unit.operations.test_search_quality`。
- 文件抽取 launcher、Java 呼叫程式、控制素材產生器與使用文件：
  [`cd5f28ee`](https://github.com/ThreeMonth03/depositarAI/commit/cd5f28eed2946d09fed559ea85ab974aa816a9e5)，
  分支 `experiment/document-extraction-1esicrts`，路徑
  `experiments/document-extraction/`。該交付沒有建立 PR、跑產品 CI 或部署。
  原 README 有合成 CSV manifest 與明確 launcher 指令；需要自備輸入及固定映像。

PR #9 的歷史交接收據記錄當時仍為 `OPEN`；本封存另列後續 merge commit，沒有
改寫舊收據的時間語意。來源及 CI 成功不代表當時已部署。

## 沒有公開或重複保留的內容

未加入原始資料、CSV／PDF bytes、抽取文字／表格、完整請求回應、私人日誌、
API key、憑證、主機與營運路徑、Docker／模型／索引狀態、映像、模型權重、
編譯產物及來源打包／還原副本。這些檔案不是公開來源交付的一部分。

來源打包副本、編譯產物、工具與模型快取可由對應版本及依賴重建。原始來源網站
的內容則可能改變；未公開的固定輸入及原始 report 清理後，僅有 SHA256 不足以
重現相同 bytes。因此本目錄是可核對的歷史摘要，不是完整、可逐 byte 重跑的
實驗資料包。重新採集或執行會產生新實驗，需另記來源、版本、輸入及結果。
