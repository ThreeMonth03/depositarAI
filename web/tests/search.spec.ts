import { expect, test } from "@playwright/test";

async function ask(page: import("@playwright/test").Page, query: string) {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(query);
  await page.getByRole("button", { name: "送出搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
}

test("search → format → year, reset; real website and private API HTTP", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "從一句話開始",
  );
  await page.screenshot({
    path: "test-results/desktop-home.png",
    fullPage: true,
  });
  const firstReply = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/turn") &&
      response.request().method() === "POST",
  );
  await ask(page, "找去年的氣候資料");
  const firstBody = await (await firstReply).json();
  const lastYear = String(
    Number(firstBody.context_used.reference_date.slice(0, 4)) - 1,
  );
  await expect(page.locator(".results")).toHaveCount(1);
  await expect(page.locator(".chip").filter({ hasText: lastYear })).toHaveCount(
    1,
  );
  await expect(page.locator(".result-card")).toHaveCount(5);
  await ask(page, "只要 CSV");
  await expect(
    page.locator(".results").last().locator(".chip").filter({ hasText: "CSV" }),
  ).toHaveCount(1);
  await ask(page, "改成 2024 年");
  await expect(
    page
      .locator(".results")
      .last()
      .locator(".chip")
      .filter({ hasText: "2024" }),
  ).toHaveCount(1);
  await expect(page.locator(".results").last()).toContainText("CSV");
  await expect(page.locator(".previous-results")).toHaveCount(2);
  await expect(page.locator(".result-card:visible")).toHaveCount(5);
  await page.screenshot({
    path: "test-results/desktop-results.png",
    fullPage: true,
  });
  const links = page.locator(".result-card h3 a");
  await expect(links.first()).toHaveAttribute(
    "href",
    /^https:\/\/data\.depositar\.io\/dataset\//,
  );
  await expect(links.first()).toHaveAttribute("rel", "noopener noreferrer");
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.locator(".result-card")).toHaveCount(0);
  await expect(page.locator(".message")).toHaveCount(0);
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
  expect(errors).toEqual([]);
});

test("depositar AI branding, quieter defaults and discoverable search limitations", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page).toHaveTitle("depositar AI");
  await expect(
    page.getByRole("link", { name: "depositar AI 首頁" }),
  ).toBeVisible();
  await expect(page.getByText("私有試用", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/不建立永久對話記憶/)).toBeHidden();
  await ask(page, "氣候");
  await expect(page.locator(".unknown").first()).toBeVisible();
  const details = page.locator(".diagnostics").last();
  await expect(details.getByText(/metadata/)).toBeHidden();
  await details.getByText("搜尋詳情", { exact: true }).click();
  await expect(details.getByText(/尚未讀取 PDF／CSV 內文/)).toBeVisible();
  await page.locator(".usage-notes summary").click();
  await expect(page.locator(".usage-notes")).toContainText("30 分鐘");
});

test("waiting shows actual elapsed time, not fabricated stream stages", async ({
  page,
}) => {
  await page.goto("/");
  let release = () => {};
  const hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/turn", async (route) => {
    await hold;
    await route.continue();
  });
  try {
    await page.getByRole("textbox", { name: "搜尋或追問" }).fill("氣候");
    await page.getByRole("button", { name: "送出搜尋" }).click();
    await expect(page.getByRole("status")).toContainText("正在處理你的搜尋");
    await expect(page.locator(".elapsed")).toHaveText(/已等候 [1-9][0-9]* 秒/);
    await expect(page.locator(".results")).toHaveCount(0);
    await expect(page.getByRole("status")).not.toContainText(/完成|排序中/);
    await page.screenshot({ path: "test-results/waiting.png", fullPage: true });
  } finally {
    release();
  }
  await expect(page.locator(".results")).toHaveCount(1);
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("search artwork is bundled locally and loads under the existing CSP", async ({
  page,
  request,
}) => {
  const externalRequests: string[] = [];
  page.on("request", (entry) => {
    if (new URL(entry.url()).origin !== "http://127.0.0.1:8080")
      externalRequests.push(entry.url());
  });
  await page.goto("/");
  const brand = page.getByRole("link", { name: "depositar AI 首頁" });
  await expect(brand.locator(".ai-badge")).toHaveText("AI");
  const logo = brand.locator("img");
  await expect(logo).toBeVisible();
  await expect(logo).toHaveAttribute("alt", "");
  await expect
    .poll(() => logo.evaluate((el: HTMLImageElement) => el.naturalWidth))
    .toBeGreaterThan(0);
  const logoPath = await logo.getAttribute("src");
  const iconPath = await page.locator('link[rel="icon"]').getAttribute("href");
  for (const path of [logoPath, iconPath]) {
    expect(path).toMatch(/^\/assets\/search-(?:logo|symbol)-[\w-]+\.svg$/);
    const response = await request.get(path!);
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("image/svg+xml");
    const svg = await response.text();
    expect(svg).toContain("#00e09b");
    expect(svg).not.toMatch(/<(?:script|foreignObject)\b|\bon\w+=|\bhref=/i);
    expect(svg).toContain("資料搜尋助理");
  }
  expect(externalRequests).toEqual([]);
});

