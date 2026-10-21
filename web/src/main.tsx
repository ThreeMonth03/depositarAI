import { useState } from "react";
import { createRoot } from "react-dom/client";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
} from "@assistant-ui/react";
import type { AppendMessage, ThreadMessageLike } from "@assistant-ui/react";
import { useSearchSession } from "./search/session";
import type { SearchMessage } from "./search/session";
import { Results } from "./search-results";
import { SearchProgress } from "./search-progress";
import { SearchAccess } from "./search-access";
import searchLogo from "./assets/search-logo.svg?no-inline";
import "./styles.css";

const convertMessage = (message: SearchMessage): ThreadMessageLike => ({
  id: message.id,
  role: message.role,
  content: [{ type: "text", text: message.text }],
});
const examples = [
  {
    label: "河川與水質",
    hint: "探索環境觀測資料",
    query: "找河川水質觀測資料",
  },
  {
    label: "氣候與變遷",
    hint: "從一段時間開始",
    query: "找 2020 到 2025 年的氣候資料",
  },
  {
    label: "土地與生活",
    hint: "找可下載的 CSV",
    query: "找土地利用的 CSV 資料",
  },
];

function SearchApp() {
  const [topK, setTopK] = useState(5);
  const {
    messages,
    isRunning,
    error,
    mustReset,
    clock,
    epoch,
    access,
    accessInfo,
    errorAction,
    canRetry,
    retry,
    reset,
    submit,
    recheckAccess,
  } = useSearchSession(topK);

  async function onNew(message: AppendMessage) {
    const text = message.content
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join(" ")
      .trim();
    await submit(text);
  }

  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage,
    isRunning,
    onNew,
  });
  const latestResultId = [...messages]
    .reverse()
    .find((message) => message.result?.search)?.id;
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div
        className={`app-shell ${messages.length ? "has-conversation" : "home"}`}
      >
        <header className="site-header">
          <a className="wordmark" href="/" aria-label="depositar AI 首頁">
            <img src={searchLogo} alt="" width="200" height="64" />
            <span className="ai-badge" aria-hidden="true">
              AI
            </span>
          </a>
          <div className="header-actions">
            <a
              className="catalog-link"
              href="https://data.depositar.io"
              target="_blank"
              rel="noopener noreferrer"
            >
              探索 depositar <span aria-hidden="true">↗</span>
            </a>
            <button className="reset" onClick={reset} disabled={isRunning}>
              ＋ 新搜尋
            </button>
          </div>
        </header>
        <main>
          <ThreadPrimitive.Root className="thread" key={epoch}>
            <ThreadPrimitive.Viewport className="viewport" autoScroll={false}>
              <section className={`intro ${messages.length ? "compact" : ""}`}>
                <div className="eyebrow">
                  <span aria-hidden="true">✦</span> 每個問題，都是探索的起點
                </div>
                <h1>
                  想找的資料，
                  <br />
                  <em>從一句話開始。</em>
                </h1>
                <p>用對話，找到需要的研究資料。</p>
              </section>
              <SearchAccess
                status={access}
                info={accessInfo}
                recheck={recheckAccess}
              />
              <div className="transcript" aria-label="本次搜尋對話">
                <ThreadPrimitive.Messages>
                  {({ message }) => {
                    const source = messages.find((m) => m.id === message.id);
                    return (
                      <MessagePrimitive.Root
                        className={`message ${message.role}`}
                      >
                        <div className="message-label">
                          {message.role === "user"
                            ? "你的搜尋"
                            : "depositar AI"}
                        </div>
                        <MessagePrimitive.Parts
                          components={{
                            Text: ({ text }) => (
                              <p className="message-text">{text}</p>
                            ),
                          }}
                        />
                        {source?.result?.search &&
                        source.id !== latestResultId ? (
                          <details className="previous-results">
                            <summary>
                              查看這次的 {source.result.search.cards.length}{" "}
                              筆結果
                            </summary>
                            <Results result={source.result} />
                          </details>
                        ) : (
                          source?.result && <Results result={source.result} />
                        )}
                      </MessagePrimitive.Root>
                    );
                  }}
                </ThreadPrimitive.Messages>
              </div>
              {isRunning && <SearchProgress />}
              {error && (
                <div className="error" role="alert">
                  <p>{error}</p>
                  <div className="error-actions">
                    {errorAction === "login" && accessInfo?.login_url && (
                      <a href={accessInfo.login_url}>免費登入，取得較多額度</a>
                    )}
                    {errorAction === "verify" && (
                      <a
                        href="/verify"
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        完成瀏覽器驗證 ↗
                      </a>
                    )}
                    {canRetry && (
                      <button
                        onClick={retry}
                        disabled={isRunning || access !== "ready"}
                      >
                        重試這次搜尋
                      </button>
                    )}
                  </div>
                </div>
              )}
              <ThreadPrimitive.ViewportFooter className="composer-area">
                <ComposerPrimitive.Root className="composer">
                  <ComposerPrimitive.Input
                    aria-label="搜尋或追問"
                    placeholder={
                      messages.length
                        ? "繼續追問，例如：只要 CSV、改成 2024 年、取消時間限制…"
                        : "想研究什麼？例如：2020 年以後的河川水質資料"
                    }
                    maxLength={512}
                    disabled={isRunning || mustReset || access !== "ready"}
                  />
                  <div className="composer-bottom">
                    <label>
                      結果數{" "}
                      <select
                        aria-label="結果數"
                        value={topK}
                        disabled={isRunning || access !== "ready"}
                        onChange={(e) => setTopK(Number(e.target.value))}
                      >
                        {[5, 10, 20].map((k) => (
                          <option key={k}>{k}</option>
                        ))}
                      </select>
                    </label>
                    <ComposerPrimitive.Send
                      disabled={mustReset || access !== "ready"}
                      aria-label="送出搜尋"
                    >
                      {messages.length ? "繼續搜尋" : "開始探索"}{" "}
                      <span aria-hidden="true">↑</span>
                    </ComposerPrimitive.Send>
                  </div>
                </ComposerPrimitive.Root>
                <p className="composer-hint">
                  {messages.length
                    ? "接著試試：只要 CSV、換個年份，或探索另一個主題。"
                    : "不必想好關鍵字，從你想了解的事情開始。"}
                </p>
              </ThreadPrimitive.ViewportFooter>
              {messages.length === 0 && (
                <section className="inspiration" aria-label="搜尋靈感">
                  <p>
                    還沒有方向？從這些主題開始 <span aria-hidden="true">↘</span>
                  </p>
                  <div className="examples">
                    {examples.map((example) => (
                      <ThreadPrimitive.Suggestion
                        key={example.query}
                        prompt={example.query}
                        send
                        disabled={access !== "ready" || isRunning || mustReset}
                        aria-label={example.query}
                      >
                        <span className="example-title">
                          {example.label}
                          <span aria-hidden="true">↗</span>
                        </span>
                        <span className="example-hint">{example.hint}</span>
                      </ThreadPrimitive.Suggestion>
                    ))}
                  </div>
                </section>
              )}
            </ThreadPrimitive.Viewport>
          </ThreadPrimitive.Root>
        </main>
        <footer className="site-footer">
          <span>Open data. New discoveries.</span>
          <details className="usage-notes">
            <summary>使用說明</summary>
            <p>
              每次搜尋暫存 30 分鐘，最多 20
              輪。不建立永久對話記憶；重新整理或「新搜尋」即可重新開始。
            </p>
            <p>
              可追問主題、資料涵蓋時間或格式。目前不提供附近、距離篩選或檔案內文問答。各次結果的「搜尋詳情」提供來源與搜尋範圍。
            </p>
            <p>
              年份可寫「民國111年」或「西元2022年」。農曆相關主題可以搜尋；農曆日期僅作文字搜尋，不換算或套用日期篩選。
            </p>
            {clock && <p>本次參考日：{clock}</p>}
          </details>
        </footer>
      </div>
    </AssistantRuntimeProvider>
  );
}

createRoot(document.getElementById("root")!).render(<SearchApp />);
