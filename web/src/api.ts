export type SearchState = {
  topic: string;
  time: { start: string | null; end: string | null } | null;
  formats: string[];
  unapplied_time?: { calendar: "lunar"; text: string } | null;
};
export type Card = {
  dataset_id: string;
  title: string;
  url: string;
  snippet: string;
  organization_title: string;
  resource_formats: string[];
  condition_status: "match" | "unknown" | "not_checked";
};
export type TurnResult = {
  status: "ok" | "rejected" | "clarification_required" | "unavailable";
  message: string;
  state: SearchState;
  reason: string;
  revision: string;
  timing_ms: Record<string, number>;
  notices?: string[];
  search: null | {
    cards: Card[];
    filter_scope: "none" | "index_prefilter";
    warnings: string[];
    catalog_manifest_sha256: string;
  };
};
export type Session = {
  token: string;
  version: number;
  expires_in: number;
  max_turns: number;
  revision: string;
};
export type Access = {
  auth_required: boolean;
  authenticated: boolean;
};
export type Reply = {
  version: number;
  result: TurnResult;
  context_used: { reference_date: string; timezone: string };
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function strings(value: unknown): value is string[] {
  return (
    Array.isArray(value) && value.every((item) => typeof item === "string")
  );
}

function time(value: unknown): boolean {
  return (
    value === null ||
    (record(value) &&
      [value.start, value.end].every(
        (date) => date === null || typeof date === "string",
      ))
  );
}

function state(value: unknown): boolean {
  return (
    record(value) &&
    typeof value.topic === "string" &&
    time(value.time) &&
    strings(value.formats) &&
    (value.unapplied_time == null ||
      (record(value.unapplied_time) &&
        value.unapplied_time.calendar === "lunar" &&
        typeof value.unapplied_time.text === "string"))
  );
}

function card(value: unknown): boolean {
  return (
    record(value) &&
    ["dataset_id", "title", "url", "snippet", "organization_title"].every(
      (field) => typeof value[field] === "string",
    ) &&
    strings(value.resource_formats) &&
    ["match", "unknown", "not_checked"].includes(
      value.condition_status as string,
    )
  );
}

function result(value: unknown): boolean {
  return (
    record(value) &&
    ["ok", "rejected", "clarification_required", "unavailable"].includes(
      value.status as string,
    ) &&
    typeof value.message === "string" &&
    typeof value.revision === "string" &&
    state(value.state) &&
    record(value.timing_ms) &&
    Object.values(value.timing_ms).every(
      (timing) => typeof timing === "number" && Number.isFinite(timing),
    ) &&
    (value.notices === undefined || strings(value.notices)) &&
    (value.search === null ||
      (record(value.search) &&
        typeof value.timing_ms.total === "number" &&
        Number.isFinite(value.timing_ms.total) &&
        Array.isArray(value.search.cards) &&
        value.search.cards.every(card) &&
        strings(value.search.warnings) &&
        typeof value.search.catalog_manifest_sha256 === "string"))
  );
}

// Check the values used by the UI before committing session state. Extra fields
// are intentionally accepted; this is not a copy of the backend's full schema.
export function decodeSession(value: unknown): Session {
  if (
    !record(value) ||
    typeof value.token !== "string" ||
    !/^[A-Za-z0-9_-]+$/.test(value.token) ||
    value.version !== 0 ||
    typeof value.revision !== "string" ||
    typeof value.expires_in !== "number" ||
    !Number.isFinite(value.expires_in) ||
    value.expires_in <= 0 ||
    value.expires_in * 1000 > 2_147_483_647 ||
    typeof value.max_turns !== "number" ||
    !Number.isSafeInteger(value.max_turns) ||
    value.max_turns < 1
  )
    throw new Error("invalid_session_response");
  return value as Session;
}

export function decodeReply(value: unknown, session: Session): Reply {
  if (
    !record(value) ||
    value.version !== session.version + 1 ||
    value.version > session.max_turns ||
    !record(value.context_used) ||
    typeof value.context_used.reference_date !== "string" ||
    typeof value.context_used.timezone !== "string" ||
    !result(value.result)
  )
    throw new Error("invalid_turn_response");
  return value as Reply;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
  ) {
    super(code);
  }
}

export type SearchErrorPolicy =
  { recovery: "login" } | { recovery: "reset" | "retry"; message: string };

// Recovery describes the user's next step; requests are never retried here.
export function classifySearchError(error: unknown): SearchErrorPolicy {
  if (error instanceof ApiError) {
    if (error.status === 401 && error.code === "authentication_required")
      return { recovery: "login" };

    switch (error.status) {
      case 401:
      case 409:
      case 410:
        return {
          recovery: "reset",
          message: "搜尋已到期或狀態不同步，請開始新搜尋。",
        };
      case 429:
        if (["quota_exceeded", "global_quota_exceeded"].includes(error.code))
          return {
            recovery: "retry",
            message:
              "目前 AI 搜尋額度已用完，請稍後再試。你仍可探索 depositar 的公開資料。",
          };
        if (error.code === "session_capacity")
          return {
            recovery: "retry",
            message: "目前搜尋連線已滿，請稍後再試。",
          };
        if (error.code === "rate_limited")
          return {
            recovery: "retry",
            message: "送出搜尋的頻率較高，請稍候再試。",
          };
        return {
          recovery: "retry",
          message: "服務正在處理其他搜尋。這次沒有修改條件，請稍後再送出。",
        };
      case 422:
        return {
          recovery: "retry",
          message: "輸入格式無法處理，請調整問題後再試。",
        };
      case 503:
        return {
          recovery: "retry",
          message: "搜尋服務暫時無法使用。條件未修改，請稍後再試。",
        };
    }
  }
  return {
    recovery: "reset",
    message: "連線中斷，無法確認本輪狀態。請開始新搜尋，避免沿用錯誤條件。",
  };
}

export async function request<T>(
  path: string,
  body?: unknown,
  token?: string,
  method = "POST",
  timeoutMs = 135_000,
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw new ApiError(response.status, value?.error?.code ?? "unavailable");
  }
  return response.json() as Promise<T>;
}

export function safeDatasetUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (
      url.protocol === "https:" &&
      url.hostname === "data.depositar.io" &&
      !url.username &&
      !url.password &&
      !url.port &&
      url.pathname.startsWith("/dataset/")
    )
      return url.href;
  } catch {
    /* invalid data is displayed as text only */
  }
  return undefined;
}
