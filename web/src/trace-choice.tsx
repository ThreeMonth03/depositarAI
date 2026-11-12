import type { CaptureStatus, TracePolicy, TraceStatus } from "./api";

function captureMessage(status: CaptureStatus): string {
  switch (status) {
    case "ready":
      return "本次新搜尋已選擇提供測試紀錄。";
    case "queued":
      return "本輪紀錄已排隊，尚未確認保存。";
    case "captured":
      return "已確認保存先前的測試紀錄。";
    case "disabled":
      return "本次搜尋未提供測試紀錄。";
    case "revoked":
      return "已停止提供後續測試紀錄。";
    default:
      return "部分測試紀錄未能保存；搜尋結果不受影響。";
  }
}

function deletionMessage(trace: TraceStatus, hasSession: boolean): string {
  switch (trace.deletion_status) {
    case "complete":
      return "已停止提供測試紀錄，已保存的紀錄已刪除。";
    case "pending":
      return hasSession
        ? "已停止提供後續紀錄，既有紀錄仍待刪除。可稍後確認進度。"
        : "已停止提供後續紀錄，既有紀錄仍待刪除，請聯絡維護者確認。";
    case "unavailable":
      return hasSession
        ? "已停止提供後續紀錄，暫時無法完成刪除。請重試。"
        : "已停止提供後續紀錄，暫時無法確認刪除，請聯絡維護者處理。";
    default:
      return captureMessage(trace.capture_status);
  }
}

export function TraceChoice({
  policy,
  selected,
  setSelected,
  trace,
  error,
  hasSession,
  locked,
  withdrawing,
  withdraw,
}: {
  policy: TracePolicy | undefined;
  selected: boolean;
  setSelected: (value: boolean) => void;
  trace: TraceStatus | null;
  error: string;
  hasSession: boolean;
  locked: boolean;
  withdrawing: boolean;
  withdraw: () => void;
}) {
  if (!policy) return null;
  if (!policy.available && !trace?.test_session_id)
    return <p className="trace-unavailable">此搜尋未啟用自願測試紀錄。</p>;

  const canWithdraw =
    hasSession &&
    !!trace?.test_session_id &&
    (trace.enabled ||
      error ||
      ["pending", "unavailable"].includes(trace.deletion_status));
  return (
    <section className="trace-choice" aria-labelledby="trace-title">
      <h2 id="trace-title">自願提供測試紀錄</h2>
      <p>
        選擇後，從本次新搜尋開始私下保存提問原文、前後搜尋條件、待釐清問題、
        結果資料集與順序、警告及耗時，最長 {policy.retention_days} 天。
        不選擇也能使用搜尋。
      </p>
      <p>
        不保存登入憑證或模型隱藏推理。原文可能含敏感內容，請勿輸入秘密。
        隨機測試識別不代表完全匿名。可在本次搜尋有效期間撤回並要求刪除；
        到期或重新整理後，請憑測試識別聯絡維護者處理。
      </p>
      <label className="trace-opt-in">
        <input
          type="checkbox"
          checked={selected}
          onChange={(event) => setSelected(event.target.checked)}
          disabled={locked || hasSession || !policy.available}
        />
        我已了解，願意從這次新搜尋開始提供測試紀錄
      </label>
      {hasSession && !trace?.enabled && !trace?.test_session_id && (
        <p>本次搜尋未提供測試紀錄。請開始新搜尋後重新選擇。</p>
      )}
      {trace?.test_session_id && (
        <div className="trace-state" aria-live="polite">
          <p>{deletionMessage(trace, hasSession)}</p>
          <p className="trace-id">測試識別：{trace.test_session_id}</p>
        </div>
      )}
      {error && (
        <p className="trace-error" role="alert">
          {error}
        </p>
      )}
      {canWithdraw && (
        <button
          className="trace-withdraw"
          onClick={withdraw}
          disabled={withdrawing}
        >
          {withdrawing
            ? "正在確認撤回…"
            : error
              ? "重試撤回"
              : trace.enabled
                ? "撤回並刪除測試紀錄"
                : "確認刪除進度"}
        </button>
      )}
    </section>
  );
}
