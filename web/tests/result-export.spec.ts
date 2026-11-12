import { readFile, writeFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import type { Page, TestInfo } from "@playwright/test";
import type { Access, Reply } from "../src/api";
import {
  createResultExport,
  MAX_EXPORT_BYTES,
  serializeResultExport,
} from "../src/search/export";
import { privateAccess, userAccess } from "./access-fixture";

const RECEIVED = "2026-11-12T08:00:01.000Z";
const EXPORTED = "2026-11-12T08:01:02.000Z";

function reply(count = 2, version = 1): Reply {
  const conditions = {
    time: {
      start: "2024",
      end: "2024",
      field: "data_coverage" as const,
      relation: "overlaps" as const,
      endpoints: "inclusive" as const,
    },
    place: null,
    formats: ["CSV"],
  };
  return {
    version,
    context_used: {
      reference_time: "2026-11-12T08:00:00+00:00",
      reference_date: "2026-11-12",
      timezone: "Asia/Taipei",
    },
    result: {
      status: "ok",
      message: "搜尋完成",
      reason: "none",
      revision: "reported-api",
      timing_ms: { total: 10 },
      state: {
        topic: "氣候",
        time: { start: "2024", end: "2024" },
        formats: ["CSV"],
        unapplied_time: null,
      },
      search: {
        cards: Array.from({ length: count }, (_, index) => {
          const id = `${String(index + 1).padStart(8, "0")}-1111-4111-8111-111111111111`;
          return {
            rank: index + 1,
            dataset_id: id,
            title: `氣候資料 ${index + 1}，"引號"\n第二行`,
            url: `https://data.depositar.io/dataset/${id}`,
            snippet: "not exported: snippet sentinel",
            organization_title: index === 0 ? "研究團隊" : "",
            resource_formats: ["CSV", "PDF"],
            resource_context: [],
            temporal_coverage:
              index === 0
                ? { start: "2020", end: "2025", status: "complete" as const }
                : { start: null, end: null, status: "absent" as const },
            metadata_modified: "2025-01-01T00:00:00Z",
            condition_status:
              index === 0 ? ("match" as const) : ("unknown" as const),
          };
        }),
        filter_scope: "index_prefilter",
        warnings: ["unknown_condition_results_retained"],
        catalog_manifest_sha256: "a".repeat(64),
        parsed_conditions: conditions,
        applied_conditions: structuredClone(conditions),
      },
    },
  };
}

function snapshot(input = reply(), topK = 5) {
  const result = createResultExport(input, topK, "reported-bff", RECEIVED);
  expect(result).not.toBeNull();
  return result!;
}

/** Read ordinary quoted/unquoted CSV, independently of the export writer. */
function csvRows(content: string): string[][] {
  const source = content.replace(/^\ufeff/, "");
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < source.length; index++) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') {
        cell += '"';
        index++;
      } else quoted = !quoted;
    } else if (!quoted && character === ",") {
      row.push(cell);
      cell = "";
    } else if (!quoted && (character === "\r" || character === "\n")) {
      if (character === "\r" && source[index + 1] === "\n") index++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += character;
  }
  expect(quoted).toBe(false);
  if (cell || row.length) rows.push([...row, cell]);
  return rows;
}

