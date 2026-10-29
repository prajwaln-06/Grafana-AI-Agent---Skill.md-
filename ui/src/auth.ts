/**
 * auth.ts
 *
 * Session handling for the browser.
 *
 * Authentication is OpenID Connect against Authentik. The browser never
 * sees a client secret, an access token, a refresh token or an ID token:
 * it only ever receives an HttpOnly session cookie that the browser sends
 * automatically. Nothing is written to localStorage or sessionStorage.
 *
 * The UI needs three things:
 *
 *   1. the public login configuration (where sign-in starts, where
 *      Authentik's enrollment and password-change flows live) --
 *      /api/auth/config;
 *   2. who the current user is -- /api/auth/whoami;
 *   3. a way to end the session -- /api/auth/logout.
 *
 * There is deliberately no email/password form here: Authentik collects and
 * validates credentials, and the browser is sent to its real login form.
 */

/** The verified identity of the current session, as the backend sees it. */
export type Principal = {
  issuer: string;
  subject: string;
  authenticated: boolean;
  username?: string | null;
  email?: string | null;
  name?: string | null;
  app?: string | null;
};

export type SessionStatus = "loading" | "authenticated" | "unauthenticated" | "error";

/**
 * The public configuration of the login screen.
 *
 * `loginPath` is a path on this origin: the browser is sent there and the
 * backend starts the OIDC flow. `signupUrl` and `passwordChangeUrl` are
 * absolute URLs on the Authentik server, where those flows live.
 */
export type AuthConfig = {
  providerName: string;
  authenticationEnabled: boolean;
  loginPath: string;
  callbackPath: string;
  logoutPath: string;
  signupEnabled: boolean;
  signupUrl: string | null;
  passwordChangeUrl: string | null;
  sessionMaxAgeSeconds: number;
};

/**
 * Used when the login configuration cannot be fetched (for instance when
 * the backend is unreachable). Sign-in still works -- the login path is a
 * path on this origin -- but sign-up and password change cannot be
 * offered, because those flow URLs are not guessable.
 */
export const DEFAULT_AUTH_CONFIG: AuthConfig = {
  providerName: "Authentik",
  authenticationEnabled: true,
  loginPath: "/api/auth/login",
  callbackPath: "/api/auth/callback",
  logoutPath: "/api/auth/logout",
  signupEnabled: false,
  signupUrl: null,
  passwordChangeUrl: null,
  sessionMaxAgeSeconds: 3600,
};

/** Fetch the public login configuration. Never throws. */
export async function fetchAuthConfig(): Promise<AuthConfig> {
  let response: Response;
  try {
    response = await fetch("/api/auth/config", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
  } catch {
    return DEFAULT_AUTH_CONFIG;
  }

  if (!response.ok) return DEFAULT_AUTH_CONFIG;

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) return DEFAULT_AUTH_CONFIG;

  try {
    const raw = (await response.json()) as Partial<AuthConfig>;
    return {
      providerName:
        typeof raw.providerName === "string" && raw.providerName
          ? raw.providerName
          : DEFAULT_AUTH_CONFIG.providerName,
      authenticationEnabled:
        typeof raw.authenticationEnabled === "boolean"
          ? raw.authenticationEnabled
          : DEFAULT_AUTH_CONFIG.authenticationEnabled,
      loginPath:
        typeof raw.loginPath === "string" && raw.loginPath
          ? raw.loginPath
          : DEFAULT_AUTH_CONFIG.loginPath,
      callbackPath:
        typeof raw.callbackPath === "string" && raw.callbackPath
          ? raw.callbackPath
          : DEFAULT_AUTH_CONFIG.callbackPath,
      logoutPath:
        typeof raw.logoutPath === "string" && raw.logoutPath
          ? raw.logoutPath
          : DEFAULT_AUTH_CONFIG.logoutPath,
      signupEnabled:
        typeof raw.signupEnabled === "boolean"
          ? raw.signupEnabled
          : DEFAULT_AUTH_CONFIG.signupEnabled,
      signupUrl:
        typeof raw.signupUrl === "string" && raw.signupUrl ? raw.signupUrl : null,
      passwordChangeUrl:
        typeof raw.passwordChangeUrl === "string" && raw.passwordChangeUrl
          ? raw.passwordChangeUrl
          : null,
      sessionMaxAgeSeconds:
        typeof raw.sessionMaxAgeSeconds === "number" && raw.sessionMaxAgeSeconds > 0
          ? raw.sessionMaxAgeSeconds
          : DEFAULT_AUTH_CONFIG.sessionMaxAgeSeconds,
    };
  } catch {
    return DEFAULT_AUTH_CONFIG;
  }
}

/** The verified identity of the current session, or `null` when there is none. */
export async function fetchPrincipal(): Promise<Principal | null> {
  let response: Response;
  try {
    response = await fetch("/api/auth/whoami", {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
  } catch {
    return null;
  }

  if (!response.ok) return null;

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    // Something other than the API answered (an HTML error page, for
    // instance). There is no usable identity in that case.
    return null;
  }

  try {
    return (await response.json()) as Principal;
  } catch {
    return null;
  }
}

/**
 * Start a new session.
 *
 * The browser is sent to the backend's login endpoint on this origin. The
 * backend then redirects to Authentik, which shows its real login form.
 */
export function signIn(config?: AuthConfig | null): void {
  const path = config?.loginPath || DEFAULT_AUTH_CONFIG.loginPath;
  window.location.assign(path);
}

/**
 * End the application session.
 *
 * This ends *this application's* session: the backend clears its own
 * session cookie. It is deliberately not presented as a global
 * single-sign-on logout -- the user stays signed in to Authentik and to
 * every other application that uses it, unless the Authentik provider's
 * invalidation flow is configured to end that session too.
 *
 * Returns false when the logout request could not be completed.
 */
export async function signOut(config?: AuthConfig | null): Promise<boolean> {
  const path = config?.logoutPath || DEFAULT_AUTH_CONFIG.logoutPath;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    });
    return response.ok;
  } catch {
    return false;
  }
}

/**
 * Start a sign-up.
 *
 * Sign-up is Authentik's own enrollment flow: the browser is sent to the
 * enrollment flow on the Authentik server, which asks for the account
 * details and creates the user. This application never sees or stores a
 * password.
 *
 * Returns false when no enrollment flow is configured, so the caller can
 * hide the action instead of sending the user nowhere.
 */
export function signUp(config?: AuthConfig | null): boolean {
  const url = config?.signupUrl;
  if (!url) return false;
  window.location.assign(url);
  return true;
}

/**
 * Open Authentik's password-change flow.
 *
 * The password is entered on the Authentik server, never here: this
 * application does not collect, transmit or store passwords.
 *
 * Returns false when no password-change flow is configured, so the caller
 * can hide the action instead of sending the user nowhere.
 */
export function changePassword(config?: AuthConfig | null): boolean {
  const url = config?.passwordChangeUrl;
  if (!url) return false;
  window.location.assign(url);
  return true;
}
