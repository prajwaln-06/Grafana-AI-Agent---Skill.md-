/**
 * LoginScreen.tsx
 *
 * The branded login screen.
 *
 * Authentication happens in Authentik, so this screen does not collect a
 * password and does not talk to any password API. It offers the two things
 * a visitor can do:
 *
 *   Login with Authentik -- the backend starts the OIDC Authorization Code
 *                            flow and Authentik shows its real login form.
 *   Create account        -- Authentik's own enrollment flow, which creates
 *                            the account. This application never sees a
 *                            password.
 */
import { useEffect, useState } from "react";

import { signIn, signUp } from "../auth";
import type { AuthConfig } from "../auth";

export type LoginScreenProps = {
  config: AuthConfig | null;
  /** Set when the session ended rather than never having existed. */
  expired?: boolean;
  /** A message from a failed login attempt (from the callback redirect). */
  error?: string | null;
  onSignedOut?: () => void;
};

/**
 * The error codes the backend puts into the redirect after a failed login,
 * and what they mean for the user.
 */
const AUTH_ERRORS: Record<string, string> = {
  authentication_disabled:
    "Authentication is disabled on this deployment, so there is nothing to sign in to.",
  provider_error: "The identity provider rejected the sign-in attempt.",
  login_state_missing:
    "The sign-in attempt expired or was started in another browser. Please try again.",
  state_mismatch:
    "The sign-in attempt did not match this browser's session. Please try again.",
  missing_code: "The identity provider did not return an authorization code.",
  missing_nonce: "The sign-in attempt was incomplete. Please try again.",
  missing_id_token: "The identity provider did not return an ID token.",
  token_exchange_failed: "The authorization code could not be exchanged for a token.",
  id_token_invalid: "The identity token returned by the provider could not be verified.",
  missing_subject: "The identity token did not identify a user.",
};

export function describeAuthError(code: string | null): string | null {
  if (!code) return null;
  return (
    AUTH_ERRORS[code] ||
    "The sign-in attempt could not be completed. Please try again."
  );
}

export function LoginScreen({ config, expired = false, error, onSignedOut }: LoginScreenProps) {
  const [busy, setBusy] = useState<"signin" | "signup" | null>(null);

  useEffect(() => {
    setBusy(null);
  }, [config?.signupUrl]);

  const canSignUp = Boolean(config?.signupUrl);
  const providerName = config?.providerName || "Authentik";

  function handleSignIn() {
    setBusy("signin");
    signIn(config);
  }

  function handleSignUp() {
    if (!config) return;
    setBusy("signup");
    signUp(config);
  }

  return (
    <div className="auth-screen">
      <div className="login-card">
        <div className="login-brand">
          <span className="login-logo" aria-hidden="true">
            <BrandMark />
          </span>
          <div className="login-brand-text">
            <h1>Observability Lab</h1>
            <p className="muted small">AI agent for Prometheus, Loki and OpenSearch</p>
          </div>
        </div>

        <h2 className="login-title">Login</h2>

        <p className="login-lead">
          {expired
            ? "Your session has ended. Sign in again to return to the dashboard."
            : "Sign in to reach the AI agent dashboard, your dashboards and your saved queries."}
        </p>

        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}

        <div className="login-actions">
          <button
            type="button"
            className="btn login-primary"
            onClick={handleSignIn}
            disabled={busy !== null}
          >
            {busy === "signin" ? "Redirecting…" : `Login with ${providerName}`}
          </button>
        </div>

        <p className="login-signup">
          {canSignUp ? (
            <>
              <span className="muted small">No account yet? </span>
              <button
                type="button"
                className="link-button"
                onClick={handleSignUp}
                disabled={busy !== null}
              >
                {busy === "signup" ? "Redirecting…" : "Create account"}
              </button>
            </>
          ) : (
            <span className="muted small">
              Self-service sign-up is not enabled on this deployment: no Authentik enrollment
              flow is configured. Ask an administrator for an account.
            </span>
          )}
        </p>

        <div className="login-footer">
          <span className="muted small">
            Identity is managed by {providerName}. Your password is never handled by this
            application.
          </span>
          {onSignedOut && (
            <button type="button" className="btn ghost small" onClick={onSignedOut}>
              Dismiss
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** The application's mark: a small stylised series with an eye-like lens. */
function BrandMark() {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true">
      <path d="M3 22.5 9.5 15l5 4.5L21 10l8 9.5" />
      <path d="M3 27h26" />
      <circle cx="16" cy="9" r="3.5" />
    </svg>
  );
}

export default LoginScreen;
