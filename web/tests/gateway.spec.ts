import { expect, test } from "./gateway";

test("the real gateway rejects a burst above the production shared-IP budget", async ({
  request,
}) => {
  const responses = await Promise.all(
    Array.from({ length: 40 }, () => request.get("/healthz")),
  );
  const statuses = responses.map((response) => response.status());
  expect(statuses.filter((status) => status === 200).length).toBeGreaterThan(0);
  expect(statuses.filter((status) => status === 429).length).toBeGreaterThan(0);
  expect(statuses.every((status) => status === 200 || status === 429)).toBe(
    true,
  );
  for (const response of responses.filter(
    (response) => response.status() === 429,
  )) {
    expect(response.headers().server).toBe("nginx");
    expect(await response.text()).toContain("429 Too Many Requests");
  }
});

test("a fresh gateway serves the homepage and assets after the preceding burst", async ({
  page,
}) => {
  const assets: { url: string; status: number }[] = [];
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/assets/"))
      assets.push({ url: response.url(), status: response.status() });
  });
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "從一句話開始",
  );
  expect(assets.length).toBeGreaterThan(0);
  expect(assets.every((asset) => asset.status === 200)).toBe(true);
});
