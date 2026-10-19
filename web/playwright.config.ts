import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8080",
    browserName: "chromium",
    screenshot: "only-on-failure",
    trace: "off",
  },
  reporter: [["list"], ["json", { outputFile: "test-results/results.json" }]],
});
