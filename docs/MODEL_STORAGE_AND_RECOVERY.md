# 模型、索引與復原素材

模型、catalog、secrets 與備份保存在 Git 外的專用私有儲存。
正常準備與 catalog 重用見[產品 CLI](development/PRODUCT_LIFECYCLE.md)及
[Catalog cache](development/CATALOG_CACHE.md)。來源或 image archive 本身不能
取代索引、私有設定及憑證備份。

## 模型取得

原始碼不附帶模型權重。模型由下列上游提供，部署與再分發時分別遵守其授權及
使用條款；本專案程式碼的授權不會取代模型授權。

| 用途 | 上游來源 |
|---|---|
| Scope | [Mistral Ministral 3 8B Instruct 2512](https://huggingface.co/mistralai/Ministral-3-8B-Instruct-2512-GGUF) |
| Planner | [OpenAI gpt-oss-20b](https://huggingface.co/openai/gpt-oss-20b) |
| Embedding | [IBM granite-embedding-278m-multilingual](https://huggingface.co/ibm-granite/granite-embedding-278m-multilingual) |
| Reranker | [NVIDIA llama-nemotron-rerank-1b-v2 的固定版本](https://huggingface.co/nvidia/llama-nemotron-rerank-1b-v2/tree/828765652b05bd439c9789d2a6d093db1caa1443) |

Reranker 的固定版本同時提供 LICENSE 與 model card 中的 Llama 相關條款，使用前
須一起閱讀。下載與準備仍依程式中的固定 manifest、revision 及檔案雜湊驗證。

## 儲存分工

- 模型使用固定 manifest／blob digest，唯讀掛入對應服務。複製工具為
  `scripts/prepare_search_models.py`；先以 `--help` 確認參數，再選擇全新目的目錄。
- Metadata 快照、封存 bundle、索引／向量及操作收據各自保留來源身分。
  不能把模型快取、catalog cache 與 source checkout 混用同一目錄。
- OpenSearch 索引及向量放在 catalog 專用的 Docker local volume；metadata、模型、
  dictionary、配置與收據另存於私有路徑。備份須同時涵蓋 volume 與必要的私有素材，
  只複製 `state_root` 不會包含索引資料。
- 真實 env、secrets 與安全設定以私有權限另存；不輸出完整 env 到 PR 或文件。
- 備份與交付包另計空間，不能把同一磁碟的另一份檔案當成異地備份。

規劃容量時，同時計入模型原件及副本、metadata、索引、向量、失敗候選與交付包。
未來 PDF／CSV 內文索引需要另估原始檔、抽取文字及向量增長，不能沿用目前
metadata-only 的容量估計。

## 索引復原原則

不要複製正在寫入的 OpenSearch data directory 當作備份。需要冷副本時，先鎖住
維運操作、停止使用該資料的入口／API、embedding、Fess 與 OpenSearch，再確認
資料已關閉。保存 exact configuration、secrets、bundle manifest 及 immutable image ID，
核對副本的完整檔案清單及 digest。

原生 snapshot 與還原須在隔離服務與全新的 catalog volume 進行，
通過以下核對後才考慮接入產品：

1. Snapshot 的索引範圍、alias、mapping 與資料 inventory 符合預期。
2. 每個 dataset ID、document digest 與 URL 符合封存 bundle。
3. 向量完成狀態、權限、查詢與模型依賴均正常。
4. 私有設定及 service credentials 已恢復；不是只有資料索引存在。

ConfigSync plugin 可能先建立開啟的 `configsync` placeholder，與 snapshot restore
衝突。先確認隔離還原目標的狀態，再處理該 placeholder；不能把 close-index 指令
盲目套在提供服務的節點。Snapshot 不包含所需的全部安全設定與其他 system state，
須另行保存，不以 snapshot 成功當成整套產品恢復。

`package-product` 匯出來源、metadata、模型及映像素材；`restore-product` 核對素材
並還原來源，均不等於索引備份或全新主機自動安裝。交接範圍見
[Product restore](development/PRODUCT_RESTORE.md)。正式更新與切換由生命週期管理，
不透過修改 manifest、刪除失敗紀錄或手動覆蓋 active data 完成。
