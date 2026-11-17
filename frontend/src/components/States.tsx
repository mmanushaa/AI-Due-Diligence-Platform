import type { ReactNode } from "react";

export function SkeletonRows({ rows = 5, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="skeleton" role="status" aria-live="polite" aria-label={label}>
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-row" key={index} />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="state state-empty">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action ? <div className="state-action">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  error,
  onRetry,
}: {
  title?: string;
  error: unknown;
  onRetry?: () => void;
}) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="state state-error" role="alert">
      <h3>{title}</h3>
      <p className="mono">{message}</p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function PermissionDenied({ what }: { what: string }) {
  return (
    <div className="state state-denied" role="alert">
      <h3>You do not have access to {what}</h3>
      <p>
        This screen is limited to a role your account does not hold. Ask an administrator if you
        believe you should see it.
      </p>
    </div>
  );
}

export function IncompleteBanner({ reason }: { reason: string | null | undefined }) {
  if (!reason) return null;
  return (
    <div className="banner banner-warn" role="status">
      <strong>Partial results.</strong> {reason}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p className="page-subtitle">{subtitle}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}
