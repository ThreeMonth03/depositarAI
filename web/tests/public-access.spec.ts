import { expect, test } from "./gateway";
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

test("public anonymous homepage stays readable and never allocates a search session", async ({
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
          ? { auth_required: true, authenticated: true }
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
          ? { auth_required: false, authenticated: false }
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
      json:
        path === "/api/access"
          ? { auth_required: true, authenticated: true }
          : session,
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
      json:
        path === "/api/access"
          ? { auth_required: false, authenticated: false }
          : session,
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
        ? route.fulfill({ json: { auth_required: true, authenticated: true } })
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
      : route.fulfill({ json: { auth_required: false, authenticated: false } });
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
        json: { auth_required: false, authenticated: false },
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
        json: { auth_required: false, authenticated: false },
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
