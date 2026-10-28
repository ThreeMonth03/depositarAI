# 比較 CSV／PDF 轉換工具

使用相同檔案比較 Fess 與 Docling 的欄位、文字、表格及處理成本，協助選擇
正式資料準備流程的轉換工具。這些程式只執行獨立的 CPU 容器，不啟動網站、
搜尋索引或模型服務。

工具使用固定映像；實驗依賴與產品的 Python lock、Compose 及映像建置分開。
轉換結果仍需另行設計來源對應及索引整合，執行實驗不會改變網站搜尋範圍。

## 準備檔案

需要 Docker，以及主機已快取的對應映像。Fess launcher 另使用主機 `javac`
編譯小型呼叫程式，透過映像內原有的 Java 函式庫抽取內容。Docling launcher
直接呼叫映像內的 Python 函式庫與模型；實驗期間不下載依賴或模型。

原始檔、快取、編譯產物及輸出使用 repository 外的私有目錄。已提交的檔案
會進入 Git 歷史與來源交付包，即使它們不會進入產品映像，也不可放入真實資料。

在輸入目錄建立 `manifest.json`，每筆記錄指定完整檔案大小與 SHA256。以下範例
對應 UTF-8、LF、最後有換行的 `water-quality.csv`：

```csv
station,pH
A,7.2
```

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "water-quality",
      "filename": "water-quality.csv",
      "format": "CSV",
      "complete": true,
      "bytes": 17,
      "sha256": "3083585a5f58f9231045232b816f477b5732704b1a118f258ec198e7c2c0c77c",
      "source": "controlled_fixture",
      "processing_options": {
        "encoding": "utf-8",
        "header_policy": "first_record"
      },
      "expected_columns": ["station", "pH"],
      "expected_columns_basis": "declared_controlled_fixture"
    }
  ]
}
```

換成自己的檔案時，需重新填寫實際 bytes 與完整 SHA256；部分下載不得標記為
`complete: true`。`filename` 使用單一檔名；CSV 與 PDF 檔案副檔名分別為
`.csv`、`.pdf`。
`expected_columns` 是選樣時明確指定的預期，用於事後比較，不代表轉換器可以
自動證明第一列是標頭。Big5 可明確指定 `encoding: "cp950"`，無標頭可指定
`header_policy: "absent"`；工具不支援的設定須保留在結果限制中。

## 執行

先建立 repository 外的工作目錄，將下列路徑換成自己的位置。每次比較使用
新的輸出目錄，保留原始結果。

```sh
extract_inputs=/absolute/private/extraction-inputs
extract_results=/absolute/private/extraction-results

python3 experiments/document-extraction/fess/run.py \
  --inputs "$extract_inputs" \
  --output "$extract_results/fess" \
  --scratch "$extract_results/scratch/fess"

python3 experiments/document-extraction/docling/run.py \
  --inputs "$extract_inputs" \
  --output "$extract_results/docling" \
  --format all
```

一般 PDF 先關閉 OCR；掃描件使用另一份輸出，單獨測試：

```sh
python3 experiments/document-extraction/docling/run.py \
  --inputs "$extract_inputs" \
  --output "$extract_results/docling-ocr" \
  --format pdf --ocr --case scanned-document
```

表格重建另可指定 `--table-mode accurate`，使用新的輸出目錄比較 cell 與欄位
關係。`fast` 與 `accurate` 的模型、成本及結果分別記錄，偵測到表格不代表
數值已對應正確。

Fess 的呼叫程式、映像檢查及 PDF 測試素材產生器放在 `fess/`；Docling 的轉換
程式放在 `docling/`。轉換由工具原有的 parser 完成；呼叫程式記錄輸入身分、
設定、輸出與量測。

## 如何比較

- 先核對輸入 SHA256。長 PDF 可先產生固定頁數的衍生檔，讓兩個工具讀取同一份
  bytes，並另存原件身分與選取頁數。
- 分開看 CSV 欄位、PDF 文字、表格結構與 OCR。Fess 一般文字抽取與 Docling
  版面／表格分析的工作量不同，耗時不能直接換算成相同工作的速度差。
- 分開記錄容器啟動、函式庫初始化及檔案轉換；記憶體限制與實際觀察值分開列出。
- 以明確的欄名、文字及表格預期檢查內容。文字比較長、表格比較多，皆不直接
  代表解析比較正確或搜尋效果比較好。
- 格式錯標、無標頭、未知編碼及損壞檔也應保留。呼叫特定 parser 成功，不代表
  它已辨識出實際檔案種類。

確定工具後，將採用的能力接入正式離線資料準備流程，再驗證檢索、排序及來源
呈現；實驗程式不匯入產品 runtime，也不直接操作現役 catalog。
