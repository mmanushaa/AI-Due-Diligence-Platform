export interface RepositoryOut {
  id: string;
  name: string;
  source_type: string;
  source_ref: string | null;
  storage_path: string;
  sha256: string | null;
  size_bytes: number | null;
  created_at: string;
}

export interface UploadedRepository extends RepositoryOut {
  file_count: number;
  uncompressed_bytes: number;
}

export interface AuditOut {
  id: string;
  status: string;
  current_stage: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface FindingSummary {
  total: number;
  by_category: Record<string, number>;
  by_severity: Record<string, number>;
  high_critical: number;
}

export interface FindingEntry {
  id: string;
  title: string;
  category: string;
  severity: string;
  status: string;
  file_path: string | null;
  line_start: number | null;
  line_end: number | null;
  source_tool: string | null;
  fingerprint: string;
}

export interface FindingList {
  audit: AuditOut;
  repository: RepositoryOut | null;
  summary: FindingSummary;
  findings: FindingEntry[];
  grouped: Record<string, Record<string, string[]>>;
}

export interface EvidenceRow {
  id: string;
  kind: string;
  file_path: string;
  line_start: number;
  line_end: number;
  snippet: string | null;
  label: string | null;
}

export interface FindingDetail {
  id: string;
  title: string;
  description: string | null;
  category: string;
  severity: string;
  status: string;
  file_path: string | null;
  line_start: number | null;
  line_end: number | null;
  source_tool: string | null;
  scanner_tool: string | null;
  fingerprint: string;
  recommendation: string | null;
  verdict: string | null;
  evidence: EvidenceRow[];
  audit: AuditOut | null;
  repository_id: string | null;
}

const BASE = "";

const TOKEN_KEY = "dd.access_token";

/** The access token lives in memory by day and in localStorage by night.
 *  The refresh token is an HttpOnly cookie the browser sends to /auth/refresh;
 *  JavaScript never sees it. */
export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface SessionUser {
  id: string;
  email: string;
  role: "analyst" | "reviewer" | "admin";
}

export interface LoginResult {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: SessionUser;
}

export async function login(email: string, password: string): Promise<LoginResult> {
  const res = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (res.status === 401) throw new Error("Invalid email or password");
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const result = (await res.json()) as LoginResult;
  setToken(result.access_token);
  return result;
}

export function logout(): void {
  setToken(null);
  void fetch(`${BASE}/api/v1/auth/logout`, { method: "POST", credentials: "include" });
}

/** One audit's live progress. Mirrors app/progress.py on the server. */
export interface ProgressSnapshot {
  type: "progress";
  audit_id: string;
  status: string;
  current_stage: string | null;
  stages: Record<string, StageState>;
  elapsed_ms: number;
  cost_usd: number;
  final: boolean;
  at: string;
}

export type StageState = "pending" | "running" | "done" | "failed" | "skipped";

/** EventSource cannot send headers, so the access token rides in the query. */
export function progressEventsUrl(auditId: string | number): string {
  const token = getToken() ?? "";
  return `${BASE}/api/v1/audits/${auditId}/events?token=${encodeURIComponent(token)}`;
}

function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = res.statusText;
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

export async function uploadRepository(
  form: FormData
): Promise<UploadedRepository> {
  const res = await fetch(`${BASE}/api/v1/repositories`, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  return json<UploadedRepository>(res);
}

export async function startAudit(
  repoId: string | number
): Promise<AuditOut> {
  const res = await fetch(`${BASE}/api/v1/repositories/${repoId}/audits`, {
    method: "POST",
    headers: authHeaders(),
  });
  if (!res.ok) {
    if (res.status === 404) {
      throw new Error("Repository not found");
    }
    throw new Error(`HTTP ${res.status}`);
  }
  return json<AuditOut>(res);
}

export async function getAudit(
  auditId: string | number
): Promise<AuditOut> {
  const res = await fetch(`${BASE}/api/v1/audits/${auditId}`, { headers: authHeaders() });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return json<AuditOut>(res);
}

export async function getFindings(
  auditId: string | number,
  filters?: {
    category?: string;
    severity?: string;
    status?: string;
    file?: string;
  }
): Promise<FindingList> {
  const params = new URLSearchParams();
  if (filters?.category) params.set("category", filters.category);
  if (filters?.severity) params.set("severity", filters.severity);
  if (filters?.status) params.set("status", filters.status);
  if (filters?.file) params.set("file", filters.file);

  const qs = params.size ? `?${params.toString()}` : "";
  const res = await fetch(`${BASE}/api/v1/audits/${auditId}/findings${qs}`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return json<FindingList>(res);
}

export async function getFinding(
  findingId: string
): Promise<FindingDetail> {
  const res = await fetch(`${BASE}/api/v1/findings/${findingId}`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return json<FindingDetail>(res);
}

export async function getReportHtml(auditId: string | number): Promise<string> {
  const res = await fetch(`${BASE}/api/v1/audits/${auditId}/report`, {
    headers: authHeaders(),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return res.text();
}

