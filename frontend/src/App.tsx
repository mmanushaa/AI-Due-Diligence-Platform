import { useState } from "react";
import UploadPage from "./pages/UploadPage";
import StatusPage from "./pages/StatusPage";
import FindingsPage from "./pages/Findings";
import type { UploadedRepository } from "./lib/api";

type View = "upload" | "status" | "findings";

function App() {
  const [view, setView] = useState<View>("upload");
  const [repo, setRepo] = useState<UploadedRepository | null>(null);
  const [auditId, setAuditId] = useState<string | null>(null);

  const navigate = (v: View) => setView(v);

  return (
    <div className="app">
      <nav className="top-nav">
        <span className="brand">AI Due Diligence</span>
        <div className="nav-links">
          <button
            className={`nav-btn${view === "upload" ? " active" : ""}`}
            onClick={() => navigate("upload")}
          >
            Upload
          </button>
          <button
            className={`nav-btn${view === "status" ? " active" : ""}`}
            onClick={() => {
              if (repo && repo.id) {
                navigate("status");
                setAuditId(repo.id);
              }
            }}
          >
            Status
          </button>
          <button
            className={`nav-btn${view === "findings" ? " active" : ""}`}
            onClick={() => {
              if (auditId) {
                navigate("findings");
              }
            }}
            disabled={!auditId}
          >
            Findings
          </button>
        </div>
      </nav>

      <main className="main">
        {view === "upload" && (
          <UploadPage
            onUploaded={(r) => {
              setRepo(r);
              navigate("status");
            }}
          />
        )}
        {view === "status" && (
          <StatusPage
            key={repo?.id ?? "no-repo"}
            repositoryId={repo?.id}
            auditId={auditId}
            onAuditStarted={(id) => {
              setAuditId(id);
              navigate("findings");
            }}
          />
        )}
        {view === "findings" && auditId && (
          <FindingsPage auditId={auditId} />
        )}
      </main>
    </div>
  );
}

export default App;
