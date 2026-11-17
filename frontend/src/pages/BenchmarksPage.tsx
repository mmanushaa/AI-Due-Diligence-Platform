import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import DataTable, { type Column } from "../components/DataTable";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { getBenchmarks, type BenchmarkRun } from "../lib/api";
import { formatDate, money, percent } from "../lib/format";

const METRICS: { key: keyof BenchmarkRun; label: string; format: (value: number) => string }[] = [
  { key: "precision", label: "Precision", format: percent },
  { key: "recall", label: "Recall", format: percent },
  { key: "evidence_accuracy", label: "Evidence accuracy", format: percent },
  { key: "severity_accuracy", label: "Severity accuracy", format: percent },
  { key: "cross_file_recall", label: "Cross-file recall", format: percent },
  { key: "reproducibility", label: "Reproducibility", format: percent },
  { key: "cost_usd", label: "Cost", format: money },
  { key: "runtime_seconds", label: "Runtime (s)", format: (value) => `${Math.round(value)}s` },
];

export default function BenchmarksPage() {
  const query = useQuery({ queryKey: ["benchmarks"], queryFn: getBenchmarks });
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");

  const runs = query.data?.runs ?? [];
  const leftRun = runs.find((run) => run.configuration === left);
  const rightRun = runs.find((run) => run.configuration === right);

  const columns: Column<BenchmarkRun>[] = [
    {
      key: "configuration",
      header: "Configuration",
      render: (row) => <strong>{row.configuration}</strong>,
      sortValue: (row) => row.configuration,
    },
    ...METRICS.map<Column<BenchmarkRun>>((metric) => ({
      key: metric.key,
      header: metric.label,
      align: "right",
      render: (row) => {
        const value = row[metric.key];
        return <span className="mono">{typeof value === "number" ? metric.format(value) : "—"}</span>;
      },
      sortValue: (row) => (typeof row[metric.key] === "number" ? (row[metric.key] as number) : -1),
    })),
    {
      key: "false_positives",
      header: "False positives",
      align: "right",
      render: (row) => <span className="mono">{row.false_positives ?? "—"}</span>,
      sortValue: (row) => row.false_positives ?? -1,
    },
  ];

  return (
    <>
      <PageHeader
        title="Benchmark results"
        subtitle="Scanners only, single agent, multi-agent, and multi-agent with validation, on the same fixtures."
      />

      {query.isPending ? <SkeletonRows rows={6} label="Loading benchmark runs" /> : null}
      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}
      {query.isSuccess ? (
        runs.length === 0 ? (
          <EmptyState title="No benchmark runs recorded">
            {query.data.note ??
              "The four-way comparison is produced by the benchmark harness; this page reads its results."}
          </EmptyState>
        ) : (
          <>
            <p className="muted">
              {query.data.generated_at ? `Generated ${formatDate(query.data.generated_at)}.` : null}{" "}
              {query.data.note}
            </p>
            <section className="panel">
              <DataTable
                caption="Benchmark runs by configuration"
                columns={columns}
                rows={runs}
                rowKey={(row) => row.configuration}
              />
            </section>

            <section className="panel">
              <h2>Compare two runs</h2>
              <div className="compare-selects">
                <label>
                  Run A
                  <select value={left} onChange={(event) => setLeft(event.target.value)}>
                    <option value="">Choose…</option>
                    {runs.map((run) => (
                      <option key={run.configuration} value={run.configuration}>
                        {run.configuration}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Run B
                  <select value={right} onChange={(event) => setRight(event.target.value)}>
                    <option value="">Choose…</option>
                    {runs.map((run) => (
                      <option key={run.configuration} value={run.configuration}>
                        {run.configuration}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              {leftRun && rightRun ? (
                <table>
                  <caption className="visually-hidden">Two-run comparison</caption>
                  <thead>
                    <tr>
                      <th scope="col">Metric</th>
                      <th scope="col">{leftRun.configuration}</th>
                      <th scope="col">{rightRun.configuration}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {METRICS.map((metric) => {
                      const a = leftRun[metric.key];
                      const b = rightRun[metric.key];
                      return (
                        <tr key={metric.key}>
                          <th scope="row">{metric.label}</th>
                          <td className="mono">
                            {typeof a === "number" ? metric.format(a) : "—"}
                          </td>
                          <td className="mono">
                            {typeof b === "number" ? metric.format(b) : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              ) : (
                <p className="muted">Pick two configurations to compare them side by side.</p>
              )}
            </section>
          </>
        )
      ) : null}
    </>
  );
}
