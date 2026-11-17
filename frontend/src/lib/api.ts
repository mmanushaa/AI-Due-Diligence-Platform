/**
 * The one place the UI talks to the API.
 *
 * Every function here is typed by `api-types.ts`, which is generated from the
 * FastAPI OpenAPI schema - a field renamed on the server is a compile error here.
 * Nothing in this file formats anything for display; that belongs to format.ts.
 */

import type {
  AuditLogOut,
  AuditResponse,
  AuditRows,
  BenchmarkOut,
  CodeOut,
  DecisionIn,
  DecisionOut,
  FindingDetail,
  FindingHistoryOut,
  FindingList,
  LoginOut,
  MeOut,
  RepositoryCreated,
  RepositoryRows,
  ReviewList,
  RiskOut,
  SettingsOut,
  UserRows,
} from "./api-types";

export type * from "./api-types";

const BASE = "";
const TOKEN_KEY = "dd.access_token";
const USER_KEY = "dd.session_user";

export type Role = "analyst" | "reviewer" | "admin";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

export function getStoredUser(): MeOut | null {
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as MeOut;
  } catch {
    return null;
  }
}

export function storeUser(user: MeOut | null): void {
  if (user) window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  else window.localStorage.removeItem(USER_KEY);
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function errorFrom(res: Response): Promise<ApiError> {
  let detail = res.statusText || `HTTP ${res.status}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // A non-JSON error body keeps the status text.
  }
  return new ApiError(res.status, detail);
}

let refreshing: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  refreshing ??= fetch(`${BASE}/api/v1/auth/refresh`, {
    method: "POST",
    credentials: "include",
  })
    .then(async (res) => {
      if (!res.ok) return false;
      const body = (await res.json()) as { access_token: string };
      setToken(body.access_token);
      return true;
    })
    .catch(() => false)
    .finally(() => {
      refreshing = null;
    });
  return refreshing;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  { retry = true }: { retry?: boolean } = {}
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...init.headers },
    credentials: "include",
  });
  if (res.status === 401 && retry && (await refreshSession())) {
    return request<T>(path, init, { retry: false });
  }
  if (!res.ok) throw await errorFrom(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export async function login(email: string, password: string): Promise<LoginOut> {
  const res = await fetch(`${BASE}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ email, password }),
  });
  if (res.status === 401) throw new ApiError(401, "Invalid email or password");
  if (!res.ok) throw await errorFrom(res);
  const result = (await res.json()) as LoginOut;
  setToken(result.access_token);
  storeUser(result.user);
  return result;
}

export async function logout(): Promise<void> {
  setToken(null);
  storeUser(null);
  try {
    await fetch(`${BASE}/api/v1/auth/logout`, { method: "POST", credentials: "include" });
  } catch {
    // Signing out locally is enough when the network is down.
  }
}

export const me = () => request<MeOut>("/api/v1/auth/me");

export const listAudits = (limit = 50) =>
  request<AuditRows>(`/api/v1/audits${query({ limit })}`);

export const listRepositories = () => request<RepositoryRows>("/api/v1/repositories");

export const getAudit = (auditId: string) =>
  request<AuditResponse>(`/api/v1/audits/${auditId}`);

export interface FindingFilters {
  category?: string;
  severity?: string;
  status?: string;
  file?: string;
}

export const listFindings = (auditId: string, filters: FindingFilters = {}) =>
  request<FindingList>(`/api/v1/audits/${auditId}/findings${query({ ...filters })}`);

export const getFinding = (findingId: string) =>
  request<FindingDetail>(`/api/v1/findings/${findingId}`);

export const getFindingCode = (findingId: string, context = 8) =>
  request<CodeOut>(`/api/v1/findings/${findingId}/code${query({ context })}`);

export const getFindingHistory = (findingId: string) =>
  request<FindingHistoryOut>(`/api/v1/findings/${findingId}/history`);

export const getRisk = (auditId: string) => request<RiskOut>(`/api/v1/audits/${auditId}/risk`);

export const getAuditLog = (auditId: string) =>
  request<AuditLogOut>(`/api/v1/audits/${auditId}/audit-log`);

export const getReportHtml = async (auditId: string): Promise<string> => {
  const res = await fetch(`${BASE}/api/v1/audits/${auditId}/report`, {
    headers: authHeaders(),
    credentials: "include",
  });
  if (!res.ok) throw await errorFrom(res);
  return res.text();
};

/** The PDF endpoint needs a bearer header, so it is fetched and saved as a blob. */
export async function downloadReportPdf(auditId: string): Promise<void> {
  const res = await fetch(`${BASE}/api/v1/audits/${auditId}/report.pdf`, {
    headers: authHeaders(),
    credentials: "include",
  });
  if (!res.ok) throw await errorFrom(res);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `audit-${auditId}.pdf`;
  link.click();
  URL.revokeObjectURL(url);
}

export const listReviews = (status = "pending") =>
  request<ReviewList>(`/api/v1/reviews${query({ status })}`);

export const decideReview = (reviewId: string, body: DecisionIn) =>
  request<DecisionOut>(`/api/v1/reviews/${reviewId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export async function uploadRepository(form: FormData): Promise<RepositoryCreated> {
  return request<RepositoryCreated>("/api/v1/repositories", { method: "POST", body: form });
}

export const startAudit = (repositoryId: string) =>
  request<AuditResponse>(`/api/v1/repositories/${repositoryId}/audits`, { method: "POST" });

export const listUsers = () => request<UserRows>("/api/v1/users");
export const getSettings = () => request<SettingsOut>("/api/v1/settings");
export const getBenchmarks = () => request<BenchmarkOut>("/api/v1/benchmarks");

/** EventSource cannot send headers, so the access token rides in the query. */
export function progressEventsUrl(auditId: string): string {
  const token = getToken() ?? "";
  return `${BASE}/api/v1/audits/${auditId}/events?token=${encodeURIComponent(token)}`;
}

/** SSE is not described by OpenAPI; these mirror app/progress.py. */
export type StageState = "pending" | "running" | "done" | "failed" | "skipped";

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
