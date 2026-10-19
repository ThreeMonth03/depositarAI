# Catalog 更新失敗與臨時 token 清理

管理 client 的 `search.catalog.admin.AdminAccess` 在匯入、核對及向量工作結束後
清理臨時 token；各工作共用這一個權限生命週期。
清理成功只表示權限已撤銷，不會把部分匯入或失敗工作改成成功。
正常操作從[產品 CLI](development/PRODUCT_LIFECYCLE.md)開始；
續跑條件見[向量建置](development/VECTOR_PASSES.md)。

```text
工作失敗 → 停止寫入 → 在剩餘預算內清理 token → 保留原失敗
                         ↓ 無法確認已清理
                   明確失敗，交由維護者處理
```

## 為什麼不能直接重送

負載拒絕可能同時影響資料操作與 token 撤銷。收到 HTTP 429 不代表可以立刻重試，
一般 403 也不代表 token 已失效：來源 IP 被封鎖時，同樣可能得到 403。
DELETE 回應遺失可能發生在實際刪除之後，不能將所有錯誤都解讀為尚未執行。

Bulk、建立 token 與啟動 vector job **不自動重送**。先保存工作目錄、輸入 manifest、
nonce 與錯誤；不要刪除 quarantine、覆寫收據或補送部分資料讓狀態看似完成。

## Client 的清理規則

- 只有清理時的明確 HTTP 429 可以重試；每個清理操作至多三次，共用 deadline。
  HTTP 200 內的 status=9、一般 4xx／5xx 及 timeout 不套用這個重試。
- 尊重合法的 `Retry-After` delta-seconds；無 header 時退避 2、4 秒。
  重複、負數、過長或 HTTP-date 格式會拒絕；超出預算也不能縮短等待後硬試。
- 清理最多使用 180 秒，並在原 token 到期前 30 秒停止，另留最後 5 秒供 logout。
  這是使用剩餘有效期，不會延長 token 權限或核心工作 deadline。
- 重試 DELETE 前，依原 nonce 重新核對同一 token ID／內容。已消失就不再刪除；
  身分改變則失敗，不掃描或刪除其他工作的 token。
- DELETE 結果不明時只讀取查證：原 token 在雙次列表快照均不見，且純 bearer
  驗證確實被拒絕，才算清理完成。無法證明時回 `TokenCleanupError`，不重送 DELETE。
- 撤銷驗證接受 HTTP 401 或明確的 authentication-failure envelope，
  不接受 `403 {"error":"ip_blocked"}`；bearer request 不附管理 cookie。
- Logout 遇明確 429 才保留 session 狀態以供有限重試；其他不明結果不重送。
  清除 client cookie／CSRF 不代表 server-side logout 已成功。

## 時間預算

| 項目 | 上限 |
|---|---:|
| Import／reconcile token 有效期 | 15 分鐘 |
| Vector token 有效期 | 35 分鐘 |
| Import／reconcile 核心工作 | 600 秒；reconcile CLI 預設 180 秒 |
| Vector 核心工作／停止與核對 reserve | 1,800／240 秒 |
| 清理 | 最多 180 秒，另受 token 到期前 30 秒約束 |

Vector 用滿預算時可能只剩 30 秒清理，放不下 60 秒的 `Retry-After`，此時必須失敗。
不能延長 token 有效期或關閉負載防護換取完成；產品 CLI 的總預算仍另外限制工作量。

## 維護者如何處理失敗

1. 保存失敗收據與原 catalog，不直接重跑會寫入資料的操作。
2. 核對工作 nonce、token 清理結果與 scheduler 是否已停用；不把不明結果當成已撤銷。
3. 無法確認權限已清理時，依該 catalog 的私有管理流程處理，只針對已辨識的工作，
   不輸出 token、密碼或完整 env。
4. 完成清理後，仍依 inventory、向量 journal 與 quarantine 決定復原或重建。
   部分匯入的 `ImportFailed`／`possibly_polluted=true` 不因 token 已撤銷而清除。

永久過載、斷電或 SIGKILL 可能需要人工恢復；重試規則不提供自動 promotion、
rollback 或完整災難復原。儲存與備份範圍見[模型與索引復原](MODEL_STORAGE_AND_RECOVERY.md)。
