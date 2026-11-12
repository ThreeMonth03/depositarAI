import { safeDatasetUrl } from "../api";
import type {
  Reply,
  SearchConditions,
  SearchState,
  TemporalCoverage,
} from "../api";

export const MAX_EXPORT_BYTES = 1024 * 1024;

type ExportItem = {
  rank: number;
  dataset_id: string;
  title: string;
  organization_title: string;
  url: string | null;
  resource_formats: string[];
  temporal_coverage: TemporalCoverage;
  condition_status: "match" | "unknown" | "not_checked";
  metadata_modified: string;
};

export type ResultExportSnapshot = {
  metadata: {
    schema_version: 1;
    scope: "displayed_top_k";
    turn_version: number;
    requested_top_k: number;
    result_count: number;
    state: SearchState;
    parsed_conditions: SearchConditions;
    applied_conditions: SearchConditions | null;
    filter_scope: "none" | "index_prefilter";
    warnings: string[];
    api_revision: string;
    bff_revision: string;
    catalog_manifest_sha256: string;
    catalog_observed_at: null;
    catalog_observation_status: "not_provided";
    query_context: {
      reference_time: string;
      reference_date: string;
      timezone: string;
    };
    received_at: string;
    received_at_source: "browser_clock";
  };
  items: ExportItem[];
};

export class ResultExportError extends Error {}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new ResultExportError("invalid_metadata");
  return value as Record<string, unknown>;
}

function text(value: unknown, maximum: number): string {
  if (typeof value !== "string" || value.length > maximum)
    throw new ResultExportError("invalid_metadata");
  return value;
}

function strings(value: unknown, maximum = 256): string[] {
  if (!Array.isArray(value) || value.length > maximum)
    throw new ResultExportError("invalid_metadata");
  return value.map((item) => text(item, 512));
}

function endpoint(value: unknown): string | null {
  return value === null ? null : text(value, 128);
}

function instant(value: unknown): string {
  const result = text(value, 40);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
      result,
    ) ||
    !Number.isFinite(Date.parse(result))
  )
    throw new ResultExportError("invalid_metadata");
  return result;
}

function conditions(value: unknown): SearchConditions {
  const source = record(value);
  if (source.place !== null) throw new ResultExportError("invalid_metadata");
  let time: SearchConditions["time"] = null;
  if (source.time !== null) {
    const range = record(source.time);
    if (
      range.field !== "data_coverage" ||
      range.relation !== "overlaps" ||
      range.endpoints !== "inclusive"
    )
      throw new ResultExportError("invalid_metadata");
    time = {
      start: endpoint(range.start),
      end: endpoint(range.end),
      field: "data_coverage",
      relation: "overlaps",
      endpoints: "inclusive",
    };
  }
  return { time, place: null, formats: strings(source.formats, 8) };
}

function state(value: SearchState): SearchState {
  return {
    topic: text(value.topic, 256),
    time:
      value.time === null
        ? null
        : {
            start: endpoint(value.time.start),
            end: endpoint(value.time.end),
          },
    formats: strings(value.formats, 8),
    ...(value.unapplied_time
      ? {
          unapplied_time: {
            calendar: "lunar" as const,
            text: text(value.unapplied_time.text, 512),
          },
        }
      : value.unapplied_time === null
        ? { unapplied_time: null }
        : {}),
  };
}