test("export projects one reply deeply and excludes unapproved fields", () => {
  const input = reply();
  const expected = structuredClone(input);
  const wire = {
    ...input,
    token: "forbidden-token",
    principal: "forbidden-principal",
    headers: {
      authorization: "forbidden-header",
      cookie: "forbidden-cookie",
      "x-api-key": "forbidden-api-key",
    },
    trace: { test_session_id: "forbidden-trace" },
    context_used: { ...input.context_used, extra: "forbidden-context" },
    result: {
      ...input.result,
      internal: "forbidden-result",
      state: { ...input.result.state, extra: "forbidden-state" },
      search: {
        ...input.result.search!,
        private_metadata: "forbidden-search",
        parsed_conditions: {
          ...input.result.search!.parsed_conditions!,
          extra: "forbidden-parsed",
          time: {
            ...input.result.search!.parsed_conditions!.time!,
            extra: "forbidden-time",
          },
        },
        applied_conditions: {
          ...input.result.search!.applied_conditions!,
          extra: "forbidden-applied",
        },
        cards: input.result.search!.cards.map((card) => ({
          ...card,
          snippet: "forbidden-snippet",
          resource_context: [{ excerpt: "forbidden-excerpt" }],
          internal: "forbidden-card",
          temporal_coverage: {
            ...card.temporal_coverage,
            extra: "forbidden-coverage",
          },
        })),
      },
    },
  } as unknown as Reply;
  const saved = snapshot(wire);
  wire.result.state.topic = "later mutation";
  wire.result.search!.cards[0].title = "later title";
  wire.result.search!.parsed_conditions!.formats.push("JSON");
  const file = serializeResultExport(saved, "json", EXPORTED);
  const data = JSON.parse(file.content);
  expect(file.content).not.toContain("forbidden-");
  expect(file.content).not.toContain("later");
  expect(data.metadata.state).toEqual(expected.result.state);
  expect(data.metadata.parsed_conditions).toEqual(
    expected.result.search!.parsed_conditions,
  );
  expect(data.metadata.query_context).toEqual(expected.context_used);
  expect(data.metadata.api_revision).toBe("reported-api");
  expect(data.metadata.bff_revision).toBe("reported-bff");
  expect(data.metadata.catalog_observed_at).toBeNull();
  expect(data.metadata.catalog_observation_status).toBe("not_provided");
  expect(data.metadata.received_at).toBe(RECEIVED);
  expect(data.metadata.exported_at).toBe(EXPORTED);
  expect(data.items[1].organization_title).toBe("");
  expect(data.items[1].temporal_coverage).toEqual({
    start: null,
    end: null,
    status: "absent",
  });
});

for (const status of [
  "rejected",
  "clarification_required",
  "unavailable",
] as const)
  test(`export refuses ${status} even if cards are present`, () => {
    const input = reply();
    input.result.status = status;
    expect(createResultExport(input, 5, "bff", RECEIVED)).toBeNull();
  });

test("empty results retain metadata in both ordinary CSV and JSON", () => {
  const saved = snapshot(reply(0));
  const json = JSON.parse(
    serializeResultExport(saved, "json", EXPORTED).content,
  );
  const csv = csvRows(serializeResultExport(saved, "csv", EXPORTED).content);
  expect(json.items).toEqual([]);
  expect(json.metadata.result_count).toBe(0);
  expect(csv).toHaveLength(2);
  expect(csv[0]).toHaveLength(13);
  expect(csv[1]).toHaveLength(13);
  expect(csv[1][0]).toBe("metadata");
  expect(JSON.parse(csv[1][1])).toEqual(json.metadata);
});

test("export-only guards reject missing identities, bounds and ambiguous ranks", () => {
  const cases: Reply[] = [];
  for (const field of ["rank", "metadata_modified"] as const) {
    const input = reply();
    delete input.result.search!.cards[0][field];
    cases.push(input);
  }
  const mismatched = reply();
  mismatched.result.search!.cards[0].rank = 2;
  cases.push(mismatched);
  const missingConditions = reply();
  delete missingConditions.result.search!.parsed_conditions;
  cases.push(missingConditions);
  const missingClock = reply();
  delete missingClock.context_used.reference_time;
  cases.push(missingClock);
  const missingManifest = reply();
  missingManifest.result.search!.catalog_manifest_sha256 = "";
  cases.push(missingManifest, reply(21, 1), reply(1, 21));
  for (const input of cases)
    expect(createResultExport(input, 20, "bff", RECEIVED)).toBeNull();
  expect(createResultExport(reply(), 21, "bff", RECEIVED)).toBeNull();
  expect(createResultExport(reply(), 0, "bff", RECEIVED)).toBeNull();
  expect(createResultExport(reply(), 5, "", RECEIVED)).toBeNull();
  expect(snapshot(reply(20), 20).items).toHaveLength(20);
});

test("unsafe URLs stay absent while JSON text values remain unchanged", () => {
  const input = reply();
  input.result.search!.cards[0].url = "javascript:forbidden-url";
  const saved = snapshot(input);
  const file = serializeResultExport(saved, "json", EXPORTED);
  expect(saved.items[0].url).toBeNull();
  expect(file.content).not.toContain("javascript:");
  expect(JSON.parse(file.content).items[0].title).toBe(
    input.result.search!.cards[0].title,
  );
});

