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

  return (
    <div className="app">
      <nav className="top-nav">
        <span className="brand">AI Due Diligence</span>
        <div className="nav-links">
          <button
            className={`nav-btn${view === "upload" ? " active" : ""}`}
            onClick={() => setView("upload")}
          >
            Upload
          </button>
          <button
            className={`nav-btn${view === "status" ? " active" : ""}`}
            onClick={() => repo && setView("status")}
            disabled={!repo}
          >
            Status
          </button>
          <button
            className={`nav-btn${view === "findings" ? " active" : ""}`}
            onClick={() => auditId && setView("findings")}
            disabled={!auditId}
          >
            Findings
          </button>
        </div>
      </nav>

      <main className="main">
        {view === "upload" && (
          <UploadPage
            onUploaded={(uploaded) => {
              setRepo(uploaded);
              setAuditId(null);
              setView("status");
            }}
          />
        )}
        {view === "status" && (
          <StatusPage
            repositoryId={repo?.id}
            onAuditStarted={(id) => setAuditId(id)}
          />
        )}
        {view === "findings" && auditId && <FindingsPage auditId={auditId} />}
      </main>
    </div>
  );
}

export default App;
