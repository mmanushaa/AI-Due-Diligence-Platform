import type { FindingFilters } from "../lib/api";

interface Props {
  filters: FindingFilters;
  counts: {
    categories: Record<string, number>;
    severities: Record<string, number>;
    statuses: Record<string, number>;
  };
  total: number;
  onChange: (next: FindingFilters) => void;
}

const SEVERITIES = ["critical", "high", "medium", "low", "info"];
const STATUSES = [
  "scanner_confirmed",
  "ai_suggested",
  "verified",
  "unresolved",
  "rejected_false_positive",
];

function label(value: string): string {
  return value.replaceAll("_", " ");
}

/** Single-select filters, each with its count, mirrored into the URL by the page.
 *  The backend filters one value per field; the rail says so rather than pretending
 *  to be multi-select. */
export default function FilterRail({ filters, counts, total, onChange }: Props) {
  function choose(field: keyof FindingFilters, value: string) {
    const next = { ...filters };
    if (!value || filters[field] === value) delete next[field];
    else next[field] = value;
    onChange(next);
  }

  return (
    <aside className="filter-rail" aria-label="Finding filters">
      <div className="rail-head">
        <h2>Filters</h2>
        {Object.keys(filters).length > 0 ? (
          <button type="button" className="button ghost small" onClick={() => onChange({})}>
            Clear all
          </button>
        ) : null}
      </div>

      <fieldset>
        <legend>Severity</legend>
        <button
          type="button"
          className={`rail-option${!filters.severity ? " selected" : ""}`}
          aria-pressed={!filters.severity}
          onClick={() => choose("severity", "")}
        >
          All severities <span>{total}</span>
        </button>
        {SEVERITIES.map((value) => (
          <button
            key={value}
            type="button"
            className={`rail-option${filters.severity === value ? " selected" : ""}`}
            aria-pressed={filters.severity === value}
            onClick={() => choose("severity", value)}
          >
            {label(value)} <span>{counts.severities[value] ?? 0}</span>
          </button>
        ))}
      </fieldset>

      <fieldset>
        <legend>Category</legend>
        {Object.entries(counts.categories)
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([value, count]) => (
            <button
              key={value}
              type="button"
              className={`rail-option${filters.category === value ? " selected" : ""}`}
              aria-pressed={filters.category === value}
              onClick={() => choose("category", value)}
            >
              {label(value)} <span>{count}</span>
            </button>
          ))}
      </fieldset>

      <fieldset>
        <legend>Verification</legend>
        {STATUSES.map((value) => (
          <button
            key={value}
            type="button"
            className={`rail-option${filters.status === value ? " selected" : ""}`}
            aria-pressed={filters.status === value}
            onClick={() => choose("status", value)}
          >
            {label(value)} <span>{counts.statuses[value] ?? 0}</span>
          </button>
        ))}
      </fieldset>

      <fieldset>
        <legend>
          <label htmlFor="file-filter">File path contains</label>
        </legend>
        <input
          id="file-filter"
          type="search"
          placeholder="app/api"
          defaultValue={filters.file ?? ""}
          onKeyDown={(event) => {
            if (event.key === "Enter") choose("file", (event.target as HTMLInputElement).value.trim());
          }}
          onBlur={(event) => choose("file", event.target.value.trim())}
        />
      </fieldset>
    </aside>
  );
}
