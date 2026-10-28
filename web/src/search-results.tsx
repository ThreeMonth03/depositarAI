import { useState } from "react";
import { safeDatasetUrl } from "./api";
import type {
  Card,
  ResourceContext,
  SearchState,
  TemporalCoverage,
  TurnResult,
} from "./api";

function dateLabel(value: string): string {
  const [year, month, day] = value.split("-");
  return `${year} 年${month ? ` ${Number(month)} 月` : ""}${day ? ` ${Number(day)} 日` : ""}`;
}

function timeLabel({ start, end }: NonNullable<SearchState["time"]>): string {
  if (start === null)
    return end === null ? "不限時間" : `截至 ${dateLabel(end)}`;
  const startLabel = dateLabel(start);
  if (end === null) return `${startLabel}起`;
  const endLabel = dateLabel(end);
  if (startLabel === endLabel) return startLabel;
  if (start.length === 4 && end.length === 4) return `${start}～${end} 年`;
  return `${startLabel}～${endLabel}`;
}

function coverageLabel({ start, end, status }: TemporalCoverage): string {
  if (status === "absent") return "未提供";
  if (status === "invalid") return "日期資訊無法判讀";
  if (status === "partial")
    return `${start === null ? "起始未提供" : dateLabel(start)}～${end === null ? "結束未提供" : dateLabel(end)}`;
  return timeLabel({ start, end });
}

function Conditions({ state }: { state: SearchState }) {
  return (
    <div className="chips" aria-label="本次搜尋條件">
      <span className="chip topic">{state.topic}</span>
      {state.time && <span className="chip">{timeLabel(state.time)}</span>}
      {state.unapplied_time && (
        <span className="chip">{state.unapplied_time.text}（僅文字搜尋）</span>
      )}
      {state.formats.length > 0 && (
        <span className="chip">{state.formats.join(" / ")}</span>
      )}
    </div>
  );
}

function ResourcePreview({ resource }: { resource: ResourceContext }) {
  const url = safeDatasetUrl(resource.url);
  return (
    <div className="resource-context">
      <div className="resource-name">
        <span>{resource.format}</span>
        {url ? (
          <a
            href={url}
            title={resource.name}
            target="_blank"
            rel="noopener noreferrer"
          >
            {resource.name.trim() || "查看檔案來源"}{" "}
            <span aria-hidden="true">↗</span>
          </a>
        ) : (
          resource.name
        )}
      </div>
      {resource.format === "CSV" ? (
        <details>
          <summary>CSV 欄位（節錄）</summary>
          <div
            className="resource-columns"
            tabIndex={0}
            aria-label="CSV 欄位節錄"
          >
            {resource.columns.map((column, index) => (
              <code key={index}>{column}</code>
            ))}
          </div>
        </details>
      ) : (
        <p>
          <strong>檔案摘錄：</strong>
          {resource.excerpt}
        </p>
      )}
    </div>
  );
}

function ResultCard({ card, rank }: { card: Card; rank: number }) {
  const [expanded, setExpanded] = useState(false);
  const url = safeDatasetUrl(card.url);
  const collapsible = card.snippet.length > 120;
  return (
    <article className="result-card">
      <div className="card-eyebrow">
        <span>{card.organization_title || "depositar"}</span>
        <span className="card-rank">{String(rank).padStart(2, "0")}</span>
      </div>
      <h3>
        {url ? (
          <a href={url} target="_blank" rel="noopener noreferrer">
            {card.title}
            <span aria-hidden="true">↗</span>
          </a>
        ) : (
          card.title
        )}
      </h3>
      <p className={collapsible && !expanded ? "excerpt clamped" : "excerpt"}>
        {card.snippet}
      </p>
      {collapsible && (
        <button
          className="text-button"
          aria-expanded={expanded}
          aria-label={`${expanded ? "收合" : "展開"}「${card.title}」的描述`}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收合描述" : "展開描述"}
        </button>
      )}
      {card.resource_context.length > 0 && (
        <div className="resource-contexts" aria-label="檔案內容節錄">
          {card.resource_context.map((resource) => (
            <ResourcePreview key={resource.resource_id} resource={resource} />
          ))}
        </div>
      )}
      <div className="card-foot">
        <div className="format-tags" aria-label="資料集資訊">
          <span>資料涵蓋：{coverageLabel(card.temporal_coverage)}</span>
          {card.resource_formats.map((format) => (
            <span key={format}>{format}</span>
          ))}
        </div>
        {card.condition_status === "unknown" && (
          <span
            className="unknown"
            title="資料的時間或格式資訊不完整，尚無法確認所有搜尋條件。"
          >
            部分條件待確認
          </span>
        )}
        {url && (
          <a
            className="source-link"
            href={url}
            target="_blank"
            rel="noopener noreferrer"
          >
            查看資料集 <span aria-hidden="true">↗</span>
          </a>
        )}
      </div>
    </article>
  );
}

export function Results({ result }: { result: TurnResult }) {
  if (!result.search) return null;
  return (
    <div className="results">
      <Conditions state={result.state} />
      {result.notices?.map((notice) => (
        <p className="calendar-note" key={notice}>
          {notice}
        </p>
      ))}
      <div className="result-note">
        <span>
          資料集{" "}
          <span className="result-count">{result.search.cards.length}</span>
        </span>
        <span>{(result.timing_ms.total / 1000).toFixed(1)} 秒</span>
      </div>
      {result.search.cards.length === 0 && (
        <p className="notice">試著放寬年份、移除格式限制，或換個主題再找找。</p>
      )}
      {result.search.cards.map((card, index) => (
        <ResultCard key={card.dataset_id} card={card} rank={index + 1} />
      ))}
      <details className="diagnostics">
        <summary>搜尋詳情</summary>
        <p>
          資料來源：公開 metadata 快照，以及已建入索引的 CSV 欄位與 PDF
          文字節錄。
        </p>
        <p>
          地名僅作文字比對，未啟用地理篩選。日期指資料涵蓋範圍，不是上架日期。
        </p>
        <p>
          API 版本：<code>{result.revision}</code>
        </p>
        <p>
          快照：<code>{result.search.catalog_manifest_sha256}</code>
        </p>
        {result.search.warnings.length > 0 && (
          <p>診斷：{result.search.warnings.join(", ")}</p>
        )}
      </details>
    </div>
  );
}
