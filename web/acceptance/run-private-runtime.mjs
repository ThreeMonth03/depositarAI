import { spawnSync } from "node:child_process";
import { lstatSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

if (
  process.argv.length !== 2 ||
  process.env.DEPOSITAR_ACCEPT_PRIVATE_RUNTIME !== "1"
)
  throw new Error(
    "Private runtime acceptance requires explicit opt-in and no arguments.",
  );

// Playwright removes outputDir before globalSetup and reloads its configuration
// inside workers. Check freshness here, before starting that lifecycle.
const output = process.env.DEPOSITAR_PRIVATE_RUNTIME_OUTPUT;
if (
  !output ||
  !isAbsolute(output) ||
  resolve(output) !== output ||
  lstatSync(output, { throwIfNoEntry: false })
)
  throw new Error("Acceptance output must be a new absolute directory.");

const result = spawnSync(
  process.execPath,
  [
    fileURLToPath(
      new URL("../node_modules/@playwright/test/cli.js", import.meta.url),
    ),
    "test",
    "--config",
    fileURLToPath(
      new URL("../playwright.private-runtime.config.ts", import.meta.url),
    ),
  ],
  {
    cwd: fileURLToPath(new URL("../", import.meta.url)),
    env: { ...process.env, DEPOSITAR_PRIVATE_RUNTIME_LAUNCHED: "1" },
    stdio: "inherit",
  },
);
process.exit(result.status ?? 1);
