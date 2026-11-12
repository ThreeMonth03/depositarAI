import { readFile, readdir } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

// Enable this flow only against the deterministic CPU HTTP fixture.
test.skip(
  process.env.DEPOSITAR_TEST_TRACE_RUNTIME !== "1",
  "Private trace runtime fixture is not enabled.",
);

async function ask(page: Page, query: string) {
  const response = page.waitForResponse(
    (item) =>
      item.url().endsWith("/api/turn") && item.request().method() === "POST",
  );
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(query);
  await page.getByRole("button", { name: "送出搜尋" }).click();
  const value = await (await response).json();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  return value;
}

test("real BFF consent captures two turns, polls status, withdraws and resets to off", async ({
  page,
}) => {
  const root = process.env.DEPOSITAR_TRACE_FIXTURE_ROOT;
  const requirePending = process.env.DEPOSITAR_TRACE_FIXTURE_PENDING === "1";
  const ids = async () =>
    (await readdir(root!)).filter((name) => /^[a-f0-9]{32}$/.test(name));
  const statuses: { capture_status: string; deletion_status: string }[] = [];
  page.on("response", async (response) => {
    if (
      !response.url().endsWith("/api/session") ||
      response.request().method() !== "GET" ||
      !response.ok()
    )
      return;
    const value = await response.json();
    statuses.push(value.trace);
  });
  await page.goto("/");
  const choice = page.getByRole("checkbox", { name: /願意從這次新搜尋開始/ });
  await expect(choice).not.toBeChecked();
  await expect(page.locator(".trace-choice")).toContainText("最長 7 天");
  await expect(page.locator(".trace-choice")).toContainText("本次搜尋有效期間");
  const ordinary = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/session") &&
      response.request().method() === "POST",
  );
  await ask(page, "氣候資料");
  const ordinaryResponse = await ordinary;
  expect(ordinaryResponse.request().postDataJSON()).toEqual({});
  const ordinaryTrace = (await ordinaryResponse.json()).trace;
  expect(ordinaryTrace.enabled).toBe(false);
  expect(ordinaryTrace.test_session_id).toBeNull();
  if (root) expect(await ids()).toEqual([]);

  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(choice).toBeEnabled();
  await choice.check();
  const consent = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/session") &&
      response.request().method() === "POST",
  );
  const first = await ask(page, "找 2024 年的氣候資料");
  const consentResponse = await consent;
  expect(consentResponse.request().postDataJSON()).toEqual({
    capture: true,
    notice_version: "search-trace-v1",
  });
  const session = await consentResponse.json();
  const identifier = session.trace.test_session_id;
  expect(identifier).toMatch(/^[a-f0-9]{32}$/);
  expect(identifier).not.toBe(session.token);
  const readStatus = () =>
    page.evaluate(async (token: string) => {
      const response = await fetch("/api/session", {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) throw new Error("fixture_trace_status_unavailable");
      return response.json();
    }, session.token);
  await expect(page.locator(".trace-state")).toContainText("已確認保存");
  await expect(page.locator(".trace-state")).not.toContainText("未能保存");
  const second = await ask(page, "只要 CSV");
  await expect(page.locator(".trace-state")).toContainText("已確認保存");
  expect(first.version).toBe(1);
  expect(second.version).toBe(2);
  expect(second.result.state.topic).toBe(first.result.state.topic);
  expect((await readStatus()).trace.capture_status).toBe("captured");
  expect(statuses.some((value) => value.capture_status === "captured")).toBe(
    true,
  );

  if (root) {
    const one = JSON.parse(
      await readFile(`${root}/${identifier}/turn-001.json`, "utf8"),
    );
    const two = JSON.parse(
      await readFile(`${root}/${identifier}/turn-002.json`, "utf8"),
    );
    expect(one.input.query).toBe("找 2024 年的氣候資料");
    expect(two.input.query).toBe("只要 CSV");
    expect(two.state_before).toEqual(one.state_after);
    expect(one.execution.kind).toBe("legacy_conditions");
    expect(two.execution.kind).toBe("legacy_conditions");
    expect(
      one.output.search.cards.map(
        (card: { dataset_id: string }) => card.dataset_id,
      ),
    ).toEqual(
      first.result.search.cards.map(
        (card: { dataset_id: string }) => card.dataset_id,
      ),
    );
    expect(
      two.output.search.cards.map(
        (card: { dataset_id: string }) => card.dataset_id,
      ),
    ).toEqual(
      second.result.search.cards.map(
        (card: { dataset_id: string }) => card.dataset_id,
      ),
    );
    expect(JSON.stringify([one, two])).not.toContain(session.token);
  }

  const withdrawal = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/session") &&
      response.request().method() === "PATCH",
  );
  await page.getByRole("button", { name: "撤回並刪除測試紀錄" }).click();
  const withdrawn = await (await withdrawal).json();
  expect(withdrawn.trace.enabled).toBe(false);
  expect(["pending", "complete"]).toContain(withdrawn.trace.deletion_status);
  if (requirePending) {
    expect(withdrawn.trace.deletion_status).toBe("pending");
    await expect(page.locator(".trace-state")).toContainText("仍待刪除");
  }
  await expect
    .poll(async () => (await readStatus()).trace.deletion_status)
    .toBe("complete");
  await expect(page.locator(".trace-state")).toContainText("紀錄已刪除");
  expect(statuses.some((value) => value.deletion_status === "complete")).toBe(
    true,
  );
  if (root) expect(await ids()).not.toContain(identifier);
  await ask(page, "改成 2024 年");
  if (root) expect(await ids()).not.toContain(identifier);

  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(choice).not.toBeChecked();
  const fresh = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/session") &&
      response.request().method() === "POST",
  );
  await ask(page, "新的氣候搜尋");
  const freshResponse = await fresh;
  expect(freshResponse.request().postDataJSON()).toEqual({});
  expect((await freshResponse.json()).trace.enabled).toBe(false);
  if (root) expect(await ids()).toEqual([]);
});
