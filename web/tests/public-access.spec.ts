import { guestAccess, privateAccess, userAccess } from "./access-fixture";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

const session = {
  token: "test-search-session-token",
  version: 0,
  expires_in: 1800,
  max_turns: 20,
  revision: "test",
};
const reply = {
  version: 1,
  context_used: { reference_date: "2026-10-14", timezone: "Asia/Taipei" },
  result: {
    status: "rejected",
    message: "測試搜尋回覆",
    state: { topic: "", time: null, formats: [] },
    reason: "test",
    revision: "test",
    timing_ms: {},
    search: null,
  },
};

async function submit(page: Page) {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill("河川水質");
  await page.getByRole("button", { name: "送出搜尋" }).click();
}

test("login-required mode keeps the anonymous homepage readable without allocating a search session", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    calls.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      status: 401,
      json: { error: { code: "authentication_required" } },
    });
  });
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "免費登入後開始搜尋" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "免費登入", exact: true }),
  ).toHaveAttribute("href", "/auth/login");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "找河川水質觀測資料" }),
  ).toBeDisabled();
  await page.locator(".usage-notes summary").click();
  await expect(page.locator(".usage-notes")).toContainText("30 分鐘");
  expect(calls).toEqual(["/api/access"]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("authenticated public requests send the same-origin login cookie and keep the search token separate", async ({
  page,
  context,
}) => {
  await context.addCookies([
    {
      name: "login_session",
      value: "verified-cookie",
      url: "http://127.0.0.1:8080",
    },
  ]);
  const calls: { path: string; headers: Record<string, string> }[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    calls.push({ path, headers: route.request().headers() });
    return route.fulfill({
      json:
        path === "/api/access"
          ? userAccess
          : path === "/api/session"
            ? session
            : reply,
    });
  });
  await page.goto("/");
  await expect(
    page.getByRole("link", { name: "登出", exact: true }),
  ).toHaveAttribute("href", "/auth/logout");
  expect(calls.map((call) => call.path)).toEqual(["/api/access"]);
  await submit(page);
  await expect(page.locator(".assistant")).toContainText("測試搜尋回覆");
  expect(calls.map((call) => call.path)).toEqual([
    "/api/access",
    "/api/session",
    "/api/turn",
  ]);
  for (const call of calls)
    expect(call.headers.cookie).toContain("login_session=verified-cookie");
  expect(calls[0].headers.authorization).toBeUndefined();
  expect(calls[1].headers.authorization).toBeUndefined();
  expect(calls[2].headers.authorization).toBe(`Bearer ${session.token}`);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("private mode keeps the existing search flow without login or eager session creation", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    calls.push(path);
    return route.fulfill({
      json:
        path === "/api/access"
          ? privateAccess
          : path === "/api/session"
            ? session
            : reply,
    });
  });
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  await expect(
    page.getByRole("link", { name: "免費登入", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "登出", exact: true }),
  ).toHaveCount(0);
  expect(calls).toEqual(["/api/access"]);
  await submit(page);
  await expect(page.locator(".assistant")).toContainText("測試搜尋回覆");
  expect(calls).toEqual(["/api/access", "/api/session", "/api/turn"]);
});

test("expired public login clears the search and asks for login without retrying the model request", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    calls.push(path);
    if (path === "/api/turn")
      return route.fulfill({
        status: 401,
        json: { error: { code: "authentication_required" } },
      });
    return route.fulfill({
      json: path === "/api/access" ? userAccess : session,
    });
  });
  await page.goto("/");
  await submit(page);
  await expect(
    page.getByRole("link", { name: "免費登入", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".message")).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  expect(calls).toEqual(["/api/access", "/api/session", "/api/turn"]);
});

test("a lost search session requires reset without offering a login route in private mode", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/turn")
      return route.fulfill({
        status: 401,
        json: { error: { code: "session_required" } },
      });
    return route.fulfill({
      json: path === "/api/access" ? privateAccess : session,
    });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.getByRole("alert")).toContainText("請開始新搜尋");
  await expect(
    page.getByRole("link", { name: "免費登入", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
});

for (const [code, message] of [
  ["quota_exceeded", "AI 搜尋額度已用完"],
  ["session_capacity", "搜尋連線已滿"],
  ["rate_limited", "頻率較高"],
  ["busy", "服務正在處理其他搜尋"],
] as const)
  test(`${code} is explained without automatic retries or allocating a session`, async ({
    page,
  }) => {
    const calls: string[] = [];
    await page.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      calls.push(path);
      return path === "/api/access"
        ? route.fulfill({ json: userAccess })
        : route.fulfill({ status: 429, json: { error: { code } } });
    });
    await page.goto("/");
    await submit(page);
    await expect(page.getByRole("alert")).toContainText(message);
    await expect(page.getByRole("button", { name: "新搜尋" })).toBeEnabled();
    expect(calls).toEqual(["/api/access", "/api/session"]);
  });

