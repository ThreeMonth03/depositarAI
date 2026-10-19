import { useEffect, useState } from "react";

/** Real elapsed time only: the JSON API does not yet emit stage events. */
export function SearchProgress() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const start = performance.now();
    const timer = setInterval(
      () => setSeconds(Math.floor((performance.now() - start) / 1000)),
      1000,
    );
    return () => clearInterval(timer);
  }, []);
  return (
    <div className="search-progress">
      <div className="progress" role="status">
        <span className="pulse" aria-hidden="true" />
        <span>正在處理你的搜尋</span>
        <span className="elapsed" aria-hidden="true">
          已等候 {seconds} 秒
        </span>
      </div>
      <p className="progress-hint">
        {seconds >= 30
          ? "這次需要多一點時間，請再稍候。"
          : "理解需求、尋找資料，再整理成適合探索的結果。"}
      </p>
      <div className="skeleton" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
    </div>
  );
}
