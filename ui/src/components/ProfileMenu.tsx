/**
 * ProfileMenu.tsx
 *
 * The circular profile button in the top-right corner of the dashboard.
 *
 * Clicking it opens a small panel that shows the signed-in user's details.
 * The email shown there is not taken from a cached value: it is read from
 * /api/auth/whoami when the panel is opened, so it is the address
 * Authentik actually verified for this session.
 */
import { useEffect, useRef, useState } from "react";

import { changePassword, fetchPrincipal } from "../auth";
import type { Principal } from "../auth";
import { useAuth } from "./AuthGate";
import { UserManagementModal } from "./UserManagementModal";

export function ProfileMenu() {
  const { principal, devMode, config, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const [details, setDetails] = useState<Principal | null>(principal);
  const [loading, setLoading] = useState(false);
  const [showUsersModal, setShowUsersModal] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;

    function onPointerDown(event: MouseEvent) {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Read the verified identity from the backend every time the panel is
  // opened: the panel must show the account's real email address.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    void fetchPrincipal().then((resolved) => {
      if (cancelled) return;
      setDetails(resolved);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const shown = details ?? principal;
  const initials = initialsOf(shown);
  const email = shown?.email || null;
  const isAdmin = shown?.role === "admin";

  return (
    <>
      <div className="profile-menu" ref={wrapRef}>
        <button
          type="button"
          className="profile-button"
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={email ? `Account: ${email}` : "Account"}
          title={email || "Account"}
          onClick={() => setOpen((value) => !value)}
        >
          <span aria-hidden="true">{initials}</span>
        </button>

        {open && (
          <div className="profile-panel" role="dialog" aria-label="Signed-in account">
            <div className="profile-panel-head">
              <span className="profile-panel-avatar" aria-hidden="true">
                {initials}
              </span>
              <div className="profile-panel-identity">
                <span className="profile-panel-name">
                  {shown?.name || shown?.username || "Signed in"}
                </span>
                <span className="profile-panel-email muted small" title={email || undefined}>
                  {loading && !email ? "Loading…" : email || "No email address on file"}
                </span>
              </div>
            </div>

            {shown?.role && (
              <div className="profile-panel-row">
                <span className="muted small">Role</span>
                <span
                  className={`admin-badge ${
                    shown.role === "admin"
                      ? "role-admin"
                      : shown.role === "maintainer"
                      ? "role-maintainer"
                      : shown.role === "operator"
                      ? "role-operator"
                      : "role-observer"
                  }`}
                >
                  {shown.role.toUpperCase()}
                </span>
              </div>
            )}

            {shown?.username && (
              <div className="profile-panel-row">
                <span className="muted small">Username</span>
                <span className="small">{shown.username}</span>
              </div>
            )}

            {isAdmin && (
              <div style={{ marginTop: 12, paddingTop: 10, borderTop: "1px solid var(--border)" }}>
                <button
                  type="button"
                  className="btn secondary small"
                  style={{ width: "100%", justifyContent: "center" }}
                  onClick={() => {
                    setOpen(false);
                    setShowUsersModal(true);
                  }}
                >
                  👥 Manage Team & Users
                </button>
              </div>
            )}

            {!devMode && (
              <div className="profile-panel-actions">
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={() => changePassword(config)}
                  disabled={!config.passwordChangeUrl}
                  title={
                    config.passwordChangeUrl
                      ? "Change your password in Authentik"
                      : "No Authentik password-change flow is configured for this deployment"
                  }
                >
                  Change password
                </button>
                <button
                  type="button"
                  className="btn ghost small"
                  onClick={signOut}
                >
                  Sign out
                </button>
              </div>
            )}

            {!devMode && (
              <p className="muted small profile-panel-note">
                Signing out ends this application's session and your {config.providerName || "Authentik"}{" "}
                session, and returns you to the login screen.
              </p>
            )}
          </div>
        )}
      </div>

      <UserManagementModal
        isOpen={showUsersModal}
        onClose={() => setShowUsersModal(false)}
      />
    </>
  );
}

/** Up to two initials for the avatar, derived from the user's name. */
function initialsOf(principal: Principal | null): string {
  if (!principal) return "?";
  const source = principal.name || principal.username || principal.email || "";
  const parts = source.trim().split(/[\s@._-]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const letters = parts.slice(0, 2).map((part) => part.charAt(0).toUpperCase());
  return letters.join("") || "?";
}

export default ProfileMenu;
