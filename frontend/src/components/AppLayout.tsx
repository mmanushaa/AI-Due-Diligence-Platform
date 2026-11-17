import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { RoleBadge } from "./Badges";

const THEME_LABEL = { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" };

function navClass({ isActive }: { isActive: boolean }): string {
  return `nav-link${isActive ? " active" : ""}`;
}

export default function AppLayout() {
  const { user, signOut, can } = useAuth();
  const { choice, cycle } = useTheme();

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar" aria-label="Main navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            DD
          </span>
          <span>
            <strong>Due Diligence</strong>
            <small>audit console</small>
          </span>
        </div>

        <nav className="nav">
          <NavLink to="/" end className={navClass}>
            Dashboard
          </NavLink>
          {can("analyst") ? (
            <NavLink to="/repositories" className={navClass}>
              Repositories
            </NavLink>
          ) : null}
          {can("reviewer") ? (
            <NavLink to="/reviews" className={navClass}>
              Review queue
            </NavLink>
          ) : null}
          {can("admin") ? (
            <NavLink to="/benchmarks" className={navClass}>
              Benchmarks
            </NavLink>
          ) : null}
          {can("admin") ? (
            <NavLink to="/settings" className={navClass}>
              Settings
            </NavLink>
          ) : null}
        </nav>

        <div className="sidebar-footer">
          <button type="button" className="button ghost small" onClick={cycle}>
            {THEME_LABEL[choice]}
          </button>
          <div className="account">
            <span className="account-email" title={user?.email}>
              {user?.email}
            </span>
            {user ? <RoleBadge value={user.role} /> : null}
          </div>
          <button type="button" className="button ghost small" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </aside>

      <main id="main" className="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
