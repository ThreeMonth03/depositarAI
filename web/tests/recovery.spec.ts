import { privateAccess } from "./access-fixture";
import { expect, test } from "@playwright/test";

const session = {
  token: "recovery-test-session",
  version: 0,
  expires_in: 1800,
  max_turns: 20,
  revision: "test",
};
const reply = {
  version: 1,
  context_used: { reference_date: "2026-10-19", timezone: "Asia/Taipei" },
  result: {
    status: "ok",
    message: "搜尋完成",
    state: { topic: "河川水質", time: null, formats: ["CSV"] },
    revision: "test",
    timing_ms: { total: 20 },
    search: {
      cards: [
        {
          dataset_id: "recovery-dataset",
          title: "河川水質資料",
          url: "https://data.depositar.io/dataset/recovery-dataset",
          snippet: "公開資料說明",
          organization_title: "測試機構",
          resource_formats: ["CSV"],
          condition_status: "match",
        },
      ],
      filter_scope: "index_prefilter",
      warnings: [],
      catalog_manifest_sha256: "test-snapshot",
    },
  },
};

for (const code of ["session_required", "unavailable"])
  test(`access 401 ${code} offers a manual check rather than a login route`, async ({
    page,
  }) => {
    const calls: string[] = [];
    await page.route("**/api/**", (route) => {
      calls.push(new URL(route.request().url()).pathname);
      return calls.length === 1
        ? route.fulfill({ status: 401, json: { error: { code } } })
        : route.fulfill({
            json: privateAccess,
          });
    });
    await page.goto("/");
    await expect(page.getByRole("alert")).toContainText("暫時無法確認使用狀態");
    await expect(
      page.getByRole("link", { name: "免費登入", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeDisabled();
    expect(calls).toEqual(["/api/access"]);
    await page.getByRole("button", { name: "重新確認" }).click();
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeEnabled();
    expect(calls).toEqual(["/api/access", "/api/access"]);
  });

test("invalid successful session replies never allocate a turn or arm an invalid expiry", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let changed: Record<string, unknown> = {};
  const calls: string[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    calls.push(`${route.request().method()} ${path}`);
    return route.fulfill({
      json:
        path === "/api/access"
          ? privateAccess
          : path === "/api/session"
            ? { ...session, ...changed }
            : reply,
    });
  });
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  for (const invalid of [
    { token: null },
    { token: "" },
    { token: " " },
    { version: "0" },
    { version: -1 },
    { expires_in: null },
    { expires_in: 0 },
    { expires_in: 2 ** 31 },
    { max_turns: 0 },
    { max_turns: 1.5 },
  ]) {
    changed = invalid;
    await page.getByRole("button", { name: "新搜尋" }).click();
    calls.length = 0;
    await page.getByRole("textbox", { name: "搜尋或追問" }).fill("河川水質");
    await page.getByRole("button", { name: "送出搜尋" }).click();
    await expect(page.getByRole("alert")).toContainText("請開始新搜尋");
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeDisabled();
    await expect(page.getByRole("button", { name: "新搜尋" })).toBeEnabled();
    expect(calls).toEqual(["POST /api/session"]);
  }
  expect(errors).toEqual([]);
});

test("malformed successful turns keep prior results and require an explicit new search", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const invalidResults = [
    {
      ...reply,
      version: 2,
      result: {
        ...reply.result,
        state: { ...reply.result.state, formats: null },
      },
    },
    { ...reply, version: "2" },
    { ...reply, version: 1 },
    { ...reply, version: 2, context_used: null },
    { ...reply, version: 2, result: null },
    {
      ...reply,
      version: 2,
      result: {
        ...reply.result,
        state: { ...reply.result.state, time: { start: 2024, end: null } },
      },
    },
    {
      ...reply,
      version: 2,
      result: {
        ...reply.result,
        search: {
          ...reply.result.search,
          cards: [{ ...reply.result.search.cards[0], title: {} }],
        },
      },
    },
    { ...reply, version: 2, result: { ...reply.result, notices: [{}] } },
    {
      ...reply,
      version: 2,
      result: { ...reply.result, timing_ms: { total: "unknown" } },
    },
    { ...reply, version: 2, result: { ...reply.result, timing_ms: {} } },
    {
      ...reply,
      version: 2,
      result: {
        ...reply.result,
        search: { ...reply.result.search, warnings: {} },
      },
    },
  ];
  let invalid: unknown = null;
  let sessions = 0;
  const turns: { version: number; token: string | undefined }[] = [];
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: privateAccess,
      });
    if (path === "/api/session") {
      if (request.method() === "POST") sessions++;
      return route.fulfill({
        json: {
          ...session,
          token: `session-${sessions}`,
          extra_session_metadata: "allowed",
        },
      });
    }
    turns.push({
      version: request.postDataJSON().version,
      token: request.headers().authorization,
    });
    return route.fulfill({
      json:
        turns.length === 2
          ? invalid
          : { ...reply, extra_reply_metadata: "allowed" },
    });
  });
  await page.goto("/");
  for (const changed of invalidResults) {
    await page.getByRole("button", { name: "新搜尋" }).click();
    invalid = changed;
    sessions = 0;
    turns.length = 0;
    const ask = async () => {
      await page.getByRole("textbox", { name: "搜尋或追問" }).fill("河川水質");
      await page.getByRole("button", { name: "送出搜尋" }).click();
    };
    await ask();
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeEnabled();
    await expect(page.locator(".result-card:visible")).toHaveCount(1);
    await ask();
    await expect(page.getByRole("alert")).toContainText("請開始新搜尋");
    await expect(page.locator(".result-card:visible")).toHaveCount(1);
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeDisabled();
    expect(turns).toHaveLength(2);
    expect(sessions).toBe(1);
    await page.getByRole("button", { name: "新搜尋" }).click();
    await expect(page.locator(".result-card")).toHaveCount(0);
    await ask();
    await expect(page.locator(".result-card:visible")).toHaveCount(1);
    expect(turns).toEqual([
      { version: 0, token: "Bearer session-1" },
      { version: 1, token: "Bearer session-1" },
      { version: 0, token: "Bearer session-2" },
    ]);
    expect(errors).toEqual([]);
  }
});
