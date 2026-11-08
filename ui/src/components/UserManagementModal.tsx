/**
 * UserManagementModal.tsx
 *
 * Administrator view for listing all registered users, roles, statuses,
 * and scoped permissions stored in the User & Role store (Section 5.3),
 * with inline role assignment and status activation (Section 5.2).
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export type AdminUser = {
  user_id: string;
  display_name: string | null;
  username: string | null;
  email: string | null;
  role: string | null;
  status: "active" | "pending" | "deactivated" | string;
  authz_version: number;
  created_at: string | null;
  last_seen_at: string | null;
  scopes: {
    allowed_folder_uids: string[];
    allowed_dashboard_uids: string[];
    denied_dashboard_uids: string[];
    allowed_environments: string[];
  };
};

interface UserManagementModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function UserManagementModal({ isOpen, onClose }: UserManagementModalProps) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingUserId, setEditingUserId] = useState<string | null>(null);
  const [editRole, setEditRole] = useState<string>("observer");
  const [editStatus, setEditStatus] = useState<string>("active");
  const [editFolders, setEditFolders] = useState<string>("");
  const [editDashboards, setEditDashboards] = useState<string>("");
  const [editEnvs, setEditEnvs] = useState<string>("");
  const [availableFolders, setAvailableFolders] = useState<Array<{ uid: string; title: string }>>([
    { uid: "*", title: "All Folders (*)" },
  ]);
  const [saving, setSaving] = useState(false);

  function loadUsers() {
    setLoading(true);
    setError(null);

    fetch("/api/v1/admin/users", {
      headers: { Accept: "application/json" },
    })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${res.status}: ${res.statusText}`);
        }
        return res.json();
      })
      .then((data) => {
        setUsers(data.users || []);
        setLoading(false);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }

  useEffect(() => {
    if (isOpen) {
      loadUsers();
      setEditingUserId(null);

      fetch("/api/v1/admin/folders", {
        headers: { Accept: "application/json" },
      })
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (data?.folders?.length) {
            setAvailableFolders(data.folders);
          }
        })
        .catch(() => {});
    }
  }, [isOpen]);

  async function handleSaveAccess(user: AdminUser) {
    setSaving(true);
    setError(null);

    const foldersList = editFolders
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const dashboardsList = editDashboards
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const envsList = editEnvs
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const allowedFolders =
      editRole === "admin"
        ? (foldersList.length ? foldersList : ["*"])
        : editRole === "observer"
        ? []
        : (foldersList.length ? foldersList : ["*"]);

    const allowedDashboards =
      editRole === "observer" ? [] : dashboardsList;

    const allowedEnvironments =
      editRole === "admin"
        ? (envsList.length ? envsList : ["*"])
        : editRole === "observer"
        ? []
        : (envsList.length ? envsList : ["staging", "production"]);

    try {
      const res = await fetch(`/api/v1/admin/users/${user.user_id}/access`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          role: editRole,
          status: editStatus,
          scopes: {
            allowed_folder_uids: allowedFolders,
            allowed_dashboard_uids: allowedDashboards,
            denied_dashboard_uids: user.scopes?.denied_dashboard_uids || [],
            allowed_environments: allowedEnvironments,
          },
        }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `HTTP ${res.status}: ${res.statusText}`);
      }

      setEditingUserId(null);
      loadUsers();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  function startEdit(u: AdminUser) {
    setEditingUserId(u.user_id);
    const role = u.role || "observer";
    setEditRole(role);
    setEditStatus(u.status === "pending" ? "active" : u.status);
    const initialFolders = u.scopes?.allowed_folder_uids?.length
      ? u.scopes.allowed_folder_uids.join(", ")
      : (role === "admin" ? "*" : (role === "maintainer" || role === "operator" ? "*" : ""));
    setEditFolders(initialFolders);
    setEditDashboards(
      u.scopes?.allowed_dashboard_uids?.length
        ? u.scopes.allowed_dashboard_uids.join(", ")
        : (role === "admin" ? "*" : "")
    );
    setEditEnvs(
      u.scopes?.allowed_environments?.length
        ? u.scopes.allowed_environments.join(", ")
        : (role === "admin" ? "*" : "staging, production")
    );
  }

  function handleRoleChange(newRole: string) {
    setEditRole(newRole);
    if (newRole === "maintainer" || newRole === "operator") {
      if (!editFolders.trim()) {
        setEditFolders("*");
      }
      if (!editEnvs.trim()) {
        setEditEnvs("staging, production");
      }
    } else if (newRole === "admin") {
      if (!editFolders.trim()) setEditFolders("*");
      if (!editDashboards.trim()) setEditDashboards("*");
      if (!editEnvs.trim()) setEditEnvs("*");
    }
  }

  if (!isOpen) return null;

  return createPortal(
    <div className="admin-modal-backdrop" onClick={onClose} role="dialog" aria-modal="true">
      <div className="admin-modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="admin-modal-header">
          <div>
            <h2 className="admin-modal-title">👥 Team & RBAC Users</h2>
            <p className="muted small">
              Managed identities, roles, and resource scopes from PostgreSQL User & Role Store
            </p>
          </div>
          <button type="button" className="btn ghost small" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        {error && (
          <div className="alert error" style={{ margin: "16px 20px 0" }}>
            {error}
          </div>
        )}

        <div className="admin-modal-body">
          {loading && !users.length ? (
            <div style={{ padding: "40px 0", textAlign: "center" }} className="muted">
              Loading users from store…
            </div>
          ) : (
            <div className="admin-table-wrap">
              <table className="admin-users-table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Resource Scopes</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => {
                    const isEditing = editingUserId === u.user_id;

                    const isElevatedRole = editRole === "maintainer" || editRole === "operator";
                    const isAdminRole = editRole === "admin";
                    const isFolderActive = isElevatedRole || isAdminRole;

                    const statusBadgeClass =
                      u.status === "active"
                        ? "badge-active"
                        : u.status === "pending"
                        ? "badge-pending"
                        : "badge-deactivated";

                    const roleBadgeClass =
                      u.role === "admin"
                        ? "role-admin"
                        : u.role === "maintainer"
                        ? "role-maintainer"
                        : u.role === "operator"
                        ? "role-operator"
                        : u.role === "observer"
                        ? "role-observer"
                        : "role-none";

                    return (
                      <tr key={u.user_id} className={isEditing ? "editing-row" : ""}>
                        <td>
                          <div className="admin-user-cell">
                            <span className="admin-user-name">
                              {u.display_name || u.username || u.user_id}
                            </span>
                            {u.username && (
                              <span className="muted small">@{u.username}</span>
                            )}
                          </div>
                        </td>
                        <td>
                          <span className="small">{u.email || "—"}</span>
                        </td>
                        <td>
                          {isEditing ? (
                            <select
                              value={editRole}
                              onChange={(e) => handleRoleChange(e.target.value)}
                              className="admin-select"
                              disabled={saving}
                            >
                              <option value="observer">Observer</option>
                              <option value="operator">Operator</option>
                              <option value="maintainer">Maintainer</option>
                              <option value="admin">Admin</option>
                            </select>
                          ) : (
                            <span className={`admin-badge ${roleBadgeClass}`}>
                              {u.role ? u.role.toUpperCase() : "NO ROLE"}
                            </span>
                          )}
                        </td>
                        <td>
                          {isEditing ? (
                            <select
                              value={editStatus}
                              onChange={(e) => setEditStatus(e.target.value)}
                              className="admin-select"
                              disabled={saving}
                            >
                              <option value="active">Active</option>
                              <option value="pending">Pending</option>
                              <option value="deactivated">Deactivated</option>
                            </select>
                          ) : (
                            <span className={`admin-badge ${statusBadgeClass}`}>
                              {u.status}
                            </span>
                          )}
                        </td>
                        <td>
                          {isEditing ? (
                            <div className={`admin-scope-edit-box ${isFolderActive ? "active" : "inactive"}`}>
                              <div className="admin-scope-edit-header">
                                <span className="admin-scope-edit-title">Resource Scopes:</span>
                                {isElevatedRole && (
                                  <span className="admin-scope-pill-active">
                                    ● Active ({editRole})
                                  </span>
                                )}
                                {isAdminRole && (
                                  <span className="admin-scope-pill-admin">
                                    ★ Admin
                                  </span>
                                )}
                                {!isFolderActive && (
                                  <span className="admin-scope-pill-disabled">
                                    🔒 Inactive (Observer)
                                  </span>
                                )}
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Folders:
                                </label>
                                <input
                                  type="text"
                                  className={`admin-scope-input ${isFolderActive ? "input-active" : "input-disabled"}`}
                                  placeholder={isFolderActive ? "e.g. *, fld-payments (comma-separated)" : "Elevate role to activate"}
                                  value={editFolders}
                                  onChange={(e) => setEditFolders(e.target.value)}
                                  disabled={!isFolderActive || saving}
                                />
                                {isFolderActive && (
                                  <div className="admin-folder-quick-chips">
                                    <span className="muted x-small">Quick add:</span>
                                    {availableFolders.map((f) => (
                                      <button
                                        key={f.uid}
                                        type="button"
                                        className="admin-folder-chip"
                                        disabled={saving}
                                        onClick={() => {
                                          const current = editFolders
                                            .split(",")
                                            .map((s) => s.trim())
                                            .filter(Boolean);
                                          if (!current.includes(f.uid)) {
                                            setEditFolders(current.length ? `${editFolders}, ${f.uid}` : f.uid);
                                          }
                                        }}
                                        title={`Add folder ${f.title}`}
                                      >
                                        + {f.title}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Dashboards:
                                </label>
                                <input
                                  type="text"
                                  className={`admin-scope-input ${isFolderActive ? "input-active" : "input-disabled"}`}
                                  placeholder={isFolderActive ? "e.g. dash-checkout-slo, * (optional)" : "Elevate role to activate"}
                                  value={editDashboards}
                                  onChange={(e) => setEditDashboards(e.target.value)}
                                  disabled={!isFolderActive || saving}
                                />
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Envs:
                                </label>
                                <input
                                  type="text"
                                  className={`admin-scope-input ${isFolderActive ? "input-active" : "input-disabled"}`}
                                  placeholder={isFolderActive ? "e.g. staging, production, *" : "Elevate role to activate"}
                                  value={editEnvs}
                                  onChange={(e) => setEditEnvs(e.target.value)}
                                  disabled={!isFolderActive || saving}
                                />
                                {isFolderActive && (
                                  <div className="admin-folder-quick-chips">
                                    <span className="muted x-small">Quick add:</span>
                                    {["staging", "production", "*"].map((env) => (
                                      <button
                                        key={env}
                                        type="button"
                                        className="admin-folder-chip"
                                        disabled={saving}
                                        onClick={() => {
                                          const current = editEnvs
                                            .split(",")
                                            .map((s) => s.trim())
                                            .filter(Boolean);
                                          if (!current.includes(env)) {
                                            setEditEnvs(current.length ? `${editEnvs}, ${env}` : env);
                                          }
                                        }}
                                      >
                                        + {env}
                                      </button>
                                    ))}
                                  </div>
                                )}
                              </div>

                              {!isFolderActive && (
                                <div className="muted x-small" style={{ marginTop: "4px" }}>
                                  Observers have read-only catalog access. Elevate to Maintainer or Operator to activate resource scopes.
                                </div>
                              )}
                            </div>
                          ) : (
                            <div className="admin-scopes-cell">
                              <div className="admin-scope-item">
                                <span className="admin-scope-label">Folders:</span>{" "}
                                <code>
                                  {u.scopes?.allowed_folder_uids?.length
                                    ? u.scopes.allowed_folder_uids.join(", ")
                                    : u.role === "admin"
                                    ? "*"
                                    : "—"}
                                </code>
                              </div>
                              <div className="admin-scope-item">
                                <span className="admin-scope-label">Dashboards:</span>{" "}
                                <code>
                                  {u.scopes?.allowed_dashboard_uids?.length
                                    ? u.scopes.allowed_dashboard_uids.join(", ")
                                    : u.role === "admin"
                                    ? "*"
                                    : "—"}
                                </code>
                              </div>
                              <div className="admin-scope-item">
                                <span className="admin-scope-label">Envs:</span>{" "}
                                <code>
                                  {u.scopes?.allowed_environments?.length
                                    ? u.scopes.allowed_environments.join(", ")
                                    : u.role === "admin"
                                    ? "*"
                                    : "—"}
                                </code>
                              </div>
                              {u.scopes?.denied_dashboard_uids?.length > 0 && (
                                <div className="admin-scope-item">
                                  <span className="admin-scope-label" style={{ color: "var(--red)" }}>
                                    Denied:
                                  </span>{" "}
                                  <code>{u.scopes.denied_dashboard_uids.join(", ")}</code>
                                </div>
                              )}
                            </div>
                          )}
                        </td>
                        <td>
                          {isEditing ? (
                            <div style={{ display: "flex", gap: "6px" }}>
                              <button
                                type="button"
                                className="btn primary small"
                                onClick={() => handleSaveAccess(u)}
                                disabled={saving}
                              >
                                {saving ? "Saving…" : "Save"}
                              </button>
                              <button
                                type="button"
                                className="btn ghost small"
                                onClick={() => setEditingUserId(null)}
                                disabled={saving}
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              className={u.status === "pending" ? "btn primary small" : "btn ghost small"}
                              onClick={() => startEdit(u)}
                            >
                              {u.status === "pending" ? "⚡ Activate" : "Edit"}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="admin-modal-footer">
          <span className="muted small">
            Endpoint: <code>/api/v1/admin/users</code> • Total: {users.length} registered
          </span>
          <button type="button" className="btn secondary small" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

export default UserManagementModal;
