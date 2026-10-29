import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  classifySearchError,
  decodeAccess,
  decodeReply,
  decodeSession,
  request,
} from "../api";
import type { Access, Session, TurnResult } from "../api";

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
  const expiry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const timezone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Taipei";

  useEffect(
    () => () => {
      if (expiry.current) clearTimeout(expiry.current);
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

  function clearSession() {
    generation.current++;
    session.current = null;
    if (expiry.current) clearTimeout(expiry.current);
    expiry.current = null;
    setMessages([]);
    setClock("");
    setErrorAction(null);
    setRetryQuery(null);
    setEpoch((e) => e + 1);
  }

  async function reset() {
    if (busy.current) return;
    if (session.current)
      request("/api/session", undefined, session.current.token, "DELETE").catch(
        () => {},
      );
    clearSession();
    setError("");
    setErrorAction(null);
    setMustReset(false);
  }

  async function submit(text: string, retry = false) {
    if (!text || busy.current || mustReset || access !== "ready") return;
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
    let current = session.current;
    try {
      if (!current) {
        current = decodeSession(await request<unknown>("/api/session", {}));
        if (generation.current !== ownerGeneration) return;
        session.current = current;
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
