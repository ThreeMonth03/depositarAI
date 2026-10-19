import { expect, test as base } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFile, rename, writeFile } from "node:fs/promises";
import { join } from "node:path";

export { expect } from "@playwright/test";

export const test = base.extend<{ isolatedGateway: void }>({
  isolatedGateway: [
    async ({}, use, testInfo) => {
      expect(testInfo.config.workers).toBe(1);
      const control = process.env.BROWSER_GATEWAY_CONTROL;
      if (!control)
        throw new Error("Run bash scripts/test-search-web.sh --suite browser");
      const token = randomUUID();
      await writeFile(join(control, "request.tmp"), token);
      await rename(join(control, "request.tmp"), join(control, "request"));
      await expect
        .poll(
          async () => {
            const failed = await readFile(
              join(control, "failed"),
              "utf8",
            ).catch(() => "");
            if (failed)
              throw new Error(`Browser gateway supervisor exited (${failed})`);
            return readFile(join(control, "ready"), "utf8").catch(() => "");
          },
          { timeout: 10_000, intervals: [20, 50, 100] },
        )
        .toBe(token);
      await use();
    },
    { auto: true },
  ],
});
