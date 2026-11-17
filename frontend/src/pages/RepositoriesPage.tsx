import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type DragEvent, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import DataTable, { type Column } from "../components/DataTable";
import { RiskBadge, StatusBadge } from "../components/Badges";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import {
  ApiError,
  listRepositories,
  startAudit,
  uploadRepository,
  type RepositoryRow,
} from "../lib/api";
import { formatDate, relativeTime } from "../lib/format";

const MAX_UPLOAD_BYTES = 52_428_800;

export default function RepositoriesPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [clientError, setClientError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const repos = useQuery({ queryKey: ["repositories"], queryFn: listRepositories });

  const upload = useMutation({
    mutationFn: uploadRepository,
    onSuccess: async () => {
      setFile(null);
      setNotice("Repository stored. Start an audit when you are ready.");
      await queryClient.invalidateQueries({ queryKey: ["repositories"] });
    },
  });

  const start = useMutation({
    mutationFn: startAudit,
    onSuccess: (audit) => navigate(`/audits/${audit.id}/progress`),
  });

  function chooseFile(next: File | null) {
    setClientError(null);
    setNotice(null);
    upload.reset();
    if (next && !next.name.toLowerCase().endsWith(".zip")) {
      setFile(null);
      setClientError("Only .zip archives are accepted.");
      return;
    }
    if (next && next.size > MAX_UPLOAD_BYTES) {
      setFile(null);
      setClientError(
        `This file is ${Math.round(next.size / 1_048_576)} MB; the limit is 50 MB.`
      );
      return;
    }
    setFile(next);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("name", file.name.replace(/\.zip$/i, ""));
    try {
      await upload.mutateAsync(form);
    } catch {
      // Shown from the mutation state below.
    }
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    const dropped = event.dataTransfer.files.item(0);
    if (dropped) chooseFile(dropped);
  }

  const columns: Column<RepositoryRow>[] = [
    {
      key: "name",
      header: "Repository",
      render: (row) => (
        <span className="stack">
          <strong>{row.name}</strong>
          <span className="muted mono">{row.source_type}</span>
        </span>
      ),
      sortValue: (row) => row.name,
    },
    {
      key: "audits",
      header: "Audits",
      render: (row) => row.audit_count,
      sortValue: (row) => row.audit_count,
      align: "right",
    },
    {
      key: "last",
      header: "Last audit",
      render: (row) =>
        row.last_audit ? (
          <span className="stack">
            <StatusBadge value={row.last_audit.status} />
            <RiskBadge value={row.last_audit.risk_level} />
          </span>
        ) : (
          <span className="muted">Never audited</span>
        ),
      sortValue: (row) => row.last_audit?.finished_at ?? "",
    },
    {
      key: "created",
      header: "Uploaded",
      render: (row) => (
        <span title={formatDate(row.created_at)}>{relativeTime(row.created_at)}</span>
      ),
      sortValue: (row) => row.created_at,
    },
    {
      key: "actions",
      header: "Actions",
      render: (row) => (
        <span className="row-actions">
          {row.last_audit ? (
            <Link className="button ghost small" to={`/audits/${row.last_audit.id}/findings`}>
              Findings
            </Link>
          ) : null}
          <button
            type="button"
            className="button small"
            disabled={start.isPending}
            onClick={() => start.mutate(row.id)}
          >
            Start audit
          </button>
        </span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Repositories"
        subtitle="Upload a zip snapshot, then start an audit. The archive is extracted read-only and never executed."
      />

      <section className="panel">
        <h2>Upload a repository</h2>
        <div
          className={`dropzone${dragging ? " dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={drop}
        >
          <p>Drag a .zip here, or choose a file (max 50 MB).</p>
          <input
            ref={inputRef}
            id="repo-file"
            type="file"
            accept=".zip,application/zip"
            className="visually-hidden"
            onChange={(event) => chooseFile(event.target.files?.item(0) ?? null)}
          />
          <button type="button" className="button" onClick={() => inputRef.current?.click()}>
            Choose file
          </button>
          {file ? (
            <p className="mono">
              {file.name} · {(file.size / 1_048_576).toFixed(1)} MB
            </p>
          ) : null}
        </div>

        {clientError ? (
          <p className="form-error" role="alert">
            {clientError}
          </p>
        ) : null}
        {upload.isError ? (
          <p className="form-error" role="alert">
            {upload.error instanceof ApiError ? upload.error.message : "Upload failed"}
          </p>
        ) : null}
        {notice ? (
          <p className="form-ok" role="status">
            {notice}
          </p>
        ) : null}
        {start.isError ? (
          <p className="form-error" role="alert">
            {start.error instanceof ApiError ? start.error.message : "Could not start the audit"}
          </p>
        ) : null}

        <form onSubmit={(event) => void submit(event)}>
          <button type="submit" className="button primary" disabled={!file || upload.isPending}>
            {upload.isPending ? "Uploading…" : "Upload"}
          </button>
        </form>
      </section>

      <section className="panel">
        <h2>Stored repositories</h2>
        {repos.isPending ? <SkeletonRows rows={4} label="Loading repositories" /> : null}
        {repos.isError ? (
          <ErrorState error={repos.error} onRetry={() => void repos.refetch()} />
        ) : null}
        {repos.isSuccess ? (
          repos.data.repositories.length === 0 ? (
            <EmptyState title="No repositories yet">
              Upload a zip above; its last audit result will be listed here.
            </EmptyState>
          ) : (
            <DataTable
              caption="Stored repositories"
              columns={columns}
              rows={repos.data.repositories}
              rowKey={(row) => row.id}
              initialSort={{ key: "created", direction: "desc" }}
            />
          )
        ) : null}
      </section>
    </>
  );
}
