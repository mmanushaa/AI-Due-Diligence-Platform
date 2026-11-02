import { useState, useEffect } from "react";
import type { FindingList } from "../lib/api";

interface Props {
  auditId: string;
}

const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];

const SEVERITY_COLORS: Record<string, string> = {
  critical: "#b91c1c",
  high: "#c2410c",
  medium: "#b45309",
  low: "#a5a5a5",
  info: "#3b82f6",
};

export default function FindingsPage({ auditId }: Props) {
  const [data, setData] = useState<FindingList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState("");
  const [severity, setSeverity] = useState("");
  const [status, setStatus] = useState("");
  const [file, setFile] = useState("");  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    const run = async () => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (category) params.set("category", category);
        if (severity) params.set("severity", severity);
        if (status) params.set("status", status);
        if (file) params.set("file", file);

        const res = await fetch(
          `/api/v1/audits/${auditId}/findings?${params.toString()}`,
          { signal: controller.signal }
        );
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const json: FindingList = await res.json();
        if (!cancelled) setData(json);
      } catch (e) {
        if (!cancelled && e instanceof Error && e.name !== "AbortError") {
          setError(e.message);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [auditId, category, severity, status, file]);

  if (loading && !data) return <PageLoading />;
  if (error) return <ErrorBox message={error} />;
  if (!data) return <Empty />;

  const byCategory = data.grouped ?? {};
  const categories = Object.keys(byCategory).sort();
  const severities = [...SEVERITY_ORDER].filter((s) => data.summary.by_severity[s] > 0);

  return (
    <div className="page">
      <header className="page-header">
        <h1>Findings</h1>
        <p className="muted">
          {data.summary.total} finding{data.summary.total === 1 ? "" : "s"} for audit{" "}
          <a href={`/reports/${auditId}`}>{auditId}</a>
        </p>
      </header>

      <Filters
        category={category}
        severity={severity}
        file={file}
        categories={categories}
        severities={severities}
        onCategoryChange={setCategory}
        onSeverityChange={setSeverity}
        onStatusChange={setStatus}
        onFileChange={setFile}
      />

      <SummaryCards summary={data.summary} />

      {data.findings.length === 0 ? (
        <Empty matchFilters />
      ) : (
        <table className="findings-table">
          <thead>
            <tr>
              <th>Category</th>
              <th>Severity</th>
              <th>Status</th>
              <th>Title</th>
              <th>File</th>
              <th>Lines</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {data.findings.map((f) => (
              <tr key={f.id}>
                <td>
                  <Badge category={f.category} />
                </td>
                <td>
                  <Badge severity={f.severity} />
                </td>
                <td>
                  <Badge severity={f.status} kind="status" />
                </td>
                <td className="title-cell">{f.title}</td>
                <td>
                  <FileLink file={f.file_path} line={f.line_start} />
                </td>
                <td>
                  <span className="lines-cell">
                    {String(f.line_start ?? "-").toString()}-{String(f.line_end ?? "-").toString()}
                  </span>
                </td>
                <td className="muted">{f.source_tool ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {categories.length > 1 && (
        <section className="grouped">
          <h2>Grouped by category and severity</h2>
          {categories.map((cat) => (
            <Group key={cat} category={cat} severities={byCategory[cat]} />
          ))}
        </section>
      )}
    </div>
  );
}

function PageLoading() {
  return (
    <div className="page">
      <div className="loading">Loading…</div>
    </div>
  );
}

function ErrorBox({ message }: { message: string }) {
  return (
    <div className="page">
      <div className="error">Error: {message}</div>
    </div>
  );
}

function Empty({ matchFilters }: { matchFilters?: boolean }) {
  return (
    <div className="page">
      <div className="empty">
        {matchFilters ? "No findings match the current filters." : "No findings yet."}
      </div>
    </div>
  );
}

function Filters({
  category,
  severity,
  file,
  categories,
  severities,
  onCategoryChange,
  onSeverityChange,
  onStatusChange,
  onFileChange,
}: {
  category: string;
  severity: string;
  file: string;
  categories: string[];
  severities: string[];
  onCategoryChange: (v: string) => void;
  onSeverityChange: (v: string) => void;
  onStatusChange: (v: string) => void;
  onFileChange: (v: string) => void;
}) {
  return (
    <fieldset className="filters">
      <legend>Filters</legend>
      <select
        className="filter-input"
        value={category}
        onChange={(e) => onCategoryChange(e.target.value)}
      >
        <option value="">All categories</option>
        {categories.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      <select
        className="filter-input"
        value={severity}
        onChange={(e) => onSeverityChange(e.target.value)}
      >
        <option value="">All severities</option>
        {severities.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <input
        className="filter-input"
        type="text"
        placeholder="file path"
        value={file}
        onChange={(e) => onFileChange(e.target.value)}
      />
      <button className="filter-clear" type="button" onClick={() => {
        onCategoryChange("");
        onSeverityChange("");
        onStatusChange("");
        onFileChange("");
      }}>
        Clear
      </button>
    </fieldset>
  );
}

function SummaryCards({ summary }: { summary: FindingList["summary"] }) {
  return (
    <div className="summary-cards">
      <div className="card">
        <div className="label">Findings</div>
        <div className="value">{summary.total}</div>
      </div>
      <div className="card">
        <div className="label">Categories</div>
        <div className="value">{Object.keys(summary.by_category).length}</div>
      </div>
      <div className="card">
        <div className="label">Severities</div>
        <div className="value">{Object.keys(summary.by_severity).length}</div>
      </div>
      <div className="card">
        <div className="label">High / Critical</div>
        <div className="value">{summary.high_critical}</div>
      </div>
    </div>
  );
}

function Badge({
  category,
  severity,
  kind = "severity",
}: {
  category?: string;
  severity?: string;
  kind?: "severity" | "status" | "category";
}) {
  const label = category ?? severity ?? "";
  if (!label) return <span />;
  let color: string;
  if (kind === "status") {
    color =
      label === "verified"
        ? "#16a34a"
        : label === "rejected_false_positive"
        ? "#a5a5a5"
        : label === "scanner_confirmed"
        ? "#b45309"
        : "#6b7280";
  } else if (kind === "category") {
    color = "#3b82f6";
  } else {
    color = SEVERITY_COLORS[label.toLowerCase()] ?? "#6b7280";
  }
  return (
    <span className="badge" style={{ backgroundColor: color }}>
      {label}
    </span>
  );
}

function FileLink({ file, line }: { file?: string | null; line?: number | null }) {
  if (!file) return <span className="muted" />;
  const href = !line
    ? `#${encodeURIComponent(file)}`
    : `#${encodeURIComponent(file)}:${line}`;
  return (
    <a href={href} className="file-link" title={file} target="_blank" rel="noreferrer">
      {file}
    </a>
  );
}

function Group({ category, severities }: { category: string; severities: Record<string, string[]> }) {
  const sevList = Object.keys(severities)
    .filter((s) => severities[s].length > 0)
    .sort((a, b) => SEVERITY_ORDER.indexOf(a) - SEVERITY_ORDER.indexOf(b));

  if (sevList.length === 0) return null;

  return (
    <div className="group">
      <h3 className="group-title">
        <Badge category={category} kind="category" />
      </h3>
      <table>
        <thead>
          <tr>
            <th>Severity</th>
            <th>Count</th>
          </tr>
        </thead>
        <tbody>
          {sevList.map((sev) => (
            <tr key={sev}>
              <td>
                <Badge severity={sev} />
              </td>
              <td>{severities[sev].length}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
