import { defineConfig } from "@playwright/test";
import { lstatSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

// This separate configuration never enters the normal fixture browser suite.
// An explicit invocation authorizes exactly one real search, without retries.
if (process.env.DEPOSITAR_ACCEPT_PRIVATE_RUNTIME !== "1")
  throw new Error("Private runtime acceptance requires explicit opt-in.");
if (process.env.DEPOSITAR_PRIVATE_RUNTIME_LAUNCHED !== "1")
  throw new Error("Use the private runtime acceptance launcher.");

for (const [name, length] of [
  ["DEPOSITAR_EXPECT_WEB_REVISION", 40],
  ["DEPOSITAR_EXPECT_API_REVISION", 40],
  ["DEPOSITAR_EXPECT_CATALOG_SHA256", 64],
] as const)
  if (!new RegExp(`^[a-f0-9]{${length}}$`).test(process.env[name] ?? ""))
    throw new Error("Expected private runtime identity is missing or invalid.");

const output = process.env.DEPOSITAR_PRIVATE_RUNTIME_OUTPUT;
if (!output || !isAbsolute(output) || resolve(output) !== output)
  throw new Error("Acceptance output must be an absolute directory.");
const parent = dirname(output);
const parentInfo = lstatSync(parent);
if (
  !parentInfo.isDirectory() ||
  parentInfo.uid !== process.getuid?.() ||
  (parentInfo.mode & 0o777) !== 0o700
)
  throw new Error("Acceptance output needs an operator-owned private parent.");
for (let path = parent; ; path = dirname(path)) {
  if (lstatSync(path).isSymbolicLink())
    throw new Error("Acceptance output cannot traverse symlinks.");
  if (path === dirname(path)) break;
}
process.umask(0o077);

export default defineConfig({
  testDir: "./acceptance",
  testMatch: "private-runtime.spec.ts",
  timeout: 210_000,
  expect: { timeout: 10_000 },
  workers: 1,
  retries: 0,
  repeatEach: 1,
  forbidOnly: true,
  outputDir: output,
  reporter: "list",
  use: {
    // Reach the VM's loopback through SSH. Never target the native 18083 trial.
    baseURL: "http://127.0.0.1:18081",
    browserName: "chromium",
    viewport: { width: 1440, height: 900 },
    navigationTimeout: 15_000,
    actionTimeout: 10_000,
    trace: "off",
    video: "off",
    screenshot: "off",
  },
});
