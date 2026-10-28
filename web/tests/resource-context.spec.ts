import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { ResourceContext } from "../src/api";
import { privateAccess } from "./access-fixture";

const csv: ResourceContext = {
  resource_id: "11111111-1111-4111-8111-111111111111",
  name: "河川水質.csv",
  format: "CSV",
  url: "https://data.depositar.io/dataset/water/resource/11111111-1111-4111-8111-111111111111",
  columns: ["採樣日期", "測站", "<img src=x onerror=alert(1)>"],
  excerpt: "",
};
const pdf: ResourceContext = {
  resource_id: "22222222-2222-4222-8222-222222222222",
  name: "水質報告.pdf",
  format: "PDF",
  url: "https://data.depositar.io/dataset/water/resource/22222222-2222-4222-8222-222222222222",
  columns: [],
  excerpt: "河川水質採樣摘要。<script>alert(1)</script>",
};

async function mockSearch(page: Page, context: unknown) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access") return route.fulfill({ json: privateAccess });
    if (path === "/api/session")
      return route.fulfill({
        json: {
          token: "resource-context-session",
          version: 0,
          expires_in: 1800,
          max_turns: 20,
          revision: "test",
        },
      });
    return route.fulfill({
      json: {
        version: request.postDataJSON().version + 1,
        context_used: { reference_date: "2026-10-28", timezone: "Asia/Taipei" },
        result: {
          status: "ok",
          message: "搜尋完成",
          state: { topic: "河川水質", time: null, formats: [] },
          revision: "test",
          timing_ms: { total: 10 },
          search: {
            cards: [
              {
                dataset_id: "water",
                title: "河川水質資料集",
                url: "https://data.depositar.io/dataset/water",
                snippet: "公開資料集說明",
                organization_title: "測試機構",
                resource_formats: ["CSV", "PDF"],
                temporal_coverage: { start: null, end: null, status: "absent" },
                condition_status: "unknown",
                ...(context === undefined ? {} : { resource_context: context }),
              },
            ],
            filter_scope: "none",
            warnings: [],
            catalog_manifest_sha256: "test-snapshot",
          },
        },
      },
    });
  });
}

async function ask(page: Page) {
  await page.goto("/");
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill("河川水質");
  await page.getByRole("button", { name: "送出搜尋" }).click();
}

test("CSV columns and PDF excerpts keep one dataset card and escape source text", async ({
  page,
}) => {
  await mockSearch(page, [csv, pdf]);
  await ask(page);
  const card = page.locator(".result-card");
  await expect(card).toHaveCount(1);
  await expect(card.getByRole("heading", { level: 3 })).toHaveText(
    "河川水質資料集↗",
  );
  const files = card.getByLabel("檔案內容節錄");
  const link = files.getByRole("link", { name: /河川水質.csv/ });
  await expect(link).toHaveAttribute("href", csv.url);
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(link).toHaveAttribute("target", "_blank");
  await expect(files.getByText("採樣日期", { exact: true })).toBeHidden();
  await files.getByText("CSV 欄位（節錄）", { exact: true }).click();
  await expect(files.locator("code")).toHaveText(csv.columns);
  await expect(files).toContainText(`檔案摘錄：${pdf.excerpt}`);
  await expect(card.locator("img, script")).toHaveCount(0);
  await expect(card).not.toContainText("命中理由");
});

test("metadata-only datasets keep their card without inventing file contents", async ({
  page,
}) => {
  await mockSearch(page, []);
  await ask(page);
  await expect(page.locator(".result-card")).toHaveCount(1);
  await expect(page.getByLabel("檔案內容節錄")).toHaveCount(0);
});

test("resource source URLs cannot become script or external links", async ({
  page,
}) => {
  await mockSearch(page, [
    { ...csv, url: "javascript:alert(1)" },
    { ...pdf, url: "https://untrusted.example/report.pdf" },
  ]);
  await ask(page);
  const files = page.getByLabel("檔案內容節錄");
  await expect(files).toContainText(csv.name);
  await expect(files).toContainText(pdf.name);
  await expect(files.getByRole("link")).toHaveCount(0);
});

test("bounded file context fits a narrow screen at the supported limits", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 760 });
  await mockSearch(page, [
    {
      ...csv,
      name: "檔".repeat(160),
      columns: Array(32).fill("欄".repeat(64)),
    },
    { ...pdf, excerpt: "字".repeat(320) },
    {
      ...csv,
      resource_id: "33333333-3333-4333-8333-333333333333",
      url: "https://data.depositar.io/dataset/water/resource/33333333-3333-4333-8333-333333333333",
      columns: ["第三個檔案欄位"],
    },
  ]);
  await ask(page);
  const files = page.getByLabel("檔案內容節錄");
  await expect(files.locator(".resource-context")).toHaveCount(3);
  await files.getByText("CSV 欄位（節錄）", { exact: true }).first().click();
  await expect(files.locator("code:visible")).toHaveCount(32);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/resource-context-mobile.png",
    fullPage: true,
  });
});

const invalidContexts: { name: string; context: unknown }[] = [
  { name: "missing field from wire3", context: undefined },
  { name: "null", context: null },
  { name: "more than three files", context: [csv, pdf, csv, pdf] },
  { name: "duplicate resources", context: [csv, csv] },
  { name: "unsorted resources", context: [pdf, csv] },
  {
    name: "invalid resource identity",
    context: [{ ...csv, resource_id: "bad" }],
  },
  { name: "long filename", context: [{ ...csv, name: "名".repeat(161) }] },
  {
    name: "too many columns",
    context: [{ ...csv, columns: Array(33).fill("欄") }],
  },
  { name: "long column", context: [{ ...csv, columns: ["欄".repeat(129)] }] },
  {
    name: "total column limit",
    context: [{ ...csv, columns: Array(17).fill("欄".repeat(128)) }],
  },
  { name: "long excerpt", context: [{ ...pdf, excerpt: "字".repeat(321) }] },
  { name: "CSV with prose", context: [{ ...csv, excerpt: "不是欄位" }] },
  { name: "CSV without columns", context: [{ ...csv, columns: [] }] },
  { name: "blank column", context: [{ ...csv, columns: [" "] }] },
  { name: "PDF without excerpt", context: [{ ...pdf, excerpt: " " }] },
  {
    name: "PDF with columns",
    context: [{ ...pdf, columns: ["不是 PDF 欄位"] }],
  },
];

for (const { name, context } of invalidContexts)
  test(`invalid resource context ${name} rejects the turn before committing UI state`, async ({
    page,
  }) => {
    await mockSearch(page, context);
    await ask(page);
    await expect(page.getByRole("alert")).toContainText("請開始新搜尋");
    await expect(page.locator(".result-card")).toHaveCount(0);
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeDisabled();
  });
