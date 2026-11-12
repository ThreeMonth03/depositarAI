import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { TraceStatus } from "../src/api";
import { privateAccess } from "./access-fixture";

const noticeVersion = "search-trace-v1";

function status(enabled = false): TraceStatus {
  return {
    enabled,
    test_session_id: enabled ? "a".repeat(32) : null,
    notice_version: noticeVersion,
    retention_days: 3,
    capture_status: enabled ? "queued" : "disabled",
    deletion_status: "none",
  };
}

async function installAPI(page: Page) {
  const state = {
    available: true,
    expires: 1800,
    trace: status(),
    deletion: "complete" as TraceStatus["deletion_status"],
    failDelete: false,
    failWithdraw: false,
    failTurn: false,
    holdTurn: null as Promise<void> | null,
    holdDelete: null as Promise<void> | null,
    holdGet: null as Promise<void> | null,
    getStatuses: [] as string[],
    failGet: false,
    gets: [] as string[],
    sessionBodies: [] as Record<string, unknown>[],
    turns: [] as { query: string; version: number }[],
    patches: [] as { body: unknown; authorization: string }[],
    deletes: 0,
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/access")
      return route.fulfill({
        json: {
          ...privateAccess,
          trace_policy: {
            available: state.available,
            notice_version: noticeVersion,
            retention_days: 3,
          },
        },
      });
    if (path === "/api/session" && request.method() === "POST") {
      const body = request.postDataJSON();
      state.sessionBodies.push(body);
      state.trace = status(body.capture === true);
      return route.fulfill({
        json: {
          token: `synthetic-session-${state.sessionBodies.length}`,
          version: 0,
          expires_in: state.expires,
          max_turns: 20,
          revision: "fixture",
          trace: state.trace,
        },
      });
    }
    if (path === "/api/session" && request.method() === "GET") {
      state.gets.push(request.headers().authorization);
      const trace = {
        ...state.trace,
        capture_status: state.getStatuses.shift() ?? state.trace.capture_status,
      };
      const hold = state.holdGet;
      state.holdGet = null;
      if (hold) await hold;
      if (state.failGet)
        return route.fulfill({
          status: 503,
          json: { error: { code: "unavailable" } },
        });
      return route.fulfill({ json: { trace } });
    }
    if (path === "/api/session" && request.method() === "PATCH") {
      state.patches.push({
        body: request.postDataJSON(),
        authorization: request.headers().authorization,
      });
      if (state.failWithdraw)
        return route.fulfill({
          status: 503,
          json: { error: { code: "unavailable" } },
        });
      state.trace = {
        ...state.trace,
        enabled: false,
        capture_status: "revoked",
        deletion_status: state.deletion,
      };
      return route.fulfill({ json: { trace: state.trace } });
    }
    if (path === "/api/session" && request.method() === "DELETE") {
      state.deletes++;
      if (state.holdDelete) await state.holdDelete;
      if (state.failDelete)
        return route.fulfill({
          status: 503,
          json: { error: { code: "unavailable" } },
        });
      state.trace = {
        ...state.trace,
        enabled: false,
        capture_status: "revoked",
        deletion_status: state.deletion,
      };
      return route.fulfill({
        json: { status: "cleared", trace: state.trace },
      });
    }
    if (path !== "/api/turn")
      throw new Error(`Unexpected synthetic route ${request.method()} ${path}`);
    const { query, version } = request.postDataJSON();
    state.turns.push({ query, version });
    const trace = { ...state.trace };
    const hold = state.holdTurn;
    state.holdTurn = null;
    if (hold) await hold;
    if (state.failTurn)
      return route.fulfill({
        status: 503,
        json: { error: { code: "upstream_unavailable" }, trace },
      });
    return route.fulfill({
      json: {
        version: version + 1,
        context_used: {
          reference_date: "2026-11-12",
          timezone: "Asia/Taipei",
        },
        result: {
          status: "rejected",
          message: "合成搜尋回覆",
          state: { topic: "", time: null, formats: [] },
          reason: "off_topic",
          revision: "fixture",
          timing_ms: {},
          search: null,
        },
        trace,
      },
    });
  });
  return state;
}

