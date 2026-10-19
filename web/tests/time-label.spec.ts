import { expect, test } from "./gateway";
import type { Page } from "@playwright/test";

type Time = { start: string | null; end: string | null } | null;

async function mockSearch(page: Page, times: Time[]) {
  let turn = 0;
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: { auth_required: false, authenticated: false },
      });
    if (path === "/api/session")
      return route.fulfill({
        json: {
          token: "time-label-test-session",
          version: 0,
          expires_in: 1800,
          max_turns: 20,
          revision: "test",
        },
      });
    if (turn >= times.length) throw new Error("unexpected search request");
    return route.fulfill({
      json: {
        version: request.postDataJSON().version + 1,
        context_used: { reference_date: "2026-10-14", timezone: "Asia/Taipei" },
        result: {
          status: "ok",
          message: "搜尋完成",
          state: { topic: "河川水質", time: times[turn++], formats: ["CSV"] },
          reason: "none",
          revision: "test",
          timing_ms: { total: 10 },
          notices: [],
          search: {
            cards: [],
            filter_scope: "index_prefilter",
            warnings: [],
            catalog_manifest_sha256: "test-snapshot",
          },
        },
      },
    });
  });
}

async function ask(page: Page, query: string) {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(query);
  await page.getByRole("button", { name: "送出搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
}

test("equal year, month and day endpoints display once at their original precision", async ({
  page,
}) => {
  const cases = [
    {
      time: { start: "2024", end: "2024" },
      query: "找2024年的河川水質",
      label: "2024 年",
    },
    {
      time: { start: "2024-03", end: "2024-03" },
      query: "改查2024年3月",
      label: "2024 年 3 月",
    },
    {
      time: { start: "2024-03-15", end: "2024-03-15" },
      query: "改查2024年3月15日",
      label: "2024 年 3 月 15 日",
    },
    {
      time: { start: "2024-3", end: "2024-03" },
      query: "只查同一個3月",
      label: "2024 年 3 月",
    },
    {
      time: { start: "2024-03", end: "2024-3" },
      query: "同一個3月的資料",
      label: "2024 年 3 月",
    },
    {
      time: { start: "2024-3-1", end: "2024-03-01" },
      query: "只查3月1日",
      label: "2024 年 3 月 1 日",
    },
    {
      time: { start: "2024-03-01", end: "2024-3-1" },
      query: "同一個3月1日的資料",
      label: "2024 年 3 月 1 日",
    },
  ];
  await mockSearch(
    page,
    cases.map(({ time }) => time),
  );
  await page.goto("/");
  for (const { query, label } of cases) {
    await ask(page, query);
    const chips = page.locator(".results").last().locator(".chip");
    await expect(chips).toHaveText(["河川水質", label, "CSV"]);
    await expect(chips).not.toContainText(["→"]);
  }
  await page.locator(".previous-results summary").first().click();
  await expect(page.locator(".results").first().locator(".chip")).toHaveText([
    "河川水質",
    "2024 年",
    "CSV",
  ]);
  await expect(page.getByRole("note", { name: "時間條件變更" })).toHaveCount(0);
});

test("year ranges, mixed precision and open endpoints retain their meaning", async ({
  page,
}) => {
  await mockSearch(page, [
    { start: "2020", end: "2024" },
    { start: "2024-03", end: "2024-12" },
    { start: "2024-03", end: "2025-01-15" },
    { start: "2024", end: "2024-01-01" },
    { start: "2024-03", end: "2024-03-01" },
    { start: "2024", end: null },
    { start: null, end: "2024" },
    null,
  ]);
  await page.goto("/");
  for (const [query, label] of [
    ["找2020到2024年的河川水質", "2020～2024 年"],
    ["改查2024年3月到12月", "2024 年 3 月～2024 年 12 月"],
    ["改查2024年3月到2025年1月15日", "2024 年 3 月～2025 年 1 月 15 日"],
    ["改查2024年到1月1日", "2024 年～2024 年 1 月 1 日"],
    ["改查2024年3月到3月1日", "2024 年 3 月～2024 年 3 月 1 日"],
    ["改查2024年以後", "2024 年起"],
    ["改查截至2024年的資料", "截至 2024 年"],
  ]) {
    await ask(page, query);
    await expect(page.locator(".results").last().locator(".chip")).toHaveText([
      "河川水質",
      label,
      "CSV",
    ]);
  }
  await ask(page, "取消時間限制");
  await expect(page.locator(".results").last().locator(".chip")).toHaveText([
    "河川水質",
    "CSV",
  ]);
});

test("a full date range fits narrow mobile screens without an extra change log", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await mockSearch(page, [{ start: "2020-01-01", end: "2024-12-31" }]);
  await page.goto("/");
  await ask(page, "找2020年1月1日到2024年12月31日的河川水質");
  await expect(page.locator(".chip")).toHaveText([
    "河川水質",
    "2020 年 1 月 1 日～2024 年 12 月 31 日",
    "CSV",
  ]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await expect(page.getByRole("note", { name: "時間條件變更" })).toHaveCount(0);
});
