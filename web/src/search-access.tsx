import type { Access } from "./api";

function allowance(limits: Access["limits"]): string {
  if (limits.turns === null || limits.window_seconds === null) return "";
  const seconds = limits.window_seconds;
  const window =
    seconds % 3600 === 0
      ? `${seconds / 3600} 小時`
      : seconds % 60 === 0
        ? `${seconds / 60} 分鐘`
        : `${seconds} 秒`;
  return `每 ${window}最多 ${limits.turns} 次搜尋`;
}

export function SearchAccess({
  status,
  info,
  recheck,
}: {
  status: "checking" | "ready" | "login" | "verify" | "unavailable";
  info: Access | null;
  recheck: () => void;
}) {
  if (status === "checking")
    return (
      <p className="access-status" role="status">
        正在確認使用狀態…
      </p>
    );
  if (status === "unavailable")
    return (
      <div className="error access-unavailable" role="alert">
        <span>暫時無法確認使用狀態，請稍後再試。</span>
        <button onClick={recheck}>重新確認</button>
      </div>
    );
  if (status === "verify")
    return (
      <section className="access-notice" aria-labelledby="verify-title">
        <h2 id="verify-title">完成瀏覽器驗證後開始搜尋</h2>
        <p>請在新分頁完成驗證，再回來重新確認。搜尋不會自動送出。</p>
        <div className="access-actions">
          <a
            className="login-link"
            href="/verify"
            target="_blank"
            rel="noopener noreferrer"
          >
            完成瀏覽器驗證 ↗
          </a>
          <button className="reset" onClick={recheck}>
            驗證後重新確認
          </button>
        </div>
      </section>
    );
  if (status === "login")
    return (
      <section className="access-notice" aria-labelledby="access-title">
        <h2 id="access-title">免費登入後開始搜尋</h2>
        <p>
          AI 搜尋提供有限的使用額度，讓每個人都有探索的機會。你也可以直接瀏覽
          depositar 的公開資料。
        </p>
        <div className="access-actions">
          <a className="login-link" href="/auth/login">
            免費登入
          </a>
          <a
            href="https://data.depositar.io"
            target="_blank"
            rel="noopener noreferrer"
          >
            瀏覽公開資料 ↗
          </a>
        </div>
      </section>
    );
  if (!info || info.mode === "private") return null;
  return (
    <div className="access-status access-summary">
      <p>
        {info.principal.kind === "guest" ? "訪客試用" : "已登入"}
        {" · "}
        {allowance(info.limits)}
      </p>
      {info.login_url && <a href={info.login_url}>免費登入，取得較多額度</a>}
      {info.logout_url && <a href={info.logout_url}>登出</a>}
    </div>
  );
}