async function submit(page: Page, text = "氣候資料") {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(text);
  await page.getByRole("button", { name: "送出搜尋" }).click();
}

const completedReplies = (page: Page) =>
  page.locator(".assistant").filter({ hasText: "合成搜尋回覆" });

const choice = (page: Page) =>
  page.getByRole("checkbox", { name: /願意從這次新搜尋開始/ });

test("notice precedes the default-off choice and an ordinary session cannot opt in later", async ({
  page,
}) => {
  const api = await installAPI(page);
  await page.setViewportSize({ width: 320, height: 780 });
  await page.goto("/");
  await expect(choice(page)).not.toBeChecked();
  await expect(page.locator(".trace-choice")).toContainText("最長 3 天");
  await expect(page.locator(".trace-choice")).toContainText("提問原文");
  await expect(page.locator(".trace-choice")).toContainText("不代表完全匿名");
  expect(api.sessionBodies).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await submit(page);
  await expect(page.locator(".assistant")).toContainText("合成搜尋回覆");
  expect(api.sessionBodies).toEqual([{}]);
  await expect(choice(page)).toBeDisabled();
  await expect(page.getByRole("button", { name: /撤回並刪除/ })).toHaveCount(0);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(choice(page)).toBeEnabled();
  await expect(choice(page)).not.toBeChecked();
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("consent uses the notice version and withdrawal distinguishes pending from deleted", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.deletion = "pending";
  await page.goto("/");
  await choice(page).check();
  await submit(page, "氣候 2024 CSV");
  await expect(page.locator(".trace-state")).toContainText("已排隊");
  await expect(page.locator(".trace-state")).not.toContainText("已確認保存");
  await submit(page, "改成 2023");
  await expect(completedReplies(page)).toHaveCount(2);
  expect(api.sessionBodies).toEqual([
    { capture: true, notice_version: noticeVersion },
  ]);
  await page.getByRole("button", { name: "撤回並刪除測試紀錄" }).click();
  await expect(page.locator(".trace-state")).toContainText("仍待刪除");
  await expect(page.locator(".trace-state")).not.toContainText("已刪除");
  await expect(choice(page)).not.toBeChecked();
  expect(api.patches).toEqual([
    {
      body: { capture: false },
      authorization: "Bearer synthetic-session-1",
    },
  ]);
  api.deletion = "complete";
  await page.getByRole("button", { name: "確認刪除進度" }).click();
  await expect(page.locator(".trace-state")).toContainText("紀錄已刪除");
  await submit(page, "只要 CSV");
  await expect(completedReplies(page)).toHaveCount(3);
  await expect(choice(page)).toBeDisabled();
  expect(api.sessionBodies).toHaveLength(1);
  expect(api.turns.map((turn) => turn.version)).toEqual([0, 1, 2]);
});

test("withdrawal remains available in flight and a late turn cannot restore capture", async ({
  page,
}) => {
  const api = await installAPI(page);
  let release = () => {};
  api.holdTurn = new Promise<void>((resolve) => {
    release = resolve;
  });
  api.deletion = "pending";
  try {
    await page.goto("/");
    await choice(page).check();
    await submit(page);
    await expect(
      page.getByRole("button", { name: "撤回並刪除測試紀錄" }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "撤回並刪除測試紀錄" }).click();
    await expect(page.locator(".trace-state")).toContainText("仍待刪除");
    release();
    await expect(page.locator(".assistant")).toContainText("合成搜尋回覆");
    await expect(page.locator(".trace-state")).toContainText("仍待刪除");
    await expect(choice(page)).not.toBeChecked();
    await expect(
      page.getByRole("button", { name: "撤回並刪除測試紀錄" }),
    ).toHaveCount(0);
  } finally {
    release();
  }
});

test("failed withdrawal pauses new turns until a successful retry", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.failWithdraw = true;
  await page.goto("/");
  await choice(page).check();
  await submit(page);
  await expect(completedReplies(page)).toHaveCount(1);
  await page.getByRole("button", { name: "撤回並刪除測試紀錄" }).click();
  await expect(page.locator(".trace-error")).toContainText("無法確認撤回");
  await expect(
    page.getByRole("textbox", { name: "搜尋或追問" }),
  ).toBeDisabled();
  await expect(page.locator(".trace-state")).not.toContainText("已刪除");
  api.failWithdraw = false;
  await page.getByRole("button", { name: "重試撤回" }).click();
  await expect(page.locator(".trace-state")).toContainText("紀錄已刪除");
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  expect(api.turns).toHaveLength(1);
});