for (const value of [
  "=1+1",
  "+1",
  "-1",
  "@SUM(A1)",
  "\ttext",
  "\rtext",
  "\ntext",
  " \t=1",
  "\u200b=1",
  "\0+1",
  '中文，"引號"\n第二行',
])
  test(`CSV formula text policy and JSON original: ${JSON.stringify(value)}`, () => {
    const saved = snapshot(reply(1));
    const item = saved.items[0];
    item.title = value;
    item.organization_title = value;
    item.dataset_id = value;
    item.metadata_modified = value;
    item.temporal_coverage = { start: value, end: value, status: "invalid" };
    const csv = csvRows(serializeResultExport(saved, "csv", EXPORTED).content);
    const expected = value.startsWith("中文") ? value : "'" + value;
    for (const index of [3, 4, 5, 9, 10, 12])
      expect(csv[2][index]).toBe(expected);
    const json = JSON.parse(
      serializeResultExport(saved, "json", EXPORTED).content,
    );
    expect(json.items[0].title).toBe(value);
    expect(json.items[0].temporal_coverage.start).toBe(value);
    expect(JSON.parse(csv[2][7])).toEqual(["CSV", "PDF"]);
  });

for (const format of ["json", "csv"] as const)
  test(`${format} measures final UTF-8 bytes, including CSV BOM and quoting`, () => {
    const saved = snapshot(reply(1));
    saved.items[0].title = "";
    const base = Buffer.byteLength(
      serializeResultExport(saved, format, EXPORTED).content,
    );
    const remaining = MAX_EXPORT_BYTES - base;
    saved.items[0].title =
      format === "csv"
        ? '"'.repeat(Math.floor(remaining / 2)) + "x".repeat(remaining % 2)
        : "x".repeat(remaining);
    const boundary = serializeResultExport(saved, format, EXPORTED);
    expect(Buffer.byteLength(boundary.content)).toBe(MAX_EXPORT_BYTES);
    saved.items[0].title += "x";
    expect(() => serializeResultExport(saved, format, EXPORTED)).toThrow(
      "too_large",
    );
    saved.items[0].title = "漢".repeat(Math.ceil(MAX_EXPORT_BYTES / 3));
    expect(() => serializeResultExport(saved, format, EXPORTED)).toThrow(
      "too_large",
    );
  });

type MockOptions = {
  count?: number;
  expires?: number;
  failReset?: boolean;
  access?: () => Access;
  turn?: (
    value: Reply,
    body: { query: string; version: number },
  ) => Reply | Promise<Reply>;
};

async function mockWebsite(page: Page, options: MockOptions = {}) {
  const calls: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    calls.push(`${request.method()} ${path}`);
    if (path === "/api/access")
      return route.fulfill({ json: options.access?.() ?? privateAccess });
    if (path === "/api/session") {
      if (request.method() === "DELETE")
        return route.fulfill({
          status: options.failReset ? 503 : 200,
          json: { status: "cleared" },
        });
      return route.fulfill({
        json: {
          token: "forbidden-session-token",
          version: 0,
          expires_in: options.expires ?? 1800,
          max_turns: 20,
          revision: "reported-bff",
        },
      });
    }
    const body = request.postDataJSON();
    let value = reply(options.count ?? 2, body.version + 1);
    value = await (options.turn?.(value, body) ?? value);
    return route.fulfill({ json: value });
  });
  return calls;
}

async function ask(page: Page, query = "raw-prompt-sentinel 氣候") {
  await page.getByRole("textbox", { name: "搜尋或追問" }).fill(query);
  await page.getByRole("button", { name: "送出搜尋" }).click();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
}

async function download(page: Page, format: "CSV" | "JSON", info?: TestInfo) {
  const pending = page.waitForEvent("download");
  await page.getByRole("button", { name: `下載清單 ${format}` }).click();
  const file = await pending;
  expect(file.suggestedFilename()).toMatch(
    /^depositar-results-\d+-[\dTZ-]+\.(csv|json)$/,
  );
  if (info)
    await file.saveAs(info.outputPath(`results.${format.toLowerCase()}`));
  return readFile((await file.path())!, "utf8");
}

test("browser downloads the displayed snapshot without API, storage or link fetches", async ({
  page,
}, info) => {
  const network: string[] = [];
  page.on("request", (request) => {
    if (/^https?:/.test(request.url())) network.push(request.url());
  });
  const calls = await mockWebsite(page);
  await page.goto("/");
  await ask(page);
  const before = [...calls];
  const beforeNetwork = [...network];
  const json = await download(page, "JSON", info);
  const data = JSON.parse(json);
  const csv = csvRows(await download(page, "CSV", info));
  await writeFile(
    info.outputPath("expected-reply.json"),
    JSON.stringify(reply()),
  );
  expect(calls).toEqual(before);
  expect(network).toEqual(beforeNetwork);
  expect(json).not.toContain("forbidden-session-token");
  expect(json).not.toContain("raw-prompt-sentinel");
  expect(json).not.toContain("snippet sentinel");
  expect(data.items.map((item: { title: string }) => item.title)).toEqual(
    await page
      .locator(".result-card h3")
      .allTextContents()
      .then((titles) => titles.map((title) => title.replace(/↗$/, "").trim())),
  );
  expect(csv.slice(2).map((row) => row[3])).toEqual(
    data.items.map((item: { dataset_id: string }) => item.dataset_id),
  );
  expect(data.items[1].temporal_coverage.start).toBeNull();
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
});

