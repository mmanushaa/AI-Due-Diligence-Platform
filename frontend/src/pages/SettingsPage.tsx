import { useQuery } from "@tanstack/react-query";
import DataTable, { type Column } from "../components/DataTable";
import { RoleBadge } from "../components/Badges";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "../components/States";
import { getSettings, listUsers, type UserRow } from "../lib/api";
import { formatDate, money } from "../lib/format";

export default function SettingsPage() {
  const users = useQuery({ queryKey: ["users"], queryFn: listUsers });
  const settings = useQuery({ queryKey: ["settings"], queryFn: getSettings });

  const columns: Column<UserRow>[] = [
    {
      key: "email",
      header: "Email",
      render: (row) => <span className="mono">{row.email}</span>,
      sortValue: (row) => row.email,
    },
    {
      key: "role",
      header: "Role",
      render: (row) => <RoleBadge value={row.role} />,
      sortValue: (row) => row.role,
    },
    {
      key: "active",
      header: "Active",
      render: (row) => (row.is_active ? "Yes" : "No"),
      sortValue: (row) => (row.is_active ? 1 : 0),
    },
    {
      key: "created",
      header: "Created",
      render: (row) => formatDate(row.created_at),
      sortValue: (row) => row.created_at,
    },
  ];

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Users, model routing, limits. Accounts are seeded per deployment; there is no self-service signup."
      />

      <section className="panel">
        <h2>Users and roles</h2>
        {users.isPending ? <SkeletonRows rows={4} label="Loading users" /> : null}
        {users.isError ? <ErrorState error={users.error} onRetry={() => void users.refetch()} /> : null}
        {users.isSuccess ? (
          users.data.users.length === 0 ? (
            <EmptyState title="No accounts">
              Run the seed script to create the analyst, reviewer and admin accounts.
            </EmptyState>
          ) : (
            <DataTable
              caption="User accounts"
              columns={columns}
              rows={users.data.users}
              rowKey={(row) => row.id}
            />
          )
        ) : null}
      </section>

      <section className="panel">
        <h2>Deployment configuration</h2>
        {settings.isPending ? <SkeletonRows rows={3} label="Loading settings" /> : null}
        {settings.isError ? (
          <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />
        ) : null}
        {settings.isSuccess ? (
          <>
            <div className="cards">
              <div className="card">
                <h3>Model routing</h3>
                <dl className="facts">
                  <dt>Default</dt>
                  <dd className="mono">{settings.data.llm_default_model}</dd>
                  <dt>Triage</dt>
                  <dd className="mono">{settings.data.llm_triage_model}</dd>
                  <dt>Fallback</dt>
                  <dd className="mono">
                    {settings.data.llm_fallback_enabled ? settings.data.llm_fallback_model : "disabled"}
                  </dd>
                  <dt>Temperature / max tokens</dt>
                  <dd className="mono">
                    {settings.data.llm_temperature} / {settings.data.llm_max_output_tokens}
                  </dd>
                </dl>
              </div>
              <div className="card">
                <h3>Per-audit budget</h3>
                <p className="stat-value small">
                  {settings.data.default_budget_usd === null
                    ? "No default cap"
                    : money(settings.data.default_budget_usd)}
                </p>
                <p className="muted">
                  A cap is stored per audit when it is created; an unset budget means the run has no
                  hard stop.
                </p>
              </div>
              <div className="card">
                <h3>Retention</h3>
                <p className="stat-value small">
                  {settings.data.retention_days === null
                    ? "Not enforced in-app"
                    : `${settings.data.retention_days} days`}
                </p>
                <p className="muted">
                  Deletion follows the deployment's backup policy; the UI never deletes an audit.
                </p>
              </div>
              <div className="card">
                <h3>Upload limit</h3>
                <p className="stat-value small">{Math.round(settings.data.max_upload_bytes / 1_048_576)} MB</p>
                <p className="muted">Checked in the browser and again while the archive streams in.</p>
              </div>
            </div>
            <ul className="notes">
              {settings.data.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
              <li>This screen is read-only: changing these values is a deployment change.</li>
            </ul>
          </>
        ) : null}
      </section>
    </>
  );
}
