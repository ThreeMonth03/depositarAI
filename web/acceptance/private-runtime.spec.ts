import { expect, test } from "@playwright/test";
import { writeFile } from "node:fs/promises";

test("one real private search, responsive results and session reset", async ({
  page,
}, testInfo) => {
  expect(testInfo.config.workers).toBe(1);
  expect(testInfo.project.retries).toBe(0);
  expect(testInfo.project.repeatEach).toBe(1);
  const origin = "http://127.0.0.1:18081";
  let sessionCreated = false;
  const calls: string[] = [];
  const failures: { path: string; status: number }[] = [];
  const proof = {
    completed: false,
    origin,
    web_revision: null as string | null,
    api_revision: null as string | null,
    catalog_manifest_sha256: null as string | null,
    cards: 0,
    browser_errors: 0,
    failed_requests: 0,
    external_requests: 0,
    reset_attempted: false,
    reset_completed: false,
    calls,
    failures,
  };
  page.on("pageerror", () => proof.browser_errors++);
  page.on("requestfailed", () => proof.failed_requests++);
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin !== origin) proof.external_requests++;
    else if (url.pathname.startsWith("/api/"))
      calls.push(`${request.method()} ${url.pathname}`);
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (
      response.url() === `${origin}/api/session` &&
      response.request().method() === "POST" &&
      response.status() === 200
    )
      sessionCreated = true;
    if (url.origin === origin && response.status() >= 400)
      failures.push({ path: url.pathname, status: response.status() });
  });
  const resetSession = async () => {
    proof.reset_attempted = true;
    const button = page.getByRole("button", { name: "新搜尋" });
    await expect(button).toBeEnabled({ timeout: 145_000 });
    const [response] = await Promise.all([
      page.waitForResponse(
        (reply) =>
          reply.url() === `${origin}/api/session` &&
          reply.request().method() === "DELETE",
        { timeout: 10_000 },
      ),
      button.click(),
    ]);
    proof.reset_completed = response.status() === 200;
    expect(response.status()).toBe(200);
  };

  try {
    await page.goto("/");
    await expect(page).toHaveTitle("depositar AI");
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeEnabled();
    const sessionReply = page.waitForResponse(
      (response) =>
        response.url() === `${origin}/api/session` &&
        response.request().method() === "POST",
    );
    const turnReply = page.waitForResponse(
      (response) =>
        response.url() === `${origin}/api/turn` &&
        response.request().method() === "POST",
      { timeout: 145_000 },
    );
    await page
      .getByRole("textbox", { name: "搜尋或追問" })
      .fill("台灣空氣品質資料");
    await page.getByRole("button", { name: "送出搜尋" }).click();
    const session = await sessionReply;
    expect(session.status()).toBe(200);
    const { revision } = await session.json();
    proof.web_revision = /^[a-f0-9]{40}$/.test(revision) ? revision : null;
    expect(proof.web_revision).toBe(process.env.DEPOSITAR_EXPECT_WEB_REVISION);

    const response = await turnReply;
    expect(response.status()).toBe(200);
    const reply = await response.json();
    expect(reply.version).toBe(1);
    expect(reply.result?.status).toBe("ok");
    proof.api_revision = /^[a-f0-9]{40}$/.test(reply.result?.revision)
      ? reply.result.revision
      : null;
    proof.catalog_manifest_sha256 = /^[a-f0-9]{64}$/.test(
      reply.result?.search?.catalog_manifest_sha256,
    )
      ? reply.result.search.catalog_manifest_sha256
      : null;
    expect(proof.api_revision).toBe(process.env.DEPOSITAR_EXPECT_API_REVISION);
    expect(proof.catalog_manifest_sha256).toBe(
      process.env.DEPOSITAR_EXPECT_CATALOG_SHA256,
    );
    proof.cards = Array.isArray(reply.result?.search?.cards)
      ? reply.result.search.cards.length
      : 0;
    expect(proof.cards).toBeGreaterThan(0);
    expect(proof.cards).toBeLessThanOrEqual(5);
    await expect(page.locator(".result-card:visible")).toHaveCount(proof.cards);
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeEnabled();
    await page.screenshot({
      path: testInfo.outputPath("desktop.png"),
      fullPage: true,
    });

    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath("mobile.png"),
      fullPage: true,
    });
    await resetSession();
    await expect(page.locator(".result-card")).toHaveCount(0);
    await expect(page.locator(".message")).toHaveCount(0);
    expect(
      await page.evaluate(() => [localStorage.length, sessionStorage.length]),
    ).toEqual([0, 0]);
    expect(calls).toEqual([
      "GET /api/access",
      "POST /api/session",
      "POST /api/turn",
      "DELETE /api/session",
    ]);
    expect(failures).toEqual([]);
    expect(proof.browser_errors).toBe(0);
    expect(proof.failed_requests).toBe(0);
    expect(proof.external_requests).toBe(0);
    proof.completed = true;
  } finally {
    if (sessionCreated && !proof.reset_attempted && !page.isClosed()) {
      try {
        await resetSession();
      } catch {
        // Keep the original failure; cleanup never sends a second turn/retry.
      }
    }
    // Never persist bodies, tokens, headers, cookies or raw exception text.
    await writeFile(
      testInfo.outputPath("proof.json"),
      JSON.stringify(proof, null, 2) + "\n",
      {
        mode: 0o600,
      },
    );
  }
});
