import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import AppLayout from "./components/AppLayout";
import { RequireAuth, RequireRole } from "./components/guards";
import { AuthProvider } from "./context/AuthContext";
import { ThemeProvider } from "./context/ThemeContext";
import AuditLogPage from "./pages/AuditLogPage";
import AuditProgressPage from "./pages/AuditProgressPage";
import BenchmarksPage from "./pages/BenchmarksPage";
import DashboardPage from "./pages/DashboardPage";
import FindingDetailPage from "./pages/FindingDetailPage";
import FindingsPage from "./pages/FindingsPage";
import LoginPage from "./pages/LoginPage";
import NotFoundPage from "./pages/NotFoundPage";
import RepositoriesPage from "./pages/RepositoriesPage";
import ReportPage from "./pages/ReportPage";
import ReviewDetailPage from "./pages/ReviewDetailPage";
import ReviewQueuePage from "./pages/ReviewQueuePage";
import RiskPage from "./pages/RiskPage";
import SettingsPage from "./pages/SettingsPage";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route element={<RequireAuth />}>
                <Route element={<AppLayout />}>
                  <Route index element={<DashboardPage />} />

                  <Route
                    path="repositories"
                    element={<RequireRole roles={["analyst"]} what="repositories and upload" />}
                  >
                    <Route index element={<RepositoriesPage />} />
                  </Route>

                  <Route
                    element={
                      <RequireRole roles={["analyst", "reviewer"]} what="audit results" />
                    }
                  >
                    <Route path="audits/:auditId/progress" element={<AuditProgressPage />} />
                    <Route path="audits/:auditId/findings" element={<FindingsPage />} />
                    <Route
                      path="audits/:auditId/findings/:findingId"
                      element={<FindingDetailPage />}
                    />
                    <Route path="audits/:auditId/risk" element={<RiskPage />} />
                    <Route path="audits/:auditId/report" element={<ReportPage />} />
                  </Route>

                  <Route
                    path="audits/:auditId/audit-log"
                    element={<RequireRole roles={["reviewer"]} what="the audit log" />}
                  >
                    <Route index element={<AuditLogPage />} />
                  </Route>

                  <Route path="reviews" element={<RequireRole roles={["reviewer"]} what="reviews" />}>
                    <Route index element={<ReviewQueuePage />} />
                    <Route path=":reviewId" element={<ReviewDetailPage />} />
                  </Route>

                  <Route path="benchmarks" element={<RequireRole roles={["admin"]} what="benchmark results" />}>
                    <Route index element={<BenchmarksPage />} />
                  </Route>

                  <Route path="settings" element={<RequireRole roles={["admin"]} what="settings" />}>
                    <Route index element={<SettingsPage />} />
                  </Route>

                  <Route path="*" element={<NotFoundPage />} />
                </Route>
              </Route>
            </Routes>
          </BrowserRouter>
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
