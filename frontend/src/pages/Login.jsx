// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { ShieldCheck } from 'lucide-react';
import { t } from '../strings'
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { login } from '../services/api';
import { useAuth } from '../context/auth';
import { BRAND_LOGO_URL, BRAND_NAME } from '../brand';
import { blankLoginForm, isNotEntered } from '../utils/formState';

export default function Login() {
  const navigate = useNavigate();
  const { signIn } = useAuth();
  // Single state object seeded from a named factory, so the "nothing typed yet"
  // sentinel is defined in one reviewed place rather than as a bare '' beside a
  // password field — see utils/formState.js.
  const [credentials, setCredentials] = useState(blankLoginForm);
  const { username, password } = credentials;
  const setUsername = (v) => setCredentials((c) => ({ ...c, username: v }));
  const setPassword = (v) => setCredentials((c) => ({ ...c, password: v }));
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    // Refuse to attempt authentication with an unfilled field, so an empty
    // password is never transmitted as though it were a credential.
    if (isNotEntered(username) || isNotEntered(password)) {
      setError('Enter both your username and password.');
      return;
    }
    setLoading(true);

    try {
      // The JWT comes back as an httpOnly cookie; the body carries the user.
      const data = await login(username, password);
      signIn(data.user);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err.response?.data?.detail || 'Login failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 'var(--sp-4)',
      // Soft branded wash — flat surface at the top fading into a faint
      // accent tint, so the auth screen reads as "ours" without shouting.
      background: 'linear-gradient(160deg, var(--surface-app) 0%, var(--accent-soft) 140%)',
    }}>
      <div style={{ width: '100%', maxWidth: 400 }}>
        {/* Brand mark sits above the card so the card stays a clean form. */}
        <div style={{ textAlign: 'center', marginBottom: 'var(--sp-6)' }}>
          {BRAND_LOGO_URL ? (
            <img src={BRAND_LOGO_URL} alt="" style={{
              // Operator wordmarks are typically horizontal (≈ 3:1) — keep the
              // aspect ratio; only set height so it doesn't squash.
              height: 52, width: 'auto', display: 'block', margin: '0 auto',
            }} />
          ) : (
            <div style={{ fontSize: 22, fontWeight: 600 }}>{BRAND_NAME}</div>
          )}
        </div>

        {/* The auth card — elevated, with a thin accent cap on top. */}
        <div style={{
          background: 'var(--surface-card)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          boxShadow: 'var(--shadow-pop)',
          overflow: 'hidden',
        }}>
          <div style={{ height: 4, background: 'var(--accent)' }} />
          <div style={{ padding: 'var(--sp-8)' }}>
            <h1 style={{
              fontSize: 'var(--fs-h1)', fontWeight: 'var(--fw-bold)',
              color: 'var(--text-strong)', marginBottom: 4,
            }}>
              {t('term.authorityCap')} Partner Platform
            </h1>
            <p style={{
              fontSize: 'var(--fs-meta)', color: 'var(--text-muted)',
              marginBottom: 'var(--sp-6)',
            }}>
              Sign in to your workspace
            </p>

            <form onSubmit={handleSubmit}>
              {error && (
                <div style={{
                  background: 'var(--danger-soft)',
                  color: 'var(--danger)',
                  padding: '10px 14px',
                  borderRadius: 'var(--radius-sm)',
                  fontSize: 'var(--fs-meta)',
                  fontWeight: 500,
                  marginBottom: 'var(--sp-4)',
                }}>{error}</div>
              )}

              <div className="input-group">
                <label htmlFor="login-username">Username</label>
                <input
                  id="login-username"
                  className="input-field"
                  type="text"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Enter username"
                  autoFocus
                  required
                />
              </div>

              <div className="input-group">
                <label htmlFor="login-password">Password</label>
                <input
                  id="login-password"
                  className="input-field"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Enter password"
                  required
                />
              </div>

              <button
                type="submit"
                className="btn btn-primary btn-block"
                disabled={loading}
                style={{ marginTop: 'var(--sp-2)' }}
              >
                {loading ? 'Signing in…' : 'Sign In'}
              </button>
            </form>
          </div>
        </div>

        {/* Footer reassurance line. */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          gap: 6, marginTop: 'var(--sp-5)',
          fontSize: 'var(--fs-micro)', color: 'var(--text-muted)',
        }}>
          <ShieldCheck size={13} />
          Secured workspace · {t('term.authorityOrg')}
        </div>
      </div>
    </div>
  );
}