test("access service failure keeps search disabled and offers a bounded manual check", async ({
  page,
}) => {
  let checks = 0;
  await page.route("**/api/**", (route) => {
    expect(new URL(route.request().url()).pathname).toBe("/api/access");
    checks++;
    return checks === 1
      ? route.fulfill({ status: 503, json: { error: { code: "unavailable" } } })
      : route.fulfill({ json: privateAccess });
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("暫時無法確認使用狀態");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  expect(checks).toBe(1);
  await page.getByRole("button", { name: "重新確認" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  expect(checks).toBe(2);
});

test("invalid input and unavailable service allow manual retry with the same search session and version", async ({
  page,
}) => {
  let sessions = 0;
  const turns: { authorization: string; version: number }[] = [];
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: privateAccess,
      });
    if (path === "/api/session") {
      sessions++;
      return route.fulfill({ json: session });
    }
    turns.push({
      authorization: request.headers().authorization,
      version: request.postDataJSON().version,
    });
    return turns.length < 3
      ? route.fulfill({
          status: turns.length === 1 ? 422 : 503,
          json: { error: { code: "unavailable" } },
        })
      : route.fulfill({ json: reply });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.getByRole("alert")).toContainText("輸入格式無法處理");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  expect(turns).toHaveLength(1);
  await submit(page);
  await expect(page.getByRole("alert")).toContainText("搜尋服務暫時無法使用");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  expect(turns).toHaveLength(2);
  await submit(page);
  await expect(page.locator(".assistant")).toContainText("測試搜尋回覆");
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(sessions).toBe(1);
  expect(turns).toEqual(
    Array.from({ length: 3 }, () => ({
      authorization: `Bearer ${session.token}`,
      version: 0,
    })),
  );
});

test("an interrupted turn requires a new search before another model request", async ({
  page,
}) => {
  let sessions = 0;
  let turns = 0;
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: privateAccess,
      });
    if (path === "/api/session") {
      if (request.method() === "POST") sessions++;
      return route.fulfill({ json: session });
    }
    turns++;
    return turns === 1
      ? route.abort("connectionreset")
      : route.fulfill({ json: reply });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.getByRole("alert")).toContainText("無法確認本輪狀態");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  expect(turns).toBe(1);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  await submit(page);
  await expect(page.locator(".assistant")).toContainText("測試搜尋回覆");
  expect(sessions).toBe(2);
  expect(turns).toBe(2);
});

const searchReply = {
  ...reply,
  result: {
    ...reply.result,
    status: "ok",
    state: { topic: "河川水質", time: null, formats: ["CSV"] },
    timing_ms: { total: 25 },
    search: {
      cards: [
        {
          dataset_id: "public-water",
          title: "河川水質公開資料",
          url: "https://data.depositar.io/dataset/public-water",
          snippet: "河川觀測資料",
          organization_title: "測試資料機構",
          resource_formats: ["CSV"],
          resource_context: [],
          temporal_coverage: { start: null, end: null, status: "absent" },
          condition_status: "match",
        },
      ],
      filter_scope: "index_prefilter",
      warnings: [],
      catalog_manifest_sha256: "test",
    },
  },
};

