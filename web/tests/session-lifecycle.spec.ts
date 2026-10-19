import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Reply } from "../src/api";

function reply(version: number, referenceDate: string): Reply {
  return {
    version,
    context_used: { reference_date: referenceDate, timezone: "Asia/Taipei" },
    result: {
      status: "ok",
      message: "搜尋完成",
      state: { topic: "河川水質", time: null, formats: [] },
      reason: "none",
      revision: "test",
      timing_ms: { total: 20 },
      search: {
        cards: [
          {
            dataset_id: "session-lifecycle-test",
            title: "測試河川水質",
            url: "https://data.depositar.io/dataset/session-lifecycle-test",
            snippet: "測試資料說明",
            organization_title: "測試單位",
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
}

async function submit(page: Page, query: string) {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(query);
  await page.getByRole("button", { name: "送出搜尋" }).click();
}

for (const outcome of ["success", "unavailable", "login_required"] as const)
  test(`local expiry discards a late ${outcome} response and a new search uses a fresh session`, async ({
    page,
  }) => {
    await page.clock.install({ time: new Date("2026-10-16T12:00:00Z") });
    await page.clock.pauseAt(new Date("2026-10-16T12:00:01Z"));

    let release = () => {};
    const hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const calls: string[] = [];
    let sessions = 0;
    const turns: {
      authorization: string | undefined;
      version: number;
      query: string;
    }[] = [];
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      calls.push(`${request.method()} ${path}`);
      if (path === "/api/access")
        return route.fulfill({
          json: { auth_required: false, authenticated: false },
        });
      if (path === "/api/session" && request.method() === "POST")
        return route.fulfill({
          json: {
            token: `session-lifecycle-${++sessions}`,
            version: 0,
            expires_in: 5,
            max_turns: 20,
            revision: "test",
          },
        });
      if (path !== "/api/turn")
        throw new Error(`unexpected request: ${request.method()} ${path}`);
      const body = request.postDataJSON();
      turns.push({
        authorization: request.headers().authorization,
        version: body.version,
        query: body.query,
      });
      if (turns.length === 2) {
        await hold;
        if (outcome !== "success")
          return route.fulfill({
            status: outcome === "unavailable" ? 503 : 401,
            json: {
              error: {
                code:
                  outcome === "unavailable"
                    ? "unavailable"
                    : "authentication_required",
              },
            },
          });
        return route.fulfill({ json: reply(2, "2026-10-18") });
      }
      return route.fulfill({
        json: reply(1, sessions === 1 ? "2026-10-16" : "2026-10-17"),
      });
    });

    try {
      await page.goto("/");
      await expect(
        page.getByRole("textbox", { name: "搜尋或追問" }),
      ).toBeEnabled();
      await page.locator(".usage-notes summary").click();
      await submit(page, "河川水質");
      await expect(page.locator(".result-card")).toHaveCount(1);
      await expect(page.locator(".usage-notes")).toContainText("2026-10-16");

      const pendingTurn = page.waitForRequest(
        (request) =>
          new URL(request.url()).pathname === "/api/turn" &&
          request.postDataJSON().query === "只要 CSV",
      );
      await submit(page, "只要 CSV");
      await pendingTurn;
      await page.clock.runFor(5000);
      const expiryMessage = "本次搜尋已到期並清除，請開始新搜尋。";
      await expect(page.getByRole("alert")).toHaveText(expiryMessage);
      await expect(page.locator(".transcript")).not.toContainText(
        /河川水質|只要 CSV/,
      );
      await expect(page.locator(".result-card")).toHaveCount(0);
      if (outcome === "success")
        await expect(page.locator(".usage-notes")).not.toContainText(
          "本次參考日",
        );
      await expect(
        page.getByRole("textbox", { name: "搜尋或追問" }),
      ).toBeDisabled();

      release();
      await expect(page.getByRole("button", { name: "新搜尋" })).toBeEnabled();
      await expect(page.getByRole("alert")).toHaveText(expiryMessage);
      await expect(page.locator(".message")).toHaveCount(0);
      await expect(page.locator(".result-card")).toHaveCount(0);
      await expect(page.locator(".usage-notes")).not.toContainText(
        "本次參考日",
      );
      await expect(
        page.getByRole("textbox", { name: "搜尋或追問" }),
      ).toBeDisabled();
      await expect(
        page.getByRole("link", { name: "免費登入", exact: true }),
      ).toHaveCount(0);
      expect(calls).toEqual([
        "GET /api/access",
        "POST /api/session",
        "POST /api/turn",
        "POST /api/turn",
      ]);

      await page.getByRole("button", { name: "新搜尋" }).click();
      await expect(page.getByRole("alert")).toHaveCount(0);
      await expect(
        page.getByRole("textbox", { name: "搜尋或追問" }),
      ).toBeEnabled();
      await submit(page, "氣候");
      await expect(page.locator(".message")).toHaveCount(2);
      await expect(page.locator(".result-card")).toHaveCount(1);
      await expect(page.locator(".usage-notes")).toContainText("2026-10-17");
      await expect(page.locator(".usage-notes")).not.toContainText(
        "2026-10-18",
      );
      expect(sessions).toBe(2);
      expect(turns).toEqual([
        {
          authorization: "Bearer session-lifecycle-1",
          version: 0,
          query: "河川水質",
        },
        {
          authorization: "Bearer session-lifecycle-1",
          version: 1,
          query: "只要 CSV",
        },
        {
          authorization: "Bearer session-lifecycle-2",
          version: 0,
          query: "氣候",
        },
      ]);
      expect(calls).toEqual([
        "GET /api/access",
        "POST /api/session",
        "POST /api/turn",
        "POST /api/turn",
        "POST /api/session",
        "POST /api/turn",
      ]);
    } finally {
      release();
    }
  });