test("selector changes and refinements cannot rewrite a prior accepted snapshot", async ({
  page,
}) => {
  const calls = await mockWebsite(page, {
    turn: (value, body) => {
      if (body.version > 0) {
        value.result.state.time = { start: "2023", end: "2023" };
        value.result.search!.catalog_manifest_sha256 = "b".repeat(64);
      }
      return value;
    },
  });
  await page.goto("/");
  await ask(page);
  await page.getByLabel("結果數").selectOption("20");
  const first = JSON.parse(await download(page, "JSON"));
  expect(first.metadata.requested_top_k).toBe(5);
  expect(first.metadata.turn_version).toBe(1);
  expect(first.metadata.state.time.start).toBe("2024");
  await ask(page, "改查2023");
  const second = JSON.parse(await download(page, "JSON"));
  expect(second.metadata.requested_top_k).toBe(20);
  expect(second.metadata.turn_version).toBe(2);
  expect(second.metadata.catalog_manifest_sha256).toBe("b".repeat(64));
  expect(second.metadata.state.time.start).toBe("2023");
  await expect(page.getByRole("button", { name: "下載清單 JSON" })).toHaveCount(
    1,
  );
  expect(calls.filter((call) => call === "POST /api/turn")).toHaveLength(2);
});

test("zero-result browser files retain metadata and malformed export fields only disable download", async ({
  page,
}) => {
  await mockWebsite(page, {
    count: 0,
    turn: (value, body) => {
      if (body.version > 0) delete value.result.search!.parsed_conditions;
      return value;
    },
  });
  await page.goto("/");
  await ask(page);
  expect(JSON.parse(await download(page, "JSON")).items).toEqual([]);
  expect(csvRows(await download(page, "CSV"))).toHaveLength(2);
  await ask(page, "再搜尋");
  await expect(
    page.getByRole("button", { name: "下載清單 JSON" }),
  ).toBeDisabled();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  await expect(page.locator(".results").last()).toContainText("資料集 0");
});

test("a rejected accepted reply cannot fall back to the previous export", async ({
  page,
}) => {
  await mockWebsite(page, {
    turn: (value, body) => {
      if (body.version > 0) {
        value.result.status = "rejected";
        value.result.search = null;
        value.result.message = "請改述資料集需求";
      }
      return value;
    },
  });
  await page.goto("/");
  await ask(page);
  await expect(
    page.getByRole("button", { name: "下載清單 JSON" }),
  ).toBeEnabled();
  await ask(page, "unsupported request");
  await expect(
    page.getByRole("button", { name: "下載清單 JSON" }),
  ).toBeDisabled();
});

test("failed reset preserves the old session but successful reset removes exports", async ({
  page,
}) => {
  await mockWebsite(page, { failReset: true });
  await page.goto("/");
  await ask(page);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(
    page.getByText("無法確認新搜尋是否完成，請重試。原有對話暫時保留。"),
  ).toBeVisible();
  expect(JSON.parse(await download(page, "JSON")).metadata.turn_version).toBe(
    1,
  );
  await page.unroute("**/api/**");
  await mockWebsite(page);
  await page.getByRole("button", { name: "新搜尋" }).click();
  await expect(page.getByRole("button", { name: "下載清單 JSON" })).toHaveCount(
    0,
  );
});

test("BFCache clears the live click guard and late replies cannot restore exports", async ({
  page,
}) => {
  let release: (() => void) | undefined;
  let started: (() => void) | undefined;
  const waiting = new Promise<void>((resolve) => {
    started = resolve;
  });
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await mockWebsite(page, {
    turn: async (value, body) => {
      if (body.version > 0) {
        started!();
        await gate;
      }
      return value;
    },
  });
  await page.addInitScript(() => {
    const original = URL.createObjectURL.bind(URL);
    (window as unknown as { exportBlobs: number }).exportBlobs = 0;
    URL.createObjectURL = (blob) => {
      (window as unknown as { exportBlobs: number }).exportBlobs++;
      return original(blob);
    };
  });
  await page.goto("/");
  await ask(page);
  await page
    .getByRole("textbox", { name: "搜尋或追問" })
    .fill("late refinement");
  await page.getByRole("button", { name: "送出搜尋" }).click();
  await waiting;
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find(
      (item) => item.textContent === "下載清單 JSON",
    )!;
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    );
    button.click();
  });
  release!();
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "下載清單 JSON" })).toHaveCount(
    0,
  );
  expect(
    await page.evaluate(
      () => (window as unknown as { exportBlobs: number }).exportBlobs,
    ),
  ).toBe(0);
});

