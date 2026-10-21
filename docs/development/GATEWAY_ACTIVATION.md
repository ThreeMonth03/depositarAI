# Gateway 首次啟用

`prepare-gateway` 準備候選 Nginx 路由；`activate-gateway` 在核對私有 CPU release
與 runtime 後，首次開啟 `127.0.0.1:18083`。命令及前置條件見
[產品 CLI](PRODUCT_LIFECYCLE.md)，日常啟動流程見[部署指南](../guide/operations.md)。

先執行 `start-release` 完成私有模型暖機、readiness、搜尋與 reset 驗收。
`prepare-gateway` 核對已完成的 release、實際容器與網路、映像、模型檔案、
GPU ownership 及查詢權限，再封存候選路由；不建立或重啟 release 服務，
也不重跑模型暖機與搜尋。缺少完成紀錄或實際狀態不符時，命令會停止，
不代為補建 release。`activate-gateway` 會再次核對這些條件，並保留最後的入口搜尋驗收。

## 啟用順序

1. 核對目前應用版本的完成 release、映像及路由素材，以及相容的 catalog、查詢權限與已封存 GPU leases。
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
停止後續發布，不自動 retry 或接管未知 runtime。配方相容且已完成的 schema 2 推論
服務可跨網站版本沿用原封存 GPU authority，保留原 revision 與 image IDs；
舊 schema 1 須先安排明確交接，見[GPU 身分](PRIVATE_RUNTIME.md#gpu-資源與-runtime-身分)。

Wrapper 的 started／failed 收據不能證明 pointer 尚未寫入。中斷時須一起檢查
sealed pointer、activation intent 及 checkpoints。指標已寫入後才發生持久化錯誤時，
CLI 仍保留 `active_changed=true`；已嘗試寫入但無法確定結果時則為 `null`，
兩者都回報需要診斷，不自動重跑啟用。

路由 reload 早於 HTTP 驗收及 pointer 發布；missing pointer 或
`active_changed=false` 仍可能已有網站流量。須核對 `gateway/activation` 的
intent、created／routed／complete 紀錄、路由及實際容器，不能只靠本地 pointer
判斷是否停服。任何 activation partial 狀態或既有 pointer 都會拒絕再次啟用；
人工停用與目前支援的恢復邊界見[故障診斷與人工停用](PRODUCT_LIFECYCLE.md#故障診斷與人工停用)。

這個命令只處理首次同主機私有 gateway 啟用，不提供完整產品 update、rollback、
deactivation 或零停機保證。拆機 CPU 前端見[網站設定](../SEARCH_WEBSITE.md)，
選用公眾入口有獨立的[更新及人工回退流程](PUBLIC_ACCESS.md)。

來源責任分別是 `deployment/gateway/preparation.py`、`activation.py`、`runtime.py`
與 probes；測試鏡射於 `tests/unit/deployment/gateway/`。
