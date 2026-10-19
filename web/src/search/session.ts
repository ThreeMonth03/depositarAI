import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  classifySearchError,
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
    "checking" | "ready" | "login" | "unavailable"
  >("checking");
  const [authRequired, setAuthRequired] = useState(false);
  const [accessAttempt, setAccessAttempt] = useState(0);
  const session = useRef<Session | null>(null);
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
    request<Access>("/api/access", undefined, undefined, "GET", 10_000)
      .then((value) => {
        if (!active) return;
        if (
          typeof value.auth_required !== "boolean" ||
          typeof value.authenticated !== "boolean"
        )
          throw new Error("invalid_access_response");
        setAuthRequired(value.auth_required);
        setAccess(
          value.auth_required && !value.authenticated ? "login" : "ready",
        );
      })
      .catch((err) => {
        if (!active) return;
        if (
          err instanceof ApiError &&
          err.status === 401 &&
          err.code === "authentication_required"
        ) {
          setAuthRequired(true);
          setAccess("login");
        } else setAccess("unavailable");
      });
    return () => {
      active = false;
    };
  }, [accessAttempt]);

  function clearSession() {
    session.current = null;
    if (expiry.current) clearTimeout(expiry.current);
    expiry.current = null;
    setMessages([]);
    setClock("");
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
    setMustReset(false);
  }

  async function submit(text: string) {
    if (!text || busy.current || mustReset || access !== "ready") return;
    if ([...text].length > 512) {
      setError("每次請輸入 512 個字以內。");
      return;
    }
    busy.current = true;
    setRunning(true);
    setError("");
    setMessages((prev) => [
      ...prev,
      { id: crypto.randomUUID(), role: "user", text },
    ]);
    let current = session.current;
    try {
      if (!current) {
        current = decodeSession(await request<unknown>("/api/session", {}));
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
      if (session.current !== current) return; // reset/expiry must not revive a session
      current.version = reply.version;
      setClock(
        `${reply.context_used.reference_date} · ${reply.context_used.timezone}`,
      );
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
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
      if (session.current !== current) return;
      const failure = classifySearchError(err);
      if (failure.recovery === "login") {
        clearSession();
        setMustReset(false);
        setAuthRequired(true);
        setAccess("login");
      } else {
        if (failure.recovery === "reset") setMustReset(true);
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
    authRequired,
    reset,
    submit,
    recheckAccess: () => setAccessAttempt((value) => value + 1),
  };
}
