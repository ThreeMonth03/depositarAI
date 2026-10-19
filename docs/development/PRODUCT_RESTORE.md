# 產品包離線驗證與來源還原

`restore-product` 是新主機安裝的前置步驟。它不需要 product config、Docker、GPU
或已存在的模型目錄；可以先核對產品包，再還原可執行的固定來源。
完整網站的 `install`／`update` 與 registry identity restore 仍須另行完成。

產品素材包可能含非公開 metadata、模型與部署映像，應保存在 operator 的受保護位置。
公開的 GitHub repository 與 [source package](SOURCE_PACKAGE.md) 只交付原始碼；
不要把產品包、真實設定、憑證或 runtime 備份當成公開 artifact。

## 使用方式

先透過可信的交付紀錄取得預期 application revision 與 `package.json` SHA256。
不能只從待驗證包內讀取這兩個值，然後把它們當作外部信任來源。
來源根目錄、產品包與目標父目錄必須是 operator 擁有的私有目錄；目標須為尚未存在
的新路徑，且不得與產品包或執行此命令的 repository 重疊。

將下列兩個 `EXPECTED_` 值替換為交付者提供的版本與 manifest SHA256：

```bash
python3 scripts/depositarai.py restore-product \
  --package /absolute/private/packages/product-v1 \
  --expected-revision EXPECTED_REVISION \
  --expected-manifest-sha256 EXPECTED_MANIFEST_SHA256 \
  --destination /absolute/private/restores/product-v1
```

此命令不使用 `--config`。其他既有產品命令仍要求私有 product config。
目前只接受包含全部模型 bytes 的完整素材包；`--external-models` 匯出的包不能在此
入口當成完整交接。沒有隱式下載、Docker pull／load 或失敗後自動重試。

## 實際核對與輸出

命令核對外部指定的 manifest SHA 與 revision、封閉的 manifest schema、完整檔案
清單及 SHA256、映像 archive 結構、固定模型清單、metadata bundle 與來源閉合。
沿用來源還原工具，將包內 Git bundle 還原至精確 revision，核對 archive／Git tree、
clean checkout 及 CLI help。Git 子程序隔離外部 Git 設定與環境，避免其他 repository
設定影響這次還原。

大型模型與 `images.tar` 留在原包，只還原來源並準備小型 metadata 驗證副本，避免
再複製整個大型 payload。**原包是還原目錄持續依賴的素材，必須保留。**
後續安裝使用素材前仍須重新核對，不能把這次成功收據當作檔案永遠不變的證明。

成功結果只證明素材已驗證、來源已還原。最後才寫入成功收據；失敗保留已建立的
partial 目錄供診斷，不覆寫、續寫或自動清除。修復失敗原因後，改用新的目標路徑。
輸出沒有原始 subprocess 訊息、秘密或模型回答。

## 還原後的部署

`image_imported`、`registry_identity_restore_verified`、`runtime_install_verified`、
`install_complete`、`deployment_performed` 與 `active_changed` 都保持 false。
此命令不產生 image-stage、query、release 或 gateway 完成收據，不開 ports，
也不改動既有試用服務。

Docker archive 完整不等於 `docker load` 後的 image ID、revision tag 與 registry digest
都能恢復；尤其官方 Ollama 的 `RepoDigests` 仍是既有 runtime 的必要核對項。
下一階段須在獨立 Docker daemon 或新主機驗收映像載入與身分，再串接索引、query、
inference、release 與 gateway。單純換一個資料目錄仍可能使用相同 Compose project
名稱，不能據此宣稱已與現役 runtime 隔離。

還原的 application revision 與執行還原工具的版本各自獨立。新版工具支援的配置或
儲存功能，不會因此加入還原的舊來源；配置 schema 以還原來源的範本與 CLI 為準。
產品包不包含 live index snapshot 或真實憑證。部署前的設定、資料準備與啟動方式
見[部署與維護](../guide/operations.md)；詳細 CLI 契約見[產品生命週期](PRODUCT_LIFECYCLE.md)。
