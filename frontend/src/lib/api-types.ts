// Generated from backend/app (FastAPI OpenAPI schema) by backend/scripts/generate_ts_types.py.
// Do not edit by hand: run `python -m scripts.export_openapi` from backend/ and then
// `python -m scripts.generate_ts_types` from backend/ after changing a Pydantic model.

export interface AuditLogEntry {
  id: string;
  actor: string;
  action: string;
  object_type: string;
  object_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditLogOut {
  audit_id: string;
  entries: AuditLogEntry[];
  total: number;
}

export interface AuditOut {
  id: string;
  status: string;
  current_stage?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
}

export interface AuditResponse {
  id: string;
  repository_id: string;
  status: string;
  current_stage: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
}

export interface AuditRow {
  id: string;
  repository_id: string;
  repository_name: string;
  status: string;
  current_stage: string | null;
  risk_level: string | null;
  cost_usd: number;
  budget_usd: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AuditRows {
  audits: AuditRow[];
  total: number;
}

export interface BenchmarkOut {
  runs: BenchmarkRun[];
  generated_at: string | null;
  note: string | null;
}

export interface BenchmarkRun {
  configuration: string;
  precision: number | null;
  recall: number | null;
  false_positives: number | null;
  evidence_accuracy: number | null;
  severity_accuracy: number | null;
  cost_usd: number | null;
  runtime_seconds: number | null;
  reproducibility: number | null;
  cross_file_recall: number | null;
  runs: number | null;
}

export interface Body_create_repository_api_v1_repositories_post {
  file: string;
  name?: string | null;
}

export interface CodeLine {
  number: number;
  text: string;
  masked: boolean;
  highlighted: boolean;
}

export interface CodeOut {
  finding_id: string;
  file_path: string;
  line_start: number;
  line_end: number;
  highlight_start: number;
  highlight_end: number;
  lines: CodeLine[];
  truncated: boolean;
  masked: boolean;
}

export interface DecisionIn {
  decision: "approve" | "override" | "request_evidence";
  reason?: string | null;
  new_level?: "critical" | "high" | "medium" | "low" | "info" | null;
}

export interface DecisionOut {
  review_id: string;
  review_status: string;
  audit_id: string;
  audit_status: string;
  assessment_id?: string | null;
  superseded_assessment_id?: string | null;
  resumed: boolean;
}

export interface EvidenceOut {
  id: string;
  file_path: string;
  line_start: number;
  line_end: number;
  snippet: string | null;
  kind: string;
  label?: string | null;
}

export interface FactorRow {
  finding_id: string;
  title: string;
  severity: string;
  severity_weight: number;
  evidence_confidence: number;
  exposure_multiplier: number;
  finding_score: number;
  notes: string | null;
  hints: string[];
}

export interface FindingDetail {
  id: string;
  title: string;
  description?: string | null;
  category: string;
  severity: string;
  status: string;
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  source_tool?: string | null;
  scanner_tool?: string | null;
  fingerprint: string;
  confidence?: number | null;
  business_impact?: string | null;
  recommendation?: string | null;
  verdict?: string | null;
  evidence?: EvidenceOut[];
  audit?: AuditOut | null;
  repository_id?: string | null;
}

export interface FindingEntry {
  id: string;
  title: string;
  category: string;
  severity: string;
  status: string;
  confidence?: number | null;
  file_path?: string | null;
  line_start?: number | null;
  line_end?: number | null;
  source_tool?: string | null;
  fingerprint: string;
}

export interface FindingHistoryOut {
  finding_id: string;
  events: FindingHistoryRow[];
}

export interface FindingHistoryRow {
  id: string;
  from_status: string | null;
  to_status: string;
  actor: string;
  note: string | null;
  created_at: string;
}

export interface FindingList {
  audit: AuditOut;
  repository?: RepositoryOut | null;
  summary: FindingSummary;
  findings?: FindingEntry[];
  grouped?: Record<string, Record<string, string[]>>;
}

export interface FindingSummary {
  total?: number;
  by_category?: Record<string, number>;
  by_severity?: Record<string, number>;
  high_critical?: number;
}

export interface HTTPValidationError {
  detail?: ValidationError[];
}

export interface LastAudit {
  id: string;
  status: string;
  risk_level: string | null;
  finished_at: string | null;
}

export interface LoginIn {
  email: string;
  password: string;
}

export interface LoginOut {
  access_token: string;
  token_type?: string;
  expires_in: number;
  user: UserOut;
}

export interface MeOut {
  id: string;
  email: string;
  role: string;
}

export interface RepositoryCreated {
  id: string;
  name: string;
  source_type: string;
  source_ref: string | null;
  storage_path: string;
  sha256: string | null;
  size_bytes: number | null;
  created_at: string;
  file_count: number;
  uncompressed_bytes: number;
}

export interface RepositoryOut {
  id: string;
  name: string;
  source_type: string;
  source_ref?: string | null;
  storage_path: string;
  sha256?: string | null;
  size_bytes?: number | null;
  created_at: string;
}

export interface RepositoryRow {
  id: string;
  name: string;
  source_type: string;
  source_ref: string | null;
  size_bytes: number | null;
  created_at: string;
  audit_count: number;
  last_audit: LastAudit | null;
}

export interface RepositoryRows {
  repositories: RepositoryRow[];
  total: number;
}

export interface ReviewList {
  reviews: ReviewOut[];
  total: number;
}

export interface ReviewOut {
  id: string;
  audit_id: string;
  assessment_id: string;
  status: string;
  requested_at: string | null;
  level: string | null;
  repository: string | null;
}

export interface RiskOut {
  audit_id: string;
  level: string;
  floor: string | null;
  score: number | null;
  rule: string | null;
  rationale: string | null;
  status: string;
  supersedes_id: string | null;
  counts: Record<string, number>;
  rejected: number;
  escalations: string[];
  factors: FactorRow[];
  business_risk_note: string | null;
}

export interface SettingsOut {
  llm_default_model: string;
  llm_triage_model: string;
  llm_fallback_model: string;
  llm_fallback_enabled: boolean;
  llm_temperature: number;
  llm_max_output_tokens: number;
  max_upload_bytes: number;
  default_budget_usd: number | null;
  retention_days: number | null;
  notes: string[];
}

export interface UserOut {
  id: string;
  email: string;
  role: string;
}

export interface UserRow {
  id: string;
  email: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface UserRows {
  users: UserRow[];
  total: number;
}

export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

