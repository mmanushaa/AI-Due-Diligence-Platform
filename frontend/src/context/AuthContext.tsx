import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  ApiError,
  getStoredUser,
  getToken,
  login as apiLogin,
  logout as apiLogout,
  me as fetchMe,
  storeUser,
  type MeOut,
  type Role,
} from "../lib/api";

interface AuthValue {
  user: MeOut | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  can: (...roles: Role[]) => boolean;
}

const AuthContext = createContext<AuthValue | null>(null);

/** Admin is a superset of every role, matching require_roles on the server. */
export function hasRole(user: MeOut | null, ...roles: Role[]): boolean {
  if (!user) return false;
  if (user.role === "admin") return true;
  return roles.includes(user.role as Role);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<MeOut | null>(() => (getToken() ? getStoredUser() : null));
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function bootstrap() {
      if (!getToken()) {
        setLoading(false);
        return;
      }
      try {
        const account = await fetchMe();
        if (cancelled) return;
        setUser(account);
        storeUser(account);
      } catch (error) {
        if (cancelled) return;
        // A 401 after a refresh attempt means the session is genuinely gone.
        if (error instanceof ApiError && error.status === 401) {
          setUser(null);
          storeUser(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  const signIn = useCallback(async (email: string, password: string) => {
    const result = await apiLogin(email, password);
    setUser(result.user);
  }, []);

  const signOut = useCallback(async () => {
    await apiLogout();
    setUser(null);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({
      user,
      loading,
      signIn,
      signOut,
      can: (...roles: Role[]) => hasRole(user, ...roles),
    }),
    [user, loading, signIn, signOut]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
