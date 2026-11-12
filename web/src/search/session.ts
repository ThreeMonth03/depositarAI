import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  classifySearchError,
  decodeAccess,
  decodeReply,
  decodeSession,
  decodeTraceReply,
  request,
} from "../api";
import type { Access, Session, TraceStatus, TurnResult } from "../api";

export type SearchMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  result?: TurnResult;
};

export function useSearchSession(topK: number) {
  const [messages, setMessages] = useState<SearchMessage[]>([]);
  const [isRunning, setRunning] = useState(false);
  const [error, setError] = useState("");
  const [mustReset, setMustReset] = useState(false);
  const [clock, setClock] = useState("");
  const [epoch, setEpoch] = useState(0);
  const [captureChoice, setCaptureChoice] = useState(false);
  const [trace, setTrace] = useState<TraceStatus | null>(null);
  const [traceError, setTraceError] = useState("");
  const [isTraceBusy, setTraceBusy] = useState(false);
  const [captureBlocked, setCaptureBlocked] = useState(false);
  const [hasSession, setHasSession] = useState(false);
  const [isResetting, setResetting] = useState(false);
  const [access, setAccess] = useState<
    "checking" | "ready" | "login" | "verify" | "unavailable"
  >("checking");
  const [accessInfo, setAccessInfo] = useState<Access | null>(null);
  const [errorAction, setErrorAction] = useState<"login" | "verify" | null>(
    null,
  );
  const [retryQuery, setRetryQuery] = useState<string | null>(null);
  const [accessAttempt, setAccessAttempt] = useState(0);
  const previousAccess = useRef<Access | null>(null);
  const session = useRef<Session | null>(null);
  // These IDs are component-local React keys, not authentication tokens.
  const nextMessageId = useRef(0);
  const generation = useRef(0);
  const busy = useRef(false);
  const traceBusy = useRef(false);
  const resetting = useRef(false);
  const traceGeneration = useRef(0);
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timezone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Taipei";

  useEffect(
    () => () => {
      if (expiry.current) clearTimeout(expiry.current);
      generation.current++;
      traceGeneration.current++;
    },
    [],
  );

  useEffect(() => {
    let active = true;
    setAccess("checking");
    request<unknown>("/api/access", undefined, undefined, "GET", 10_000)
      .then(decodeAccess)
      .then((value) => {
        if (!active) return;
        const previous = previousAccess.current;
        if (
          previous &&
          (previous.mode !== value.mode ||
            previous.principal.kind !== value.principal.kind ||
            previous.principal.id !== value.principal.id)
        ) {
          clearSession();
          setMustReset(false);
          setError("使用身分已變更，請開始新的搜尋。");
          setErrorAction(null);
        }
        previousAccess.current = value;
        setAccessInfo(value);
        setAccess("ready");
      })
      .catch((err) => {
        if (!active) return;
        if (
          err instanceof ApiError &&
          err.status === 401 &&
          err.code === "authentication_required"
        ) {
          clearSession();
          setAccessInfo(null);
          setAccess("login");
        } else if (
          err instanceof ApiError &&
          err.code === "challenge_required"
        ) {
          setAccess("verify");
        } else setAccess("unavailable");
      });
    return () => {
      active = false;
    };
  }, [accessAttempt]);

  useEffect(() => {
    const restored = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      // A cached document may belong to the account used before login/logout.
      clearSession();
      setMustReset(false);
      setError("");
      setErrorAction(null);
      setAccessAttempt((value) => value + 1);
    };
    window.addEventListener("pageshow", restored);
    return () => window.removeEventListener("pageshow", restored);
  }, []);

  function clearSession(traceStatus: TraceStatus | null = null) {
    generation.current++;
    traceGeneration.current++;
    setHasSession(false);
    setCaptureChoice(false);
    setTrace(traceStatus);
    setTraceError("");
    setCaptureBlocked(false);
    session.current = null;
    if (expiry.current) clearTimeout(expiry.current);
    expiry.current = null;
    setMessages([]);
    setClock("");
    setErrorAction(null);
    setRetryQuery(null);
    setEpoch((e) => e + 1);
  }

  function pollTraceStatus(current: Session, traceFence: number) {
    const ownerGeneration = generation.current;
    const identifier = current.trace?.test_session_id;
    if (!identifier) return;
    const isCurrent = () =>
      generation.current === ownerGeneration &&
      traceGeneration.current === traceFence &&
      session.current === current &&
      current.trace?.test_session_id === identifier;
    void (async () => {
      for (const delay of [100, 500, 1500]) {
        await new Promise((resolve) => setTimeout(resolve, delay));
        if (!isCurrent()) return;
        let next: TraceStatus;
        try {
          next = decodeTraceReply(
            await request<unknown>(
              "/api/session",
              undefined,
              current.token,
              "GET",
              10_000,
            ),
          );
        } catch {
          // Status checks never alter search state or recovery actions.
          return;
        }
        if (!isCurrent() || next.test_session_id !== identifier) return;
        current.trace = next;
        setTrace(next);
        if (
          next.capture_status !== "queued" &&
          next.deletion_status !== "pending"
        )
          return;
      }
    })();
  }

  async function reset() {
    if (busy.current || traceBusy.current || resetting.current) return;
    resetting.current = true;
    traceGeneration.current++;
    setResetting(true);
    const current = session.current;
    const ownerGeneration = generation.current;
    try {
      let clearedTrace: TraceStatus | null = null;
      if (current) {
        const response = await request<unknown>(
          "/api/session",
          undefined,
          current.token,
          "DELETE",
          10_000,
        );
        if (current.trace?.test_session_id) {
          clearedTrace = decodeTraceReply(response);
          if (clearedTrace.enabled) throw new Error("trace_not_stopped");
        }
      }
      if (generation.current !== ownerGeneration || session.current !== current)
        return;
      clearSession(clearedTrace);
      setError("");
      setErrorAction(null);
      setMustReset(false);
    } catch {
      if (generation.current !== ownerGeneration || session.current !== current)
        return;
      setError("無法確認新搜尋是否完成，請重試。原有對話暫時保留。");
      if (current?.trace?.test_session_id) {
        setTraceError("無法確認測試紀錄是否已停止或刪除，請重試撤回。");
        setCaptureBlocked(true);
      }
    } finally {
      resetting.current = false;
      setResetting(false);
    }
  }

  async function withdrawCapture() {
    const current = session.current;
    if (
      !current?.trace?.test_session_id ||
      traceBusy.current ||
      resetting.current
    )
      return;
    // A late turn response must not restore a choice the user has withdrawn.
    traceGeneration.current++;
    traceBusy.current = true;
    setTraceBusy(true);
    setCaptureBlocked(true);
    setTraceError("");
    try {
      const response = decodeTraceReply(
        await request<unknown>(
          "/api/session",
          { capture: false },
          current.token,
          "PATCH",
          10_000,
        ),
      );
      if (session.current !== current) return;
      if (response.enabled) throw new Error("trace_not_stopped");
      current.trace = response;
      setTrace(response);
      setCaptureChoice(false);
      setCaptureBlocked(false);
      if (response.deletion_status === "pending")
        pollTraceStatus(current, traceGeneration.current);
    } catch {
      if (session.current !== current) return;
      setTraceError("無法確認撤回是否完成。已暫停送出新的搜尋，請重試撤回。");
    } finally {
      traceBusy.current = false;
      setTraceBusy(false);
    }
  }

  async function submit(text: string, retry = false) {
    if (
      !text ||
      busy.current ||
      resetting.current ||
      traceBusy.current ||
      captureBlocked ||
      mustReset ||
      access !== "ready"
    )
      return;
    if ([...text].length > 512) {
      setErrorAction(null);
      setRetryQuery(null);
      setError("每次請輸入 512 個字以內。");
      return;
    }
    busy.current = true;
    setRunning(true);
    setError("");
    setErrorAction(null);
    setRetryQuery(null);
    if (!retry) {
      const id = String(++nextMessageId.current);
      setMessages((prev) => [...prev, { id, role: "user", text }]);
    }
    const ownerGeneration = generation.current;
    // A newer attempt invalidates status responses from an earlier turn.
    const turnTraceGeneration = ++traceGeneration.current;
    let current = session.current;
    try {
      if (!current) {
        current = decodeSession(
          await request<unknown>(
            "/api/session",
            captureChoice
              ? { capture: true, notice_version: "search-trace-v1" }
              : {},
          ),
        );
        if (generation.current !== ownerGeneration) return;
        session.current = current;
        setHasSession(true);
        setTrace(current.trace ?? null);
        if (captureChoice && !current.trace?.enabled)
          throw new Error("capture_not_confirmed");
        if (!captureChoice && current.trace?.enabled)
          throw new Error("unexpected_capture");
        expiry.current = setTimeout(() => {
          clearSession();
          setMustReset(true);
          setError("本次搜尋已到期並清除，請開始新搜尋。");
        }, current.expires_in * 1000);
      }
      const reply = decodeReply(
        await request<unknown>(
          "/api/turn",
          { query: text, top_k: topK, timezone, version: current.version },
          current.token,
        ),
        current,
      );
      if (generation.current !== ownerGeneration || session.current !== current)
        return;
      current.version = reply.version;
      if (turnTraceGeneration === traceGeneration.current && reply.trace) {
        current.trace = reply.trace;
        setTrace(reply.trace);
        if (reply.trace.capture_status === "queued")
          pollTraceStatus(current, turnTraceGeneration);
      }
      setClock(
        `${reply.context_used.reference_date} · ${reply.context_used.timezone}`,
      );
      const id = String(++nextMessageId.current);
      setMessages((prev) => [
        ...prev,
        {
          id,
          role: "assistant",
          text:
            reply.result.status === "ok"
              ? reply.result.search?.cards.length
                ? `這次找到 ${reply.result.search.cards.length} 筆資料，看看有沒有適合你的。`
                : "這次沒有找到結果，可以試著調整條件。"
              : reply.result.message,
          result: reply.result,
        },
      ]);
      if (current.version >= current.max_turns) {
        setMustReset(true);
        setError("已完成本次搜尋的 20 輪上限，請開始新搜尋。");
      }
    } catch (err) {
      // Expiry invalidates failed replies as well as successful ones.
      if (generation.current !== ownerGeneration || session.current !== current)
        return;
      if (
        err instanceof ApiError &&
        err.trace &&
        current &&
        turnTraceGeneration === traceGeneration.current
      ) {
        current.trace = err.trace;
        setTrace(err.trace);
        if (err.trace.capture_status === "queued")
          pollTraceStatus(current, turnTraceGeneration);
      }
      const failure = classifySearchError(err);
      if (failure.recovery === "login") {
        clearSession();
        setMustReset(false);
        setAccessInfo(null);
        setAccess("login");
      } else {
        if (failure.recovery === "reset") setMustReset(true);
        else setRetryQuery(text);
        if (err instanceof ApiError && err.status === 410)
          setAccessAttempt((value) => value + 1);
        setErrorAction(failure.action ?? null);
        setError(failure.message);
      }
    } finally {
      busy.current = false;
      setRunning(false);
    }
  }

  return {
    messages,
    isRunning,
    isResetting,
    captureChoice,
    setCaptureChoice,
    trace,
    traceError,
    isTraceBusy,
    captureBlocked,
    hasSession,
    withdrawCapture,
    error,
    mustReset,
    clock,
    epoch,
    access,
    accessInfo,
    errorAction,
    canRetry: retryQuery !== null,
    retry: () => retryQuery && submit(retryQuery, true),
    reset,
    submit,
    recheckAccess: () => setAccessAttempt((value) => value + 1),
  };
}
