import { privateAccess } from "./access-fixture";
import { expect, test } from "@playwright/test";

function expectSecurityHeaders(headers: Record<string, string>) {
  expect(headers["cache-control"]).toContain("no-store");
  expect(headers["x-content-type-options"]).toBe("nosniff");
  expect(headers["content-security-policy"]).toContain(
    "frame-ancestors 'none'",
  );
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers).not.toHaveProperty("access-control-allow-origin");
}

test("concurrent static requests sharing a proxy IP are all served", async ({
  request,
}) => {
  const homepage = await request.get("/");
  expect(homepage.status()).toBe(200);
  const stylesheet = /href="([^"]+\.css)"/.exec(await homepage.text())?.[1];
  expect(stylesheet).toMatch(/^\/assets\//);
  const responses = await Promise.all(
    Array.from({ length: 40 }, () => request.get(stylesheet!)),
  );
  for (const response of responses) {
    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/css");
  }
});

test("Nginx serves the homepage and bundled assets", async ({ page }) => {
  const assets: { url: string; status: number }[] = [];
  page.on("response", (response) => {
    if (new URL(response.url()).pathname.startsWith("/assets/"))
      assets.push({ url: response.url(), status: response.status() });
  });
  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  expectSecurityHeaders(response!.headers());
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "從一句話開始",
  );
  expect(assets.length).toBeGreaterThan(0);
  expect(assets.every((asset) => asset.status === 200)).toBe(true);
});

test("Nginx serves CSS with GET and HEAD and keeps missing assets unavailable", async ({
  request,
}) => {
  const homepage = await request.get("/");
  expect(homepage.status()).toBe(200);
  const stylesheet = /href="([^"]+\.css)"/.exec(await homepage.text())?.[1];
  expect(stylesheet).toMatch(/^\/assets\//);
  const css = await request.get(stylesheet!);
  expect(css.status()).toBe(200);
  expect(css.headers()["content-type"]).toContain("text/css");
  expect(await css.text()).not.toBe("");
  const head = await request.head(stylesheet!);
  expect(head.status()).toBe(200);
  expect(head.headers()["content-type"]).toContain("text/css");
  expect(head.headers()["content-length"]).toBe(
    css.headers()["content-length"],
  );
  expect(await head.body()).toHaveLength(0);
  const missing = await request.get("/assets/nonexistent.css");
  expect(missing.status()).toBe(404);
  expect(missing.headers()["content-type"]).not.toContain("text/css");
  for (const response of [homepage, css, head, missing])
    expectSecurityHeaders(response.headers());
});

test("static files reject writes and unsupported methods", async ({
  request,
}) => {
  for (const method of ["POST", "OPTIONS"]) {
    const response = await request.fetch("/index.html", {
      method,
      data: method === "POST" ? "replacement page" : undefined,
    });
    expect(response.status()).toBe(405);
    expectSecurityHeaders(response.headers());
  }
  const homepage = await request.get("/");
  expect(homepage.status()).toBe(200);
  expect(await homepage.text()).not.toContain("replacement page");
});

test("Nginx rejects traversal and hidden files without serving the app", async ({
  request,
}) => {
  for (const path of [
    "/%2e%2e%2fetc/passwd",
    "/assets/%2e%2e%2f%2e%2e%2fetc/passwd",
    "/.env",
    "/.git/config",
    "/assets/%2e%2e%2f.env",
  ]) {
    const response = await request.get(path);
    expect([400, 404]).toContain(response.status());
    expect(await response.text()).not.toMatch(/root:|<div id="root"/);
    expectSecurityHeaders(response.headers());
  }
});

test("gateway API routes stay distinct from frontend files", async ({
  request,
}) => {
  const health = await request.get("/healthz");
  expect(health.status()).toBe(200);
  expect(await health.json()).toEqual({ status: "ok" });
  const access = await request.get("/api/access");
  expect(access.status()).toBe(200);
  expect(await access.json()).toEqual(privateAccess);
  const headers = { Origin: new URL(health.url()).origin };
  const method = await request.get("/api/session", { headers });
  expect(method.status()).toBe(405);
  expect(method.headers()["content-type"]).toContain("application/json");
  for (const path of ["/api/missing", "/api/session/"]) {
    const response = await request.get(path, { headers });
    expect(response.status()).toBe(404);
    expect(await response.text()).not.toContain('<div id="root"');
  }
});
