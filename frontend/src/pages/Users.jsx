// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect } from 'react';
import { listUsers, createUser, deactivateUser } from '../services/api';
import StatTile, { StatTileRow } from '../components/common/StatTile';
import { blankNewUserForm, isNotEntered } from '../utils/formState';

export default function Users() {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(blankNewUserForm);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  async function loadUsers() {
    try {
      const data = await listUsers();
      setUsers(data);
    } catch (err) {
      console.error('Failed to load users', err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadUsers(); }, []);

  async function handleCreate(e) {
    e.preventDefault();
    setError('');
    // Never submit an unfilled credential field: an empty password must be
    // rejected here as well as server-side, so the request is not even attempted.
    if (isNotEntered(form.username) || isNotEntered(form.password)) {
      setError('Username and password are both required.');
      return;
    }
    setSaving(true);
    try {
      await createUser(form);
      setForm(blankNewUserForm());
      setShowForm(false);
      await loadUsers();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to create user');
    } finally {
      setSaving(false);
    }
  }

  async function handleDeactivate(id, username) {
    if (!window.confirm(`Deactivate user "${username}"?`)) return;
    try {
      await deactivateUser(id);
      await loadUsers();
    } catch (err) {
      alert(err.response?.data?.detail || 'Failed to deactivate user');
    }
  }

  if (loading) return <div className="page"><div className="loading">Loading users...</div></div>;

  const active   = users.filter(u => u.is_active);
  const admins   = users.filter(u => u.role === 'admin');
  const inactive = users.filter(u => !u.is_active);

  return (
    <div className="page">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 24 }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}>User Management</h1>
        <button className="btn btn-primary" onClick={() => setShowForm(!showForm)}>
          {showForm ? 'Cancel' : 'Add User'}
        </button>
      </div>

      <StatTileRow>
        <StatTile label="Total Users" value={users.length}    accent="var(--text-secondary)" />
        <StatTile label="Active"      value={active.length}   accent="#4caf7d" />
        <StatTile label="Inactive"    value={inactive.length} accent="var(--text-muted)"
                  hint={inactive.length ? 'no login allowed' : null} />
        <StatTile label="Admins"      value={admins.length}   accent="#da7756"
                  hint="full access" />
      </StatTileRow>

      {showForm && (
        <form onSubmit={handleCreate} className="card" style={{ marginBottom: 20 }}>
          {error && (
            <div style={{
              background: 'rgba(220, 38, 38, 0.08)', color: 'var(--danger)',
              padding: '10px 14px', borderRadius: 7, fontSize: 13, fontWeight: 500, marginBottom: 16,
            }}>{error}</div>
          )}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
            <div className="input-group" style={{ marginBottom: 0 }}>
              <label>Username</label>
              <input className="input-field" value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                placeholder="username" required />
            </div>
            <div className="input-group" style={{ marginBottom: 0 }}>
              <label>Password</label>
              <input className="input-field" type="password" value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
                placeholder="password" required />
            </div>
            <div className="input-group" style={{ marginBottom: 0 }}>
              <label>Full Name</label>
              <input className="input-field" value={form.full_name}
                onChange={(e) => setForm({ ...form, full_name: e.target.value })}
                placeholder="Full Name" />
            </div>
            <div className="input-group" style={{ marginBottom: 0 }}>
              <label>Role</label>
              <select className="input-field" value={form.role}
                onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="user">User</option>
                <option value="admin">Admin</option>
              </select>
            </div>
          </div>
          <div style={{ marginTop: 16 }}>
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {saving ? 'Creating...' : 'Create User'}
            </button>
          </div>
        </form>
      )}

      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid var(--border)' }}>
              <th style={thStyle}>Username</th>
              <th style={thStyle}>Full Name</th>
              <th style={thStyle}>Role</th>
              <th style={thStyle}>Status</th>
              <th style={thStyle}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} style={{ borderBottom: '1px solid var(--border-subtle)' }}>
                <td style={tdStyle}>{u.username}</td>
                <td style={tdStyle}>{u.full_name || '-'}</td>
                <td style={tdStyle}>
                  <span className={`badge ${u.role === 'admin' ? 'badge-new' : 'badge-ready'}`}>
                    {u.role}
                  </span>
                </td>
                <td style={tdStyle}>
                  <span style={{
                    color: u.is_active ? 'var(--success)' : 'var(--text-muted)',
                    fontWeight: 600, fontSize: 12,
                  }}>
                    {u.is_active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td style={tdStyle}>
                  {u.is_active && (
                    <button
                      className="btn btn-outline btn-sm"
                      onClick={() => handleDeactivate(u.id, u.username)}
                    >
                      Deactivate
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.length === 0 && (
          <div className="empty-state">
            <h3>No users found</h3>
            <p>Create your first user above.</p>
          </div>
        )}
      </div>
    </div>
  );
}

const thStyle = {
  textAlign: 'left',
  padding: '12px 18px',
  fontSize: 12,
  fontWeight: 600,
  color: 'var(--text-muted)',
  textTransform: 'uppercase',
  letterSpacing: '0.3px',
};

const tdStyle = {
  padding: '12px 18px',
  fontSize: 14,
};