test("brand and new-search button fit narrow mobile headers", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  for (const width of [320, 390, 640]) {
    await page.setViewportSize({ width, height: 780 });
    const brand = await page
      .getByRole("link", { name: "depositar AI 首頁" })
      .boundingBox();
    const reset = await page
      .getByRole("button", { name: "新搜尋" })
      .boundingBox();
    expect(brand).not.toBeNull();
    expect(reset).not.toBeNull();
    expect(brand!.x + brand!.width + 8).toBeLessThanOrEqual(reset!.x);
    expect(reset!.x + reset!.width).toBeLessThanOrEqual(width);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
  await page.setViewportSize({ width: 320, height: 780 });
  await page.screenshot({
    path: "test-results/narrow-brand.png",
    fullPage: true,
  });
});

test("long descriptions expand as text, older result groups remain accessible", async ({
  page,
}) => {
  await page.goto("/");
  await page.route("**/api/turn", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    value.result.search.cards[0].snippet =
      "這是較長的原始資料描述。".repeat(30) + "完整內容結尾";
    await route.fulfill({ json: value });
  });
  await ask(page, "氣候");
  const card = page.locator(".result-card").first();
  await expect(card.locator(".excerpt")).toHaveClass(/clamped/);
  await card.getByRole("button", { name: /展開.*描述/ }).click();
  await expect(card.locator(".excerpt")).not.toHaveClass(/clamped/);
  await expect(card.locator(".excerpt")).toContainText("完整內容結尾");
  await ask(page, "只要 CSV");
  await expect(page.locator(".result-card:visible")).toHaveCount(5);
  await page.locator(".previous-results summary").first().click();
  await expect(page.locator(".result-card:visible")).toHaveCount(10);
});

test("mobile layout, rejection and geographic clarification", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.screenshot({
    path: "test-results/mobile-home.png",
    fullPage: true,
  });
  await ask(page, "用 Python 解題");
  await expect(page.locator(".assistant")).toContainText(
    "不提供一般問答或程式解題",
  );
  await expect(page.locator(".result-card")).toHaveCount(0);
  await ask(page, "找附近水質資料");
  await expect(page.locator(".assistant").last()).toContainText("未啟用座標");
  await page.screenshot({ path: "test-results/mobile.png", fullPage: true });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("expired session demands a new search, never silently loses conditions", async ({
  page,
}) => {
  await page.goto("/");
  await page.route("**/api/turn", (route) =>
    route.fulfill({
      status: 410,
      contentType: "application/json",
      body: '{"error":{"code":"session_expired"}}',
    }),
  );
  await ask(page, "氣候").catch(() => {});
  await expect(page.getByRole("alert")).toContainText("請開始新搜尋");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
});

test("short year asks for era; ROC reply resolves the topic and later keeps CSV", async ({
  page,
}) => {
  await page.goto("/");
  await ask(page, "111年的澎湖古蹟資料");
  await expect(page.locator(".assistant").last()).toContainText("尚未註明紀年");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".result-card")).toHaveCount(0);
  await ask(page, "民國111年");
  await expect(page.locator(".results").last()).toContainText("澎湖古蹟");
  await expect(page.locator(".results").last().locator(".chip")).toContainText([
    "澎湖古蹟",
    "2022",
  ]);
  await expect(page.locator(".calendar-note").last()).toContainText(
    "換算為西元 2022",
  );
  await ask(page, "只要 CSV");
  await ask(page, "改成民國112年");
  const latest = page.locator(".results").last();
  await expect(latest.locator(".chips")).toContainText("2023");
  await expect(latest.locator(".chips")).toContainText("CSV");
});