test("mixed mode lets guests search immediately and describes allowance without inventing remaining quota", async ({
  page,
}) => {
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    calls.push(path);
    return route.fulfill({
      json:
        path === "/api/access"
          ? guestAccess
          : path === "/api/session"
            ? session
            : searchReply,
    });
  });
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  await expect(page.locator(".access-summary")).toContainText("訪客試用");
  await expect(page.locator(".access-summary")).toContainText(
    "每 24 小時最多 3 次搜尋",
  );
  await expect(page.locator(".access-summary")).not.toContainText("剩餘");
  await expect(
    page.getByRole("link", { name: "免費登入，取得較多額度" }),
  ).toHaveAttribute("href", "/auth/login");
  expect(calls).toEqual(["/api/access"]);
  await submit(page);
  await expect(page.locator(".result-card")).toHaveCount(1);
  expect(calls).toEqual(["/api/access", "/api/session", "/api/turn"]);
  await page.screenshot({
    path: "test-results/mixed-guest-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

for (const [code, principal, message, login] of [
  ["guest_quota_exceeded", guestAccess, "訪客搜尋額度已用完", true],
  ["user_quota_exceeded", userAccess, "你的 AI 搜尋額度已用完", false],
  ["pool_quota_exceeded", guestAccess, "共用搜尋額度已用完", false],
  ["global_quota_exceeded", guestAccess, "全站搜尋額度已用完", false],
] as const)
  test(`${code} preserves results and retries only the failed turn when requested`, async ({
    page,
  }) => {
    const turns: {
      query: string;
      version: number;
      token: string | undefined;
    }[] = [];
    let sessions = 0;
    await page.route("**/api/**", (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (path === "/api/access") return route.fulfill({ json: principal });
      if (path === "/api/session") {
        sessions++;
        return route.fulfill({ json: session });
      }
      const body = request.postDataJSON();
      turns.push({
        query: body.query,
        version: body.version,
        token: request.headers().authorization,
      });
      if (turns.length === 2)
        return route.fulfill({
          status: 429,
          headers: { "Retry-After": "120", "X-Pool-RateLimit-Reset": "3600" },
          json: { error: { code } },
        });
      return route.fulfill({
        json: { ...searchReply, version: turns.length === 1 ? 1 : 2 },
      });
    });
    await page.goto("/");
    await submit(page);
    await expect(page.locator(".result-card:visible")).toHaveCount(1);
    await page
      .getByRole("textbox", { name: "搜尋或追問" })
      .fill("改成 2024 年");
    await page.getByRole("button", { name: "送出搜尋" }).click();
    const alert = page.getByRole("alert");
    await expect(alert).toContainText(message);
    await expect(alert).toContainText("約 2 分鐘後再試");
    await expect(
      alert.getByRole("link", { name: "免費登入，取得較多額度" }),
    ).toHaveCount(login ? 1 : 0);
    await expect(page.locator(".result-card:visible")).toHaveCount(1);
    await expect(page.locator(".chip.topic")).toContainText("河川水質");
    expect(turns).toHaveLength(2);
    await alert.getByRole("button", { name: "重試這次搜尋" }).click();
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(page.locator(".assistant")).toHaveCount(2);
    await expect(page.locator(".message.user")).toHaveCount(2);
    expect(sessions).toBe(1);
    expect(turns).toEqual([
      { query: "河川水質", version: 0, token: `Bearer ${session.token}` },
      { query: "改成 2024 年", version: 1, token: `Bearer ${session.token}` },
      { query: "改成 2024 年", version: 1, token: `Bearer ${session.token}` },
    ]);
  });

test("browser verification uses a separate tab and preserves the failed query until manual retry", async ({
  page,
  context,
}) => {
  const turns: { query: string; version: number; cookie: string }[] = [];
  await context.route("**/verify", (route) =>
    route.fulfill({
      contentType: "text/html; charset=utf-8",
      headers: { "Set-Cookie": "test_challenge=passed; Path=/; SameSite=Lax" },
      body: "<html><body>瀏覽器驗證完成，請回到搜尋頁。</body></html>",
    }),
  );
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access") return route.fulfill({ json: guestAccess });
    if (path === "/api/session") return route.fulfill({ json: session });
    const body = request.postDataJSON();
    const cookie = request.headers().cookie ?? "";
    turns.push({ query: body.query, version: body.version, cookie });
    return turns.length > 1 && !cookie.includes("test_challenge=passed")
      ? route.fulfill({
          status: 403,
          json: { error: { code: "challenge_required" } },
        })
      : route.fulfill({
          json: { ...searchReply, version: turns.length === 1 ? 1 : 2 },
        });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.locator(".result-card:visible")).toHaveCount(1);
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill("只要 CSV");
  await page.getByRole("button", { name: "送出搜尋" }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("完成瀏覽器驗證");
  const verify = alert.getByRole("link", { name: "完成瀏覽器驗證" });
  await expect(verify).toHaveAttribute("href", "/verify");
  await expect(verify).toHaveAttribute("rel", "noopener noreferrer");
  const popupPromise = context.waitForEvent("page");
  await verify.click();
  const popup = await popupPromise;
  await expect(popup.getByText("瀏覽器驗證完成，請回到搜尋頁。")).toBeVisible();
  await popup.close();
  expect(turns).toHaveLength(2);
  await expect(page.locator(".result-card:visible")).toHaveCount(1);
  await alert.getByRole("button", { name: "重試這次搜尋" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(turns).toHaveLength(3);
  expect(turns[2]).toEqual({
    query: "只要 CSV",
    version: 1,
    cookie: "test_challenge=passed",
  });
});

test("identity changes discard the old conversation and use a fresh session", async ({
  page,
}) => {
  let sessions = 0;
  const turns: { query: string; version: number; token: string | undefined }[] =
    [];
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: turns.length < 2 ? guestAccess : userAccess,
      });
    if (path === "/api/session")
      return route.fulfill({
        json: { ...session, token: `identity-session-${++sessions}` },
      });
    const body = request.postDataJSON();
    turns.push({
      query: body.query,
      version: body.version,
      token: request.headers().authorization,
    });
    return turns.length === 2
      ? route.fulfill({
          status: 410,
          json: { error: { code: "session_expired" } },
        })
      : route.fulfill({ json: searchReply });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.locator(".result-card")).toHaveCount(1);
  await submit(page);
  await expect(
    page.getByRole("link", { name: "登出", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".message")).toHaveCount(0);
  await expect(page.locator(".result-card")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("使用身分已變更");
  await submit(page);
  await expect(page.locator(".result-card")).toHaveCount(1);
  expect(turns.map((turn) => [turn.token, turn.version])).toEqual([
    ["Bearer identity-session-1", 0],
    ["Bearer identity-session-1", 1],
    ["Bearer identity-session-2", 0],
  ]);
});

test("malformed access and external navigation URLs never enable search", async ({
  page,
}) => {
  let value: unknown = { auth_required: false, authenticated: false };
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    calls.push(new URL(route.request().url()).pathname);
    return route.fulfill({ json: value });
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("暫時無法確認使用狀態");
  value = { ...guestAccess, login_url: "https://attacker.example/login" };
  await page.getByRole("button", { name: "重新確認" }).click();
  await expect(page.getByRole("alert")).toContainText("暫時無法確認使用狀態");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  await expect(page.locator('a[href*="attacker.example"]')).toHaveCount(0);
  expect(calls).toEqual(["/api/access", "/api/access"]);
});

test("an initial browser challenge offers verification and only checks access when requested", async ({
  page,
}) => {
  let checks = 0;
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    calls.push(new URL(route.request().url()).pathname);
    checks++;
    return checks === 1
      ? route.fulfill({
          status: 403,
          json: { error: { code: "challenge_required" } },
        })
      : route.fulfill({ json: guestAccess });
  });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "完成瀏覽器驗證後開始搜尋" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "完成瀏覽器驗證" }),
  ).toHaveAttribute("href", "/verify");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  expect(calls).toEqual(["/api/access"]);
  await page.getByRole("button", { name: "驗證後重新確認" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  expect(calls).toEqual(["/api/access", "/api/access"]);
});