/** Project only an accepted turn's displayed public fields, never its extras. */
export function createResultExport(
  reply: Reply,
  requestedTopK: number,
  bffRevision: string,
  receivedAt: string,
): ResultExportSnapshot | null {
  const result = reply.result;
  if (result.status !== "ok" || !result.search) return null;
  try {
    const search = result.search;
    if (
      !Number.isInteger(requestedTopK) ||
      requestedTopK < 1 ||
      requestedTopK > 20 ||
      !Number.isInteger(reply.version) ||
      reply.version < 1 ||
      reply.version > 20 ||
      search.cards.length > requestedTopK ||
      !result.revision.trim() ||
      !bffRevision.trim() ||
      !/^[0-9a-f]{64}$/.test(search.catalog_manifest_sha256) ||
      !["none", "index_prefilter"].includes(search.filter_scope)
    )
      return null;
    const referenceDate = text(reply.context_used.reference_date, 10);
    const timezone = text(reply.context_used.timezone, 64);
    if (
      !/^\d{4}-\d{2}-\d{2}$/.test(referenceDate) ||
      !Number.isFinite(Date.parse(referenceDate)) ||
      !timezone.trim()
    )
      return null;
    const items = search.cards.map((card, index): ExportItem => {
      if (card.rank !== index + 1)
        throw new ResultExportError("invalid_metadata");
      return {
        rank: card.rank,
        dataset_id: text(card.dataset_id, 36),
        title: text(card.title, 512),
        organization_title: text(card.organization_title, 512),
        url: safeDatasetUrl(card.url) ?? null,
        resource_formats: strings(card.resource_formats),
        temporal_coverage: {
          start: endpoint(card.temporal_coverage.start),
          end: endpoint(card.temporal_coverage.end),
          status: card.temporal_coverage.status,
        },
        condition_status: card.condition_status,
        metadata_modified: text(card.metadata_modified, 128),
      };
    });
    return {
      metadata: {
        schema_version: 1,
        scope: "displayed_top_k",
        turn_version: reply.version,
        requested_top_k: requestedTopK,
        result_count: items.length,
        state: state(result.state),
        parsed_conditions: conditions(search.parsed_conditions),
        applied_conditions:
          search.applied_conditions === null
            ? null
            : conditions(search.applied_conditions),
        filter_scope: search.filter_scope,
        warnings: strings(search.warnings),
        api_revision: text(result.revision, 128),
        bff_revision: text(bffRevision, 128),
        catalog_manifest_sha256: search.catalog_manifest_sha256,
        catalog_observed_at: null,
        catalog_observation_status: "not_provided",
        query_context: {
          reference_time: instant(reply.context_used.reference_time),
          reference_date: referenceDate,
          timezone,
        },
        received_at: instant(receivedAt),
        received_at_source: "browser_clock",
      },
      items,
    };
  } catch {
    // Export-only metadata failure must not turn a valid search into an error.
    return null;
  }
}

const CSV_FIELDS = [
  "record_type",
  "metadata_json",
  "rank",
  "dataset_id",
  "title",
  "organization_title",
  "url",
  "formats_json",
  "coverage_status",
  "coverage_start",
  "coverage_end",
  "condition_status",
  "metadata_modified",
] as const;

function csvCell(value: string | number | null): string {
  const raw = value === null ? "" : String(value);
  const formula =
    /^[\s\p{Cc}\p{Cf}]*[=+\-@]/u.test(raw) || /^[\p{Cc}\p{Cf}]/u.test(raw);
  const safe = formula ? "'" + raw : raw;
  return '"' + safe.replaceAll('"', '""') + '"';
}

/** Serialize one bounded file; exported_at is distinct from source observation. */
export function serializeResultExport(
  snapshot: ResultExportSnapshot,
  format: "csv" | "json",
  exportedAt = new Date().toISOString(),
): { content: string; mime: string; filename: string } {
  if (
    snapshot.items.length > 20 ||
    snapshot.items.length > snapshot.metadata.requested_top_k ||
    snapshot.items.length !== snapshot.metadata.result_count
  )
    throw new ResultExportError("invalid_metadata");
  const metadata = {
    ...snapshot.metadata,
    exported_at: instant(exportedAt),
    exported_at_source: "browser_clock",
  };
  let content: string;
  if (format === "json") {
    content =
      JSON.stringify({ metadata, items: snapshot.items }, null, 2) + "\n";
  } else {
    const rows: (string | number | null)[][] = [
      [...CSV_FIELDS],
      ["metadata", JSON.stringify(metadata), ...Array(11).fill("")],
      ...snapshot.items.map((item) => [
        "dataset",
        "",
        item.rank,
        item.dataset_id,
        item.title,
        item.organization_title,
        item.url,
        JSON.stringify(item.resource_formats),
        item.temporal_coverage.status,
        item.temporal_coverage.start,
        item.temporal_coverage.end,
        item.condition_status,
        item.metadata_modified,
      ]),
    ];
    content =
      "\ufeff" +
      rows.map((row) => row.map(csvCell).join(",")).join("\r\n") +
      "\r\n";
  }
  if (new TextEncoder().encode(content).byteLength > MAX_EXPORT_BYTES)
    throw new ResultExportError("too_large");
  return {
    content,
    mime:
      format === "csv"
        ? "text/csv;charset=utf-8"
        : "application/json;charset=utf-8",
    filename: `depositar-results-${metadata.turn_version}-${metadata.exported_at.replaceAll(/[:.]/g, "-")}.${format}`,
  };
}