test("failed reset keeps the conversation and never claims deletion; pending reset is explicit", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.failDelete = true;
  await page.goto("/");
  await choice(page).check();
  await submit(page);
  await expect(completedReplies(page)).toHaveCount(1);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.locator(".trace-error")).toContainText("無法確認測試紀錄");
  await expect(page.locator(".message")).toHaveCount(2);
  await expect(page.locator(".trace-state")).not.toContainText("已刪除");
  api.failDelete = false;
  api.deletion = "pending";
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.locator(".message")).toHaveCount(0);
  await expect(choice(page)).not.toBeChecked();
  await expect(choice(page)).toBeEnabled();
  await expect(page.locator(".trace-state")).toContainText("仍待刪除");
  await expect(page.locator(".trace-state")).toContainText("請聯絡維護者");
  await expect(page.getByRole("button", { name: "確認刪除進度" })).toHaveCount(
    0,
  );
  expect(api.deletes).toBe(2);
});

test("a late reset response cannot restore traces after account restoration", async ({
  page,
}) => {
  const api = await installAPI(page);
  let release = () => {};
  api.holdDelete = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await page.goto("/");
    await choice(page).check();
    await submit(page);
    await expect(completedReplies(page)).toHaveCount(1);
    await page.getByRole("button", { name: "新搜尋" }).click();
    await expect.poll(() => api.deletes).toBe(1);
    await page.evaluate(() => {
      window.dispatchEvent(
        new PageTransitionEvent("pageshow", { persisted: true }),
      );
    });
    await expect(choice(page)).not.toBeChecked();
    release();
    await expect(page.getByRole("button", { name: "新搜尋" })).toBeEnabled();
    await expect(page.locator(".trace-state")).toHaveCount(0);
    await expect(page.locator(".message")).toHaveCount(0);
  } finally {
    release();
  }
});

test("a new search after deletion starts with capture off", async ({
  page,
}) => {
  const api = await installAPI(page);
  await page.goto("/");
  await choice(page).check();
  await submit(page);
  await expect(completedReplies(page)).toHaveCount(1);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.locator(".trace-state")).toContainText("紀錄已刪除");
  await expect(choice(page)).not.toBeChecked();
  await submit(page, "新的搜尋");
  await expect(completedReplies(page)).toHaveCount(1);
  expect(api.sessionBodies).toEqual([
    { capture: true, notice_version: noticeVersion },
    {},
  ]);
});

test("upstream error exposes capture degradation without changing the search failure", async ({
  page,
}) => {
  const api = await installAPI(page);
  await page.goto("/");
  await choice(page).check();
  await submit(page);
  await expect(completedReplies(page)).toHaveCount(1);
  api.trace.capture_status = "queue_full";
  api.failTurn = true;
  await submit(page, "改 2023");
  await expect(page.locator(".error")).toContainText("搜尋服務暫時無法使用");
  await expect(page.locator(".trace-state")).toContainText(
    "部分測試紀錄未能保存",
  );
  expect(api.turns.map((turn) => turn.version)).toEqual([0, 1]);
});

