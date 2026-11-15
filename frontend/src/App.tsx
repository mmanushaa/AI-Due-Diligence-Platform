import { useState } from "react";
import UploadPage from "./pages/UploadPage";
import StatusPage from "./pages/StatusPage";
import FindingsPage from "./pages/Findings";
import LoginForm from "./components/LoginForm";
import { getToken, logout, type SessionUser, type UploadedRepository } from "./lib/api";

type View = "upload" | "status" | "findings";

function App() {
  const [view, setView] = useState<View>("upload");
  const [repo, setRepo] = useState<UploadedRepository | null>(null);
  const [auditId, setAuditId] = useState<string | null>(null);
  // Every endpoint needs a bearer token since step 3.6, so the app starts signed out.
  const [user, setUser] = useState<SessionUser | null>(null);
  const [signedIn, setSignedIn] = useState(() => getToken() !== null);

  if (!signedIn) {
    return (
      <div className="app">
        <LoginForm
          onSignedIn={(next) => {
            setUser(next);
            setSignedIn(true);
          }}
        />
      </div>
    );
  }

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
          <button
            className="nav-btn"
            onClick={() => {
              logout();
              setUser(null);
              setSignedIn(false);
            }}
            title={user ? `${user.email} (${user.role})` : "sign out"}
          >
            Sign out
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
