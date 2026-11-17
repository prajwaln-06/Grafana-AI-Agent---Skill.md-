/**
 * UserManagementModal.tsx
 *
 * Administrator view for listing all registered users, roles, statuses,
 * and scoped permissions stored in the User & Role store (Section 5.3),
 * with inline role assignment and status activation (Section 5.2).
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { getActAsUser, setActAsUser } from "../api";

export type RoleGrantItem = {
  id: string;
  role: string;
  folders: string;
  dashboards: string;
  environments: string;
};

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
    role_grants?: Array<{
      role: string;
      allowed_folder_uids?: string[];
      allowed_dashboard_uids?: string[];
      allowed_environments?: string[];
    }>;
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
  const [editRoleGrants, setEditRoleGrants] = useState<RoleGrantItem[]>([]);
  const [availableFolders, setAvailableFolders] = useState<Array<{ uid: string; title: string }>>([
    { uid: "*", title: "All Folders (*)" },
  ]);
  const [saving, setSaving] = useState(false);
  const [activeActAs, setActiveActAs] = useState<string | null>(() => getActAsUser());

  function handleToggleActAs(u: AdminUser) {
    if (activeActAs === u.user_id) {
      setActAsUser(null);
      setActiveActAs(null);
    } else {
      setActAsUser(u.user_id);
      setActiveActAs(u.user_id);
    }
  }

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

    const primaryFolders = foldersList;
    const primaryDashboards = dashboardsList;
    const primaryEnvs = envsList.length ? envsList : (editRole === "admin" ? ["*"] : ["staging", "production"]);

    const additionalGrants = editRoleGrants.map((g) => ({
      role: g.role,
      allowed_folder_uids: g.folders.split(",").map((s) => s.trim()).filter(Boolean),
      allowed_dashboard_uids: g.dashboards.split(",").map((s) => s.trim()).filter(Boolean),
      allowed_environments: g.environments.split(",").map((s) => s.trim()).filter(Boolean),
    }));

    const allGrants = [
      {
        role: editRole,
        allowed_folder_uids: editRole === "admin" && !primaryFolders.length ? ["*"] : primaryFolders,
        allowed_dashboard_uids: primaryDashboards,
        allowed_environments: primaryEnvs,
      },
      ...additionalGrants,
    ];

    const totalFolders = Array.from(new Set(allGrants.flatMap((g) => g.allowed_folder_uids)));
    const totalDashboards = Array.from(new Set(allGrants.flatMap((g) => g.allowed_dashboard_uids)));
    const totalEnvs = Array.from(new Set(allGrants.flatMap((g) => g.allowed_environments)));

    const allowedFolders = editRole === "admin" && !totalFolders.length ? ["*"] : totalFolders;
    const allowedDashboards = totalDashboards;
    const allowedEnvironments = totalEnvs.length ? totalEnvs : ["staging", "production"];

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
            role_grants: allGrants,
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

    const rawGrants = u.scopes?.role_grants || [];
    if (rawGrants.length > 0) {
      const primary = rawGrants[0];
      setEditRole(primary.role || role);
      setEditFolders((primary.allowed_folder_uids || []).join(", "));
      setEditDashboards((primary.allowed_dashboard_uids || []).join(", "));
      setEditEnvs((primary.allowed_environments || []).join(", ") || (role === "admin" ? "*" : "staging, production"));

      const additionals = rawGrants.slice(1).map((g, idx) => ({
        id: String(Date.now() + idx),
        role: g.role || "observer",
        folders: (g.allowed_folder_uids || []).join(", "),
        dashboards: (g.allowed_dashboard_uids || []).join(", "),
        environments: (g.allowed_environments || []).join(", ") || "staging, production",
      }));
      setEditRoleGrants(additionals);
    } else {
      const initialFolders = u.scopes?.allowed_folder_uids?.length
        ? u.scopes.allowed_folder_uids.join(", ")
        : (role === "admin" ? "*" : "");
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
      setEditRoleGrants([]);
    }
  }

  function handleRoleChange(newRole: string) {
    setEditRole(newRole);
    if (newRole === "maintainer" || newRole === "operator") {
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
                            editRoleGrants.length > 0 ? (
                              <div style={{ display: "flex", flexDirection: "column", gap: "5px" }}>
                                <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                                  <span className="muted x-small" style={{ minWidth: "44px", fontWeight: 600 }}>Role 1:</span>
                                  <select
                                    value={editRole}
                                    onChange={(e) => handleRoleChange(e.target.value)}
                                    className="admin-select small"
                                    disabled={saving}
                                  >
                                    <option value="maintainer">Maintainer</option>
                                    <option value="operator">Operator</option>
                                    <option value="observer">Observer</option>
                                    <option value="admin">Admin</option>
                                  </select>
                                </div>
                                {editRoleGrants.map((grant, idx) => (
                                  <div key={grant.id} style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                                    <span className="muted x-small" style={{ minWidth: "44px", fontWeight: 600 }}>Role {idx + 2}:</span>
                                    <select
                                      value={grant.role}
                                      onChange={(e) => {
                                        const next = [...editRoleGrants];
                                        next[idx] = { ...next[idx], role: e.target.value };
                                        setEditRoleGrants(next);
                                      }}
                                      className="admin-select small"
                                      disabled={saving}
                                    >
                                      <option value="observer">Observer</option>
                                      <option value="operator">Operator</option>
                                      <option value="maintainer">Maintainer</option>
                                    </select>
                                    <button
                                      type="button"
                                      className="btn ghost x-small"
                                      style={{ color: "var(--red)", padding: "1px 4px", fontSize: "0.75rem", lineHeight: 1 }}
                                      title="Remove this role scope"
                                      onClick={() => setEditRoleGrants(editRoleGrants.filter((_, i) => i !== idx))}
                                      disabled={saving}
                                    >
                                      ✕
                                    </button>
                                  </div>
                                ))}
                                {editRole !== "admin" && (
                                  <button
                                    type="button"
                                    className="btn ghost x-small"
                                    style={{ alignSelf: "flex-start", marginTop: "2px", fontWeight: 600, fontSize: "0.72rem", padding: "2px 6px" }}
                                    onClick={() => {
                                      setEditRoleGrants([
                                        ...editRoleGrants,
                                        {
                                          id: String(Date.now()),
                                          role: "observer",
                                          folders: "",
                                          dashboards: "",
                                          environments: "staging, production",
                                        },
                                      ]);
                                    }}
                                    disabled={saving}
                                  >
                                    + Add Role
                                  </button>
                                )}
                              </div>
                            ) : (
                              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
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
                                {editRole !== "admin" && (
                                  <button
                                    type="button"
                                    className="btn ghost small"
                                    style={{ padding: "2px 7px", fontWeight: "bold", fontSize: "0.95rem" }}
                                    title="Add another role scope (e.g. Observer for another folder)"
                                    onClick={() => {
                                      setEditRoleGrants([
                                        ...editRoleGrants,
                                        {
                                          id: String(Date.now()),
                                          role: "observer",
                                          folders: "",
                                          dashboards: "",
                                          environments: "staging, production",
                                        },
                                      ]);
                                    }}
                                  >
                                    +
                                  </button>
                                )}
                              </div>
                            )
                          ) : (
                            <div style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
                              <div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
                                <span className={`admin-badge ${roleBadgeClass}`}>
                                  {u.role ? u.role.toUpperCase() : "NO ROLE"}
                                </span>
                                {u.role !== "admin" && (
                                  <button
                                    type="button"
                                    className="btn ghost x-small"
                                    style={{ padding: "1px 6px", fontWeight: "bold", fontSize: "0.85rem", lineHeight: 1 }}
                                    title={`Add additional role scope (e.g. Observer) for ${u.display_name || u.username}`}
                                    onClick={() => {
                                      startEdit(u);
                                      const existingAdditionals = (u.scopes?.role_grants || []).slice(1).map((g, idx) => ({
                                        id: String(Date.now() + idx),
                                        role: g.role || "observer",
                                        folders: (g.allowed_folder_uids || []).join(", "),
                                        dashboards: (g.allowed_dashboard_uids || []).join(", "),
                                        environments: (g.allowed_environments || []).join(", ") || "staging, production",
                                      }));
                                      setEditRoleGrants([
                                        ...existingAdditionals,
                                        {
                                          id: String(Date.now() + 99),
                                          role: "observer",
                                          folders: "",
                                          dashboards: "",
                                          environments: "staging, production",
                                        },
                                      ]);
                                    }}
                                  >
                                    +
                                  </button>
                                )}
                              </div>
                              {u.scopes?.role_grants && u.scopes.role_grants.length > 1 && (
                                <div style={{ display: "flex", flexWrap: "wrap", gap: "2px" }}>
                                  {u.scopes.role_grants.slice(1).map((g, idx) => (
                                    <span key={idx} className={`admin-badge role-${g.role}`} style={{ fontSize: "0.65rem", padding: "1px 5px" }}>
                                      + {g.role.toUpperCase()}
                                    </span>
                                  ))}
                                </div>
                              )}
                            </div>
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
                            <div className="admin-scope-edit-box active">
                              <div className="admin-scope-edit-header">
                                <span className="admin-scope-edit-title">Scope #1 ({editRole.toUpperCase()}):</span>
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
                                {editRole === "observer" && (
                                  <span className="admin-scope-pill-active" style={{ background: "rgba(59, 130, 246, 0.2)", color: "#3b82f6" }}>
                                    👁 Observer (Read-Only)
                                  </span>
                                )}
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Folders:
                                </label>
                                <input
                                  type="text"
                                  className="admin-scope-input input-active"
                                  placeholder={
                                    editRole === "admin"
                                      ? "e.g. *"
                                      : "e.g. fld-payments, *"
                                  }
                                  value={editFolders}
                                  onChange={(e) => setEditFolders(e.target.value)}
                                  disabled={saving}
                                />
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
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Dashboards:
                                </label>
                                <input
                                  type="text"
                                  className="admin-scope-input input-active"
                                  placeholder="e.g. dash-checkout-slo, * (optional)"
                                  value={editDashboards}
                                  onChange={(e) => setEditDashboards(e.target.value)}
                                  disabled={saving}
                                />
                              </div>

                              <div className="admin-scope-field-group">
                                <label className="admin-scope-edit-label">
                                  Envs:
                                </label>
                                <input
                                  type="text"
                                  className="admin-scope-input input-active"
                                  placeholder="e.g. staging, production, *"
                                  value={editEnvs}
                                  onChange={(e) => setEditEnvs(e.target.value)}
                                  disabled={saving}
                                />
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
                              </div>

                              {/* Additional Role Grants (e.g. Observer access to another folder) */}
                              {editRoleGrants.map((grant, idx) => (
                                <div key={grant.id} className="admin-additional-grant-card">
                                  <div className="admin-grant-card-header">
                                    <span className="admin-scope-edit-title">Scope #{idx + 2} ({grant.role.toUpperCase()}):</span>
                                    <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                                      <select
                                        value={grant.role}
                                        onChange={(e) => {
                                          const next = [...editRoleGrants];
                                          next[idx] = { ...next[idx], role: e.target.value };
                                          setEditRoleGrants(next);
                                        }}
                                        className="admin-select small"
                                        disabled={saving}
                                      >
                                        <option value="observer">Observer (Read-only)</option>
                                        <option value="operator">Operator (Alerts)</option>
                                        <option value="maintainer">Maintainer (Dashboards)</option>
                                      </select>
                                      <button
                                        type="button"
                                        className="btn ghost x-small"
                                        onClick={() => {
                                          setEditRoleGrants(editRoleGrants.filter((_, i) => i !== idx));
                                        }}
                                        title="Remove this role scope"
                                        style={{ color: "var(--red)", padding: "2px 6px" }}
                                      >
                                        ✕
                                      </button>
                                    </div>
                                  </div>

                                  <div className="admin-scope-field-group">
                                    <label className="admin-scope-edit-label">
                                      Folders:
                                    </label>
                                    <input
                                      type="text"
                                      className="admin-scope-input input-active"
                                      placeholder="e.g. fld-alerts, fld-payments"
                                      value={grant.folders}
                                      onChange={(e) => {
                                        const next = [...editRoleGrants];
                                        next[idx] = { ...next[idx], folders: e.target.value };
                                        setEditRoleGrants(next);
                                      }}
                                      disabled={saving}
                                    />
                                    <div className="admin-folder-quick-chips">
                                      <span className="muted x-small">Quick add:</span>
                                      {availableFolders.map((f) => (
                                        <button
                                          key={f.uid}
                                          type="button"
                                          className="admin-folder-chip"
                                          disabled={saving}
                                          onClick={() => {
                                            const current = grant.folders.split(",").map((s) => s.trim()).filter(Boolean);
                                            if (!current.includes(f.uid)) {
                                              const next = [...editRoleGrants];
                                              next[idx] = {
                                                ...next[idx],
                                                folders: current.length ? `${grant.folders}, ${f.uid}` : f.uid,
                                              };
                                              setEditRoleGrants(next);
                                            }
                                          }}
                                          title={`Add folder ${f.title}`}
                                        >
                                          + {f.title}
                                        </button>
                                      ))}
                                    </div>
                                  </div>

                                  <div className="admin-scope-field-group">
                                    <label className="admin-scope-edit-label">
                                      Dashboards:
                                    </label>
                                    <input
                                      type="text"
                                      className="admin-scope-input input-active"
                                      placeholder="e.g. dash-checkout-slo, * (optional)"
                                      value={grant.dashboards}
                                      onChange={(e) => {
                                        const next = [...editRoleGrants];
                                        next[idx] = { ...next[idx], dashboards: e.target.value };
                                        setEditRoleGrants(next);
                                      }}
                                      disabled={saving}
                                    />
                                  </div>

                                  <div className="admin-scope-field-group">
                                    <label className="admin-scope-edit-label">
                                      Envs:
                                    </label>
                                    <input
                                      type="text"
                                      className="admin-scope-input input-active"
                                      placeholder="e.g. staging, production, *"
                                      value={grant.environments}
                                      onChange={(e) => {
                                        const next = [...editRoleGrants];
                                        next[idx] = { ...next[idx], environments: e.target.value };
                                        setEditRoleGrants(next);
                                      }}
                                      disabled={saving}
                                    />
                                    <div className="admin-folder-quick-chips">
                                      <span className="muted x-small">Quick add:</span>
                                      {["staging", "production", "*"].map((env) => (
                                        <button
                                          key={env}
                                          type="button"
                                          className="admin-folder-chip"
                                          disabled={saving}
                                          onClick={() => {
                                            const current = grant.environments.split(",").map((s) => s.trim()).filter(Boolean);
                                            if (!current.includes(env)) {
                                              const next = [...editRoleGrants];
                                              next[idx] = {
                                                ...next[idx],
                                                environments: current.length ? `${grant.environments}, ${env}` : env,
                                              };
                                              setEditRoleGrants(next);
                                            }
                                          }}
                                        >
                                          + {env}
                                        </button>
                                      ))}
                                    </div>
                                  </div>
                                </div>
                              ))}

                              {editRole !== "admin" && (
                                <button
                                  type="button"
                                  className="admin-add-grant-btn"
                                  onClick={() => {
                                    setEditRoleGrants([
                                      ...editRoleGrants,
                                      {
                                        id: String(Date.now()),
                                        role: "observer",
                                        folders: "",
                                        dashboards: "",
                                        environments: "staging, production",
                                      },
                                    ]);
                                  }}
                                  disabled={saving}
                                  title="Grant observer or other role access for additional folders"
                                >
                                  + Add Role Scope (e.g. Observer)
                                </button>
                              )}
                            </div>
                          ) : (
                            u.scopes?.role_grants && u.scopes.role_grants.length > 1 ? (
                              <div className="admin-grants-summary">
                                {u.scopes.role_grants.map((g, idx) => (
                                  <div key={idx} className="admin-grant-summary-card">
                                    <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "4px" }}>
                                      <span className={`admin-badge role-${g.role}`} style={{ fontSize: "0.68rem", padding: "1px 6px" }}>
                                        {g.role ? g.role.toUpperCase() : "ROLE"}
                                      </span>
                                      <span className="muted x-small">Scope #{idx + 1}</span>
                                    </div>
                                    <div className="admin-scopes-cell">
                                      <div className="admin-scope-item">
                                        <span className="admin-scope-label">Folders:</span>{" "}
                                        <code>{g.allowed_folder_uids?.length ? g.allowed_folder_uids.join(", ") : "—"}</code>
                                      </div>
                                      <div className="admin-scope-item">
                                        <span className="admin-scope-label">Dashboards:</span>{" "}
                                        <code>{g.allowed_dashboard_uids?.length ? g.allowed_dashboard_uids.join(", ") : "—"}</code>
                                      </div>
                                      <div className="admin-scope-item">
                                        <span className="admin-scope-label">Envs:</span>{" "}
                                        <code>{g.allowed_environments?.length ? g.allowed_environments.join(", ") : "staging, production"}</code>
                                      </div>
                                    </div>
                                  </div>
                                ))}
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
                            )
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
                            <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                              <button
                                type="button"
                                className={u.status === "pending" ? "btn primary small" : "btn ghost small"}
                                onClick={() => startEdit(u)}
                              >
                                {u.status === "pending" ? "⚡ Activate" : "Edit"}
                              </button>
                              {u.status === "active" && (
                                <button
                                  type="button"
                                  className={activeActAs === u.user_id ? "btn small" : "btn ghost small"}
                                  style={
                                    activeActAs === u.user_id
                                      ? { background: "var(--accent)", color: "#fff", borderColor: "var(--accent)" }
                                      : {}
                                  }
                                  onClick={() => handleToggleActAs(u)}
                                  title={
                                    activeActAs === u.user_id
                                      ? "Currently testing as this user. Click to revert to Admin."
                                      : `Test and experience application as ${u.display_name || u.username || u.user_id}`
                                  }
                                >
                                  {activeActAs === u.user_id ? "✓ Active Persona" : "🎭 Act As"}
                                </button>
                              )}
                            </div>
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
