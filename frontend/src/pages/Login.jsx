import { ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { login } from '../services/api';
import { useAuth } from '../context/auth';
import bankLogo from '../assets/bank-logo.png';

export default function Login() {
  const navigate = useNavigate();
  const { signIn } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
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
          <img src={bankLogo} alt="Bank" style={{
            // Real logo mark is a horizontal wordmark (≈ 3:1) — keep aspect
            // ratio; only set height so it doesn't squash.
            height: 52, width: 'auto', display: 'block', margin: '0 auto',
          }} />
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
              NPCI Partner Platform
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
          Secured workspace · NPCI UPI Change Management
        </div>
      </div>
    </div>
  );
}