test("lunar searches explain unapplied dates; Gregorian correction remains searchable", async ({
  page,
}) => {
  await page.goto("/");
  await ask(page, "民國111年農曆正月初一的澎湖古蹟資料");
  await expect(page.locator(".assistant").last()).toContainText(
    "未套用日期篩選",
  );
  await expect(page.locator(".results")).toHaveCount(1);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await ask(page, "找西元2022年2月1日的澎湖古蹟資料");
  await expect(page.locator(".results").last().locator(".chips")).toContainText(
    "2022 年 2 月 1 日",
  );
  await ask(page, "改成民國111年2月29日");
  await expect(page.locator(".assistant").last()).toContainText("日期無效");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".results").last().locator(".chips")).toContainText(
    "2022 年 2 月 1 日",
  );
});

test("lunar range replaces old year, retains format and warning across turns; subjects still search", async ({
  page,
}) => {
  await page.goto("/");
  await ask(page, "2024年水質資料");
  await expect(page.locator(".results")).toHaveCount(1);
  await ask(page, "只要 CSV");
  await expect(page.locator(".results")).toHaveCount(2);
  const count = await page.locator(".results").count();
  await ask(page, "改成農曆2024-01-01到2024-02-01");
  await expect(page.locator(".assistant").last()).toContainText(
    "未套用日期篩選",
  );
  await expect(page.locator(".results")).toHaveCount(count + 1);
  await expect(
    page
      .locator(".results")
      .last()
      .locator(".chip")
      .filter({ hasText: /^2024 年$/ }),
  ).toHaveCount(0);
  await expect(page.locator(".results").last().locator(".chips")).toContainText(
    "僅文字搜尋",
  );
  await expect(page.locator(".results").last().locator(".chips")).toContainText(
    "CSV",
  );
  await expect(page.getByRole("alert")).toHaveCount(0);
  await ask(page, "只要 CSV");
  await expect(page.locator(".results").last()).toContainText("未套用日期篩選");
  await ask(page, "改成2024年");
  await expect(page.locator(".results").last().locator(".chips")).toContainText(
    "2024 年",
  );
  await expect(page.locator(".results").last()).not.toContainText(
    "未套用日期篩選",
  );
  await page.getByRole("button", { name: "新搜尋" }).click();
  await ask(page, "農曆新年資料集");
  await expect(page.locator(".results")).toHaveCount(1);
  await expect(page.locator(".assistant").last()).not.toContainText(
    "不支援農曆",
  );
  await expect(page.locator(".results").last()).not.toContainText(
    "未套用日期篩選",
  );
});

test("busy does not automatically retry or disable reset", async ({ page }) => {
  let calls = 0;
  await page.goto("/");
  await page.route("**/api/turn", (route) => {
    calls++;
    return route.fulfill({
      status: 429,
      contentType: "application/json",
      body: '{"error":{"code":"busy"}}',
    });
  });
  await ask(page, "氣候");
  await expect(page.getByRole("alert")).toContainText("服務正在處理其他搜尋");
  expect(calls).toBe(1);
  await expect(page.getByRole("button", { name: "新搜尋" })).toBeEnabled();
});

test("untrusted text is not HTML, model URLs cannot become executable links", async ({
  page,
}) => {
  await page.goto("/");
  await page.route("**/api/turn", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    value.result.search.cards[0].title = '<img src=x onerror="window.pwned=1">';
    value.result.search.cards[0].url = "javascript:alert(1)";
    await route.fulfill({ json: value });
  });
  await ask(page, "氣候");
  await expect(page.locator(".result-card").first()).toContainText("<img");
  await expect(
    page.locator(".result-card").first().locator("a, img"),
  ).toHaveCount(0);
});

test("website HTTP cannot expose the private GPU endpoint", async ({
  request,
}) => {
  expect((await request.post("/v1/turn", { data: {} })).status()).toBe(404);
  expect((await request.post("/api/session", { data: {} })).status()).toBe(403);
  const response = await request.get("/");
  expect(response.headers()["content-security-policy"]).toContain(
    "frame-ancestors 'none'",
  );
});