test("expiry clears export state and each successful Blob URL is revoked once", async ({
  page,
}) => {
  await page.clock.install();
  await mockWebsite(page, { expires: 5 });
  await page.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL);
    const revoke = URL.revokeObjectURL.bind(URL);
    const events = { created: [] as string[], revoked: [] as string[] };
    (window as unknown as { exportUrls: typeof events }).exportUrls = events;
    URL.createObjectURL = (blob) => {
      const url = create(blob);
      events.created.push(url);
      return url;
    };
    URL.revokeObjectURL = (url) => {
      events.revoked.push(url);
      revoke(url);
    };
  });
  await page.goto("/");
  await ask(page);
  await download(page, "JSON");
  await page.clock.fastForward(100);
  expect(
    await page.evaluate(() => {
      const events = (
        window as unknown as {
          exportUrls: { created: string[]; revoked: string[] };
        }
      ).exportUrls;
      return (
        events.created.length === 1 &&
        JSON.stringify(events.created) === JSON.stringify(events.revoked)
      );
    }),
  ).toBe(true);
  await page.clock.fastForward(5000);
  await expect(page.getByRole("button", { name: "下載清單 JSON" })).toHaveCount(
    0,
  );
});

test("owner restoration clears a previously authorized export", async ({
  page,
}) => {
  let access: Access = privateAccess;
  await mockWebsite(page, { access: () => access });
  await page.goto("/");
  await ask(page);
  access = userAccess;
  await page.evaluate(() =>
    window.dispatchEvent(
      new PageTransitionEvent("pageshow", { persisted: true }),
    ),
  );
  await expect(page.getByRole("button", { name: "下載清單 JSON" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("textbox", { name: "搜尋或追問" })).toBeEnabled();
});

test("real BFF reply, displayed cards and both downloaded files agree without extra API", async ({
  page,
}, info) => {
  const requests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/")) requests.push(`${request.method()} ${path}`);
  });
  await page.goto("/");
  const response = page.waitForResponse(
    (item) =>
      item.url().endsWith("/api/turn") && item.request().method() === "POST",
  );
  await ask(page, "找2024年的氣候資料 CSV");
  const actual = await (await response).json();
  await expect(
    page.getByRole("button", { name: "下載清單 JSON" }),
  ).toBeEnabled();
  const before = [...requests];
  const json = JSON.parse(await download(page, "JSON", info));
  const csv = csvRows(await download(page, "CSV", info));
  await writeFile(
    info.outputPath("actual-turn.json"),
    JSON.stringify(actual, null, 2),
  );
  expect(requests).toEqual(before);
  const cards = actual.result.search.cards;
  expect(
    json.items.map((item: { dataset_id: string }) => item.dataset_id),
  ).toEqual(cards.map((card: { dataset_id: string }) => card.dataset_id));
  expect(csv.slice(2).map((row) => row[3])).toEqual(
    cards.map((card: { dataset_id: string }) => card.dataset_id),
  );
  expect(json.metadata.turn_version).toBe(actual.version);
  expect(json.metadata.state).toEqual(actual.result.state);
  expect(json.metadata.parsed_conditions).toEqual(
    actual.result.search.parsed_conditions,
  );
  expect(json.metadata.applied_conditions).toEqual(
    actual.result.search.applied_conditions,
  );
  expect(json.metadata.catalog_manifest_sha256).toBe(
    actual.result.search.catalog_manifest_sha256,
  );
  expect(json.metadata.api_revision).toBe(actual.result.revision);
  expect(json.metadata.query_context).toEqual({
    reference_time: actual.context_used.reference_time,
    reference_date: actual.context_used.reference_date,
    timezone: actual.context_used.timezone,
  });
  expect(json.items).toHaveLength(
    await page.locator(".result-card:visible").count(),
  );
  expect(requests.filter((entry) => entry === "POST /api/turn")).toHaveLength(
    1,
  );
});
