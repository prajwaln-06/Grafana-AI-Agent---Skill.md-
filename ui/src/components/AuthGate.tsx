/**
 * AuthGate.tsx
 *
 * The React side of the OpenID Connect authentication.
 *
 * The backend is the OIDC relying party: it sends the browser to Authentik,
 * validates the ID token that comes back and keeps the session in an
 * HttpOnly cookie. This component therefore does not implement a login form.
 * It
 *
 *   - resolves who the current user is (via the backend's /api/auth/whoami),
 *   - shows the branded login screen when the session is missing or has
 *     expired, and
 *   - ends the session through the backend's logout endpoint.
 *
 * The provider never holds an application role: it only knows *who* the
 * user is. Authorization is a separate, later concern.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import {
  DEFAULT_AUTH_CONFIG,
  fetchAuthConfig,
  fetchPrincipal,
  signIn,
  signOut,
} from "../auth";
import type { AuthConfig, Principal, SessionStatus } from "../auth";
import { AUTH_EXPIRED_EVENT } from "../api";
import { LoginScreen, describeAuthError } from "./LoginScreen";

export type AuthContextValue = {
  status: SessionStatus;
  principal: Principal | null;
  /** The public login configuration (login path, enrollment flow URL). */
  config: AuthConfig;
  /** True when the backend runs without authentication (development mode). */
  devMode: boolean;
  error: string | null;
  /** The message of a failed login attempt, if there was one. */
  loginError: string | null;
  /** True when the session existed and has ended (rather than never existed). */
  expired: boolean;
  signIn: () => void;
  signOut: () => void;
  retry: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used inside an <AuthProvider>.");
  }
  return value;
}

/** Read (and then forget) the error marker the backend puts in the URL. */
function readLoginError(): string | null {
  if (typeof window === "undefined") return null;
  const code = new URLSearchParams(window.location.search).get("auth_error");
  if (!code) return null;
  const message = describeAuthError(code);
  if (window.history && window.history.replaceState) {
    const clean = new URL(window.location.href);
    clean.searchParams.delete("auth_error");
    window.history.replaceState({}, "", clean.toString());
  }
  return message;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [principal, setPrincipal] = useState<Principal | null>(null);
  const [config, setConfig] = useState<AuthConfig>(DEFAULT_AUTH_CONFIG);
  const [devMode, setDevMode] = useState(false);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loginError, setLoginError] = useState<string | null>(() => readLoginError());

  const resolve = useCallback(async () => {
    setStatus("loading");
    setError(null);
    try {
      const resolved = await fetchPrincipal();
      if (!resolved) {
        setPrincipal(null);
        setDevMode(false);
        setStatus("unauthenticated");
        return;
      }
      setPrincipal(resolved);
      // A principal that is explicitly not authenticated means the backend
      // runs with authentication switched off (development mode).
      setDevMode(resolved.authenticated === false);
      setExpired(false);
      setStatus("authenticated");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    void resolve();
  }, [resolve]);

  // The login screen needs to know where to send the visitor. The
  // configuration is public, so it can be fetched before anybody is signed
  // in.
  useEffect(() => {
    let cancelled = false;
    void fetchAuthConfig().then((resolved) => {
      if (!cancelled) setConfig(resolved);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // The API client reports a rejected session here.
  useEffect(() => {
    function onExpired() {
      setPrincipal(null);
      setExpired(true);
      setStatus((current) => (current === "authenticated" ? "unauthenticated" : current));
    }
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired);
  }, []);

  const endSession = useCallback(async () => {
    const ok = await signOut(config);
    if (!ok) {
      setError("The session could not be ended. Please try again.");
      return;
    }
    setPrincipal(null);
    setDevMode(false);
    setExpired(false);
    setLoginError(null);
    setStatus("unauthenticated");
    // Reload the application so that no state of the signed-in session
    // lingers in the page.
    window.location.assign("/");
  }, [config]);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      principal,
      config,
      devMode,
      error,
      loginError,
      expired,
      signIn: () => signIn(config),
      signOut: () => void endSession(),
      retry: () => void resolve(),
    }),
    [status, principal, config, devMode, error, loginError, expired, endSession, resolve]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { status, error, config, expired, loginError, signIn: doSignIn, retry } = useAuth();

  if (status === "loading") {
    return (
      <div className="auth-screen">
        <div className="login-card login-card-slim">
          <h1>Observability Lab</h1>
          <p className="muted">Checking your session…</p>
        </div>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="auth-screen">
        <div className="login-card login-card-slim">
          <h1>Sign-in problem</h1>
          <div className="alert error">
            {error || "The application could not determine your session."}
          </div>
          <div className="auth-actions">
            <button type="button" className="btn" onClick={retry}>
              Try again
            </button>
            <button type="button" className="btn ghost" onClick={doSignIn}>
              Sign in
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (status === "unauthenticated") {
    return <LoginScreen config={config} expired={expired} error={loginError} />;
  }

  return <>{children}</>;
}

export default RequireAuth;
