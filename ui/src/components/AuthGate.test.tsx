/**
 * AuthGate.test.tsx
 *
 * The sign-in and sign-out behaviour of the React side: the login screen
 * starts exactly one sign-in, and signing out hands the browser over to the
 * provider's end-session URL so that the SSO session ends too.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider, RequireAuth, useAuth } from "./AuthGate";

const END_SESSION_URL =
  "http://localhost:9000/application/o/grafana-ai-agent/end-session/" +
  "?client_id=abc&post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A5173%2F" +
  "&id_token_hint=a.b.c";

const PRINCIPAL = {
  issuer: "http://localhost:9000/application/o/grafana-ai-agent/",
  subject: "user-1",
  authenticated: true,
  username: "jaidev",
  email: "jaidev@example.com",
};

const CONFIG = {
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

function stubLocation() {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    value: { assign, href: "http://localhost:5173/", search: "" },
    writable: true,
    configurable: true,
  });
  return assign;
}

function stubFetch(overrides: Record<string, unknown> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.startsWith("/api/auth/whoami")) {
      return { ok: true, headers: { get: () => "application/json" }, json: async () => PRINCIPAL };
    }
    if (path.startsWith("/api/auth/config")) {
      return { ok: true, headers: { get: () => "application/json" }, json: async () => CONFIG };
    }
    if (path.startsWith("/api/auth/logout")) {
      return {
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => ({ status: "ok", endSessionUrl: END_SESSION_URL }),
      };
    }
    return overrides[path] ?? { ok: false };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** A child that exposes the context so the test can drive it. */
function Probe() {
  const { status, signOut } = useAuth();
  return (
    <div>
      <span data-testid="status">{status}</span>
      <button type="button" onClick={signOut}>
        Sign out
      </button>
    </div>
  );
}

describe("AuthGate", () => {
  beforeEach(() => {
    stubLocation();
    stubFetch();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("shows the login screen to a visitor without a session", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.startsWith("/api/auth/whoami")) return { ok: false };
        if (path.startsWith("/api/auth/config")) {
          return {
            ok: true,
            headers: { get: () => "application/json" },
            json: async () => CONFIG,
          };
        }
        return { ok: false };
      })
    );

    render(
      <AuthProvider>
        <RequireAuth>
          <p>the application</p>
        </RequireAuth>
      </AuthProvider>
    );

    expect(await screen.findByRole("button", { name: /Login with Authentik/i })).toBeTruthy();
    expect(screen.queryByText("the application")).toBeNull();
  });

  it("starts exactly one sign-in when the login button is used", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path.startsWith("/api/auth/whoami")) return { ok: false };
        if (path.startsWith("/api/auth/config")) {
          return {
            ok: true,
            headers: { get: () => "application/json" },
            json: async () => CONFIG,
          };
        }
        return { ok: false };
      })
    );

    render(
      <AuthProvider>
        <RequireAuth>
          <p>the application</p>
        </RequireAuth>
      </AuthProvider>
    );

    const button = await screen.findByRole("button", { name: /Login with Authentik/i });
    fireEvent.click(button);

    expect(window.location.assign).toHaveBeenCalledTimes(1);
    expect(window.location.assign).toHaveBeenCalledWith("/api/auth/login");
  });

  it("signs out by handing the browser to the provider's end-session URL", async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await waitFor(() => expect(screen.getByTestId("status").textContent).toBe("authenticated"));
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(window.location.assign).toHaveBeenCalledTimes(1));
    // The browser goes to the provider, which ends the SSO session and
    // returns the user to the application's login screen.
    expect(window.location.assign).toHaveBeenCalledWith(END_SESSION_URL);
  });
});