for (const reason of ["expiry", "account restoration"] as const)
  test(`${reason} clears the consent choice`, async ({ page }) => {
    const api = await installAPI(page);
    if (reason === "expiry") {
      api.expires = 5;
      await page.clock.install({ time: new Date("2026-11-12T12:00:00Z") });
    }
    await page.goto("/");
    await choice(page).check();
    await submit(page);
    await expect(completedReplies(page)).toHaveCount(1);
    if (reason === "expiry") await page.clock.runFor(5000);
    else
      await page.evaluate(() => {
        window.dispatchEvent(
          new PageTransitionEvent("pageshow", { persisted: true }),
        );
      });
    await expect(page.locator(".message")).toHaveCount(0);
    await expect(choice(page)).not.toBeChecked();
  });

test("an unavailable trace policy offers no consent control and keeps ordinary search", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.available = false;
  await page.goto("/");
  await expect(choice(page)).toHaveCount(0);
  await expect(page.locator(".trace-unavailable")).toBeVisible();
  await submit(page);
  await expect(completedReplies(page)).toHaveCount(1);
  expect(api.sessionBodies).toEqual([{}]);
});

test("authenticated status polling recognizes captured and stops after success", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.getStatuses = ["captured"];
  await page.goto("/");
  await choice(page).check();
  await submit(page);
  await expect(page.locator(".trace-state")).toContainText("已確認保存");
  await expect(page.locator(".trace-state")).not.toContainText("未能保存");
  expect(api.gets).toEqual(["Bearer synthetic-session-1"]);
});

for (const outcome of ["queued", "unavailable", "unknown"] as const)
  test(`status polling is bounded and ${outcome} cannot change search state`, async ({
    page,
  }) => {
    const api = await installAPI(page);
    if (outcome === "unavailable") api.failGet = true;
    if (outcome === "unknown") api.getStatuses = ["unexpected-status"];
    await page.clock.install();
    await page.goto("/");
    await choice(page).check();
    await submit(page);
    await expect(completedReplies(page)).toHaveCount(1);
    await expect.poll(() => api.gets.length).toBe(outcome === "queued" ? 3 : 1);
    await page.clock.runFor(10000);
    expect(api.gets).toHaveLength(outcome === "queued" ? 3 : 1);
    await expect(completedReplies(page)).toHaveCount(1);
    await expect(
      page.getByRole("textbox", { name: "搜尋或追問" }),
    ).toBeEnabled();
    await expect(page.locator(".trace-state")).toContainText("已排隊");
    await expect(page.locator(".error")).toHaveCount(0);
  });

test("a late status poll cannot replace the next turn's degraded capture status", async ({
  page,
}) => {
  const api = await installAPI(page);
  api.getStatuses = ["captured"];
  let release = () => {};
  api.holdGet = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await page.goto("/");
    await choice(page).check();
    await submit(page);
    await expect.poll(() => api.gets.length).toBe(1);
    api.trace.capture_status = "queue_full";
    await submit(page, "改成 2023");
    await expect(completedReplies(page)).toHaveCount(2);
    await expect(page.locator(".trace-state")).toContainText("未能保存");
    release();
    await expect(page.locator(".trace-state")).toContainText("未能保存");
    await expect(page.locator(".trace-state")).not.toContainText("已確認保存");
  } finally {
    release();
  }
});

for (const action of ["withdraw", "reset", "restore"] as const)
  test(`a late status poll cannot restore capture after ${action}`, async ({
    page,
  }) => {
    const api = await installAPI(page);
    api.getStatuses = ["captured"];
    let release = () => {};
    api.holdGet = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      await page.goto("/");
      await choice(page).check();
      await submit(page);
      await expect.poll(() => api.gets.length).toBe(1);
      if (action === "withdraw")
        await page.getByRole("button", { name: "撤回並刪除測試紀錄" }).click();
      else if (action === "reset")
        await page.getByRole("button", { name: "新搜尋" }).click();
      else
        await page.evaluate(() =>
          window.dispatchEvent(
            new PageTransitionEvent("pageshow", { persisted: true }),
          ),
        );
      await expect(choice(page)).not.toBeChecked();
      release();
      if (action === "restore")
        await expect(page.locator(".trace-state")).toHaveCount(0);
      else
        await expect(page.locator(".trace-state")).toContainText("紀錄已刪除");
    } finally {
      release();
    }
  });
