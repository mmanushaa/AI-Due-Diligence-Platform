import { useState, useRef } from "react";
import { uploadRepository, type UploadedRepository } from "../lib/api";

export default function UploadPage({
  onUploaded,
}: {
  onUploaded: (repo: UploadedRepository) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const candidate = e.target.files?.[0];
    setFile(candidate ?? null);
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setError("Select a zip archive first.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      if (name.trim()) form.append("name", name.trim());

      const repo = await uploadRepository(form);
      onUploaded(repo);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page">
      <header className="page-header">
        <h1>Upload repository</h1>
        <p className="muted">
          Upload a zip archive of a source repository to start a due-diligence audit.
        </p>
      </header>

      <form className="upload-form" onSubmit={handleSubmit}>
        <div className="field">
          <label className="field-label" htmlFor="repo-file">
            Archive
          </label>
          <input
            ref={inputRef}
            id="repo-file"
            type="file"
            accept=".zip"
            className="field-input"
            onChange={handleFile}
          />
          {file && (
            <p className="muted field-hint">
              {file.name} — {(file.size / 1024 / 1024).toFixed(2)} MB
            </p>
          )}
        </div>

        <div className="field">
          <label className="field-label" htmlFor="repo-name">
            Name
          </label>
          <input
            id="repo-name"
            type="text"
            className="field-input"
            placeholder="e.g. python_app"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <p className="muted field-hint">Optional. Defaults to the archive name.</p>
        </div>

        <div className="field-actions">
          <button type="submit" className="btn btn-primary" disabled={submitting || !file}>
            {submitting ? "Uploading…" : "Upload"}
          </button>
          {error && <p className="field-error">{error}</p>}
        </div>
      </form>

      <section className="help">
        <h2>What happens next</h2>
        <ol>
          <li>The archive is extracted into a read-only snapshot.</li>
          <li>Source files are indexed and filtered.</li>
          <li>Gitleaks, Semgrep, OSV-Scanner and ScanCode run in parallel.</li>
          <li>Findings are grouped by category and severity on the Findings tab.</li>
        </ol>
      </section>
    </div>
  );
}
