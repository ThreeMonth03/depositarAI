# 網站識別與素材

網站以文件與放大鏡構成的圖形代表資料搜尋，搭配「資料搜尋」文字及獨立的 `AI` 標籤。
頁面名稱維持 depositar AI，資料來源連結指向 depositar 公開資料站。
這個圖形由本專案繪製，不使用 depositar 官方 Logo，也不表示官方認可或合作關係。

## 視覺規則

- 白底、深綠色內文及綠色點綴；連結與焦點框保持清楚可辨。
- Header 在桌面與手機顯示完整圖形及文字，窄螢幕縮小尺寸，保留新搜尋按鈕空間。
- Favicon 只使用文件與放大鏡圖形。
- 首頁連結的輔助名稱為「depositar AI 首頁」；裝飾圖片使用空白 alt，避免重複朗讀。

## 素材與封裝

`web/src/assets/search-logo.svg` 是完整識別，`search-symbol.svg` 是方形 favicon。
兩者使用簡單的幾何圖形；文字沿用使用者系統字型，不載入外部字型或圖片。
AI 標籤由 HTML／CSS 呈現。

素材隨 Vite 建置成帶 hash 的同源靜態檔。修改時不加入 script、外部引用或事件處理器，
也不放寬 CSP。對應 browser 案例檢查同源載入、SVG MIME、手機 header 與版面溢位；
完整介面設計見 [UX 說明](SEARCH_WEBSITE_UX.md)。
