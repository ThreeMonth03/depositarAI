# Gateway 首次啟用

`prepare-gateway` 準備候選 Nginx 路由；`activate-gateway` 在核對私有 CPU release
與 runtime 後，首次開啟 `127.0.0.1:18083`。命令及前置條件見
[產品 CLI](PRODUCT_LIFECYCLE.md)，日常啟動流程見[部署指南](../guide/operations.md)。

## 啟用順序

1. 核對同來源的完成 release、catalog、查詢權限、GPU leases、映像及路由素材。
2. 確認沒有既有 gateway、active pointer 或未完成的啟用狀態。
3. 啟動固定 bootstrap 設定：`/healthz` 為 200，其餘路徑為 503。
4. 對完整 candidate configuration 執行 Nginx syntax check，原子發布 `active.conf`，
   再次檢查後 reload。
5. 執行真實兩輪搜尋、條件調整及 session reset，重新核對 runtime 與收據。
6. 最後才發布產品的 `active.json` pointer。

Gateway 只發布 IPv4 loopback，接入的 website peers、來源與映像須精確符合計畫。
健康端點成功或手動改路由檔都不足以授予 active authority。

## Probe 與失敗恢復

HTTP probe 使用已驗證的 web image，在短生命週期 CPU 容器中執行，沒有 mount、
secret、GPU 或 published port。容器的 profile、退出、移除及完成收據都須符合預期，
前後 immutable image 身分也須一致。主機不用另外安裝 API 型別或套件。

所有 native commands 與 HTTP 操作都有總預算；mutation 前保留完成及清理所需
餘額。任何 syntax、HTTP、來源或 runtime 核對失敗，都保留 intent 與 checkpoints，
停止後續發布。沒有自動 retry、adoption 或跨來源沿用 GPU authority。

Wrapper 的 started／failed 收據不能證明 pointer 尚未寫入。中斷時須一起檢查
sealed pointer、activation intent 及 checkpoints；不要刪掉 state 後直接重跑。
只有正常返回的成功操作才回報 `active_changed=True`。

這個命令只處理首次同主機私有 gateway 啟用，不提供完整產品 update、rollback、
deactivation 或零停機保證。拆機 CPU 前端見[網站設定](../SEARCH_WEBSITE.md)，
選用公眾入口有獨立的[更新及恢復流程](PUBLIC_ACCESS.md)。

來源責任分別是 `deployment/gateway/preparation.py`、`activation.py`、`runtime.py`
與 probes；測試鏡射於 `tests/unit/deployment/gateway/`。
