import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth, hasRole } from "../context/AuthContext";
import { PermissionDenied, SkeletonRows } from "./States";
import type { Role } from "../lib/api";

export function RequireAuth() {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="centered-panel">
        <SkeletonRows rows={3} label="Checking your session" />
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  return <Outlet />;
}

/** Authenticated but the wrong role: say so in place, do not hide the route. */
export function RequireRole({ roles, what }: { roles: Role[]; what: string }) {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="centered-panel">
        <SkeletonRows rows={3} />
      </div>
    );
  }
  if (!hasRole(user, ...roles)) return <PermissionDenied what={what} />;
  return <Outlet />;
}
