import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { TemporalCoverage } from "../src/api";
import { privateAccess } from "./access-fixture";

async function mockSearch(page: Page, coverage: TemporalCoverage) {
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access") return route.fulfill({ json: privateAccess });
    if (path === "/api/session")
      return route.fulfill({
        json: {
          token: "coverage-test-session",
          version: 0,
          expires_in: 1800,
          max_turns: 20,
          revision: "test",
        },
      });
    const body = request.postDataJSON();
    const year = body.query.includes("2023") ? "2023" : "2024";
    return route.fulfill({
      json: {
        version: body.version + 1,
        context_used: { reference_date: "2026-10-21", timezone: "Asia/Taipei" },
        result: {
          status: "ok",
          message: "搜尋完成",
          state: {
            topic: "氣候",
            time: { start: year, end: year },
            formats: [],
          },
          reason: "none",
          revision: "test",
          timing_ms: { total: 10 },
          search: {
            cards: [
              {
                dataset_id: "coverage-fixture",
                title: "氣候資料",
                url: "https://data.depositar.io/dataset/coverage-fixture",
                snippet: "資料集原始說明",
                organization_title: "測試資料機構",
                resource_formats: ["CSV", "<img src=x onerror=alert(1)>"],
                temporal_coverage: coverage,
                metadata_modified: "2020-01-01",
                condition_status: "unknown",
              },
            ],
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

const cases: { name: string; coverage: TemporalCoverage; label: string }[] = [
  {
    name: "year range",
    coverage: { start: "2020", end: "2025", status: "complete" },
    label: "資料涵蓋：2020～2025 年",
  },
  {
    name: "same year",
    coverage: { start: "2024", end: "2024", status: "complete" },
    label: "資料涵蓋：2024 年",
  },
  {
    name: "same month",
    coverage: { start: "2024-03", end: "2024-03", status: "complete" },
    label: "資料涵蓋：2024 年 3 月",
  },
  {
    name: "day precision",
    coverage: { start: "2024-03-01", end: "2024-03-15", status: "complete" },
    label: "資料涵蓋：2024 年 3 月 1 日～2024 年 3 月 15 日",
  },
  {
    name: "missing end does not mean ongoing",
    coverage: { start: "2020", end: null, status: "partial" },
    label: "資料涵蓋：2020 年～結束未提供",
  },
  {
    name: "missing start",
    coverage: { start: null, end: "2025-02", status: "partial" },
    label: "資料涵蓋：起始未提供～2025 年 2 月",
  },
  {
    name: "missing coverage does not use the modification date",
    coverage: { start: null, end: null, status: "absent" },
    label: "資料涵蓋：未提供",
  },
  {
    name: "invalid dates do not become an asserted interval",
    coverage: {
      start: "<img src=x onerror=alert(1)>",
      end: null,
      status: "invalid",
    },
    label: "資料涵蓋：日期資訊無法判讀",
  },
];

for (const { name, coverage, label } of cases)
  test(`dataset tags show ${name} from metadata`, async ({ page }) => {
    await mockSearch(page, coverage);
    await page.goto("/");
    await ask(page, "找2024年的氣候資料");
    const card = page.locator(".result-card").last();
    await expect(card.getByLabel("資料集資訊").locator("span")).toHaveText([
      label,
      "CSV",
      "<img src=x onerror=alert(1)>",
    ]);
    await expect(card).toContainText("測試資料機構");
    await expect(card.locator("img, script")).toHaveCount(0);
  });

test("changing the requested year never rewrites the dataset coverage", async ({
  page,
}) => {
  await mockSearch(page, { start: "2020", end: "2025", status: "complete" });
  await page.goto("/");
  await ask(page, "找2024年的氣候資料");
  await ask(page, "改查2023年");
  const latest = page.locator(".results").last();
  await expect(latest.locator(".chips")).toContainText("2023");
  await expect(latest.getByLabel("資料集資訊")).toContainText(
    "資料涵蓋：2020～2025 年",
  );
  await expect(latest.getByLabel("資料集資訊")).not.toContainText("2023");
});
