/**
 * auth.test.ts
 *
 * The browser side of the authentication: starting a sign-in and ending a
 * session.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_AUTH_CONFIG,
  fetchAuthConfig,
  fetchPrincipal,
  signIn,
  signOut,
} from "./auth";

function stubLocation() {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    value: { assign, href: "http://localhost:5173/", search: "" },
    writable: true,
    configurable: true,
  });
  return assign;
}

describe("signIn", () => {
  beforeEach(() => {
    stubLocation();
  });

  it("sends the browser to the backend's login path exactly once", () => {
    signIn({ ...DEFAULT_AUTH_CONFIG, loginPath: "/api/auth/login" });
    expect(window.location.assign).toHaveBeenCalledTimes(1);
    expect(window.location.assign).toHaveBeenCalledWith("/api/auth/login");
  });

  it("falls back to the default login path", () => {
    signIn(null);
    expect(window.location.assign).toHaveBeenCalledWith(DEFAULT_AUTH_CONFIG.loginPath);
  });
});

describe("signOut", () => {
  beforeEach(() => {
    stubLocation();
  });

  it("posts to the logout path and hands back the provider's end-session URL", async () => {
    const endSessionUrl =
      "http://localhost:9000/application/o/grafana-ai-agent/end-session/" +
      "?client_id=abc&post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A5173%2F" +
      "&id_token_hint=a.b.c";

    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok", endSessionUrl }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await signOut({ ...DEFAULT_AUTH_CONFIG, logoutPath: "/api/auth/logout" });

    expect(result.ok).toBe(true);
    expect(result.endSessionUrl).toBe(endSessionUrl);

    const [path, options] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/auth/logout");
    expect(options.method).toBe("POST");
    // The session cookie has to travel with the request.
    expect(options.credentials).toBe("same-origin");
  });

  it("reports a failed logout instead of pretending it worked", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    const result = await signOut();
    expect(result.ok).toBe(false);
  });

  it("reports a failed logout when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const result = await signOut();
    expect(result.ok).toBe(false);
  });
});

describe("fetchAuthConfig", () => {
  it("uses the configuration the backend publishes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => ({
          providerName: "Authentik",
          authenticationEnabled: true,
          loginPath: "/api/auth/login",
          callbackPath: "/api/auth/callback",
          logoutPath: "/api/auth/logout",
          signupEnabled: false,
          signupUrl: null,
          passwordChangeUrl: null,
          sessionMaxAgeSeconds: 3600,
        }),
      })
    );

    const config = await fetchAuthConfig();
    expect(config.loginPath).toBe("/api/auth/login");
    expect(config.logoutPath).toBe("/api/auth/logout");
    expect(config.signupEnabled).toBe(false);
  });

  it("falls back to the defaults when the backend cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    const config = await fetchAuthConfig();
    expect(config).toEqual(DEFAULT_AUTH_CONFIG);
  });
});

describe("fetchPrincipal", () => {
  it("returns null when there is no session", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    expect(await fetchPrincipal()).toBeNull();
  });

  it("returns the identity of the signed-in user", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => ({ issuer: "http://issuer", subject: "u1", authenticated: true }),
      })
    );
    const principal = await fetchPrincipal();
    expect(principal?.subject).toBe("u1");
  });
});