test("multiple reset headers are treated as hints and never trigger automatic retries", async ({
  page,
}) => {
  let sessions = 0;
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/access") return route.fulfill({ json: guestAccess });
    sessions++;
    return route.fulfill({
      status: 429,
      headers: {
        "X-Guest-RateLimit-Reset": "20",
        "X-Global-RateLimit-Reset": "120",
      },
      json: { error: { code: "rate_limited" } },
    });
  });
  await page.goto("/");
  await submit(page);
  await expect(page.getByRole("alert")).toContainText("約 2 分鐘後再試");
  expect(sessions).toBe(1);
});

for (const [retryAfter, hint] of [
  [120, "約 2 分鐘後再試"],
  [-1, "約 10 分鐘後再試"],
  ["120", "約 10 分鐘後再試"],
] as const)
  test(`quota retry hint validates structured retry_after_seconds ${JSON.stringify(retryAfter)}`, async ({
    page,
  }) => {
    let sessions = 0;
    await page.route("**/api/**", (route) => {
      if (new URL(route.request().url()).pathname === "/api/access")
        return route.fulfill({ json: userAccess });
      sessions++;
      return route.fulfill({
        status: 429,
        headers: { "X-Global-RateLimit-Reset": "600" },
        json: {
          error: {
            code: "user_quota_exceeded",
            retry_after_seconds: retryAfter,
          },
        },
      });
    });
    await page.goto("/");
    await submit(page);
    await expect(page.getByRole("alert")).toContainText(hint);
    expect(sessions).toBe(1);
  });
