// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Routes, Route, NavLink, Navigate, useNavigate, useLocation } from 'react-router-dom';
import { ROUTER_BASENAME } from './utils/basePath';
import { AuthProvider, useAuth } from './context/auth';
import { logout as apiLogout } from './services/api';
import {
  BookOpen, KeyRound, LayoutDashboard, LogOut,
  Settings as SettingsIcon, Users as UsersIcon,
} from 'lucide-react';
import Dashboard from './pages/Dashboard';
import ChangeDetail from './pages/ChangeDetail';
import Settings from './pages/Settings';
import KnowledgeBase from './pages/KnowledgeBase';
import Login from './pages/Login';
import Users from './pages/Users';
import ChangePasswordModal from './components/common/ChangePasswordModal';
import { BRAND_LOGO_URL, BRAND_NAME } from './brand';

// Sidebar navigation. `admin: true` items only render for admin users —
// non-admins also can't reach them via URL (see AdminRoute).
const NAV_ITEMS = [
  { to: '/',          end: true, label: 'Dashboard',      icon: LayoutDashboard },
  { to: '/settings',             label: 'Settings',       icon: SettingsIcon, admin: true },
  { to: '/users',                label: 'Users',          icon: UsersIcon,    admin: true },
  { to: '/knowledge',            label: 'Knowledge Base', icon: BookOpen,     admin: true },
];

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      retry: false,
      staleTime: 30_000,
    },
  },
});

function ProtectedRoute({ children }) {
  const { status } = useAuth();
  if (status === 'loading') return null;   // brief: /auth/me probe in flight
  if (status === 'anon') return <Navigate to="/login" replace />;
  return children;
}

// Admin-only route guard. Requires an authenticated session and then checks
// the user role. Non-admins typing /settings or /users directly in the URL
// get bounced to Dashboard. Backend endpoints behind these pages additionally
// enforce admin via FastAPI's `require_admin` dependency — this is the UX layer.
function AdminRoute({ children }) {
  const { status, user } = useAuth();
  if (status === 'loading') return null;
  if (status === 'anon') return <Navigate to="/login" replace />;
  if (user?.role !== 'admin') return <Navigate to="/" replace />;
  return children;
}

function Layout({ children }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, clear } = useAuth();
  const [showChangePassword, setShowChangePassword] = useState(false);

  async function handleLogout() {
    await apiLogout();
    clear();
    navigate('/login', { replace: true });
  }

  // Don't show nav on login page
  if (location.pathname === '/login') {
    return <>{children}</>;
  }

  const displayName = user?.full_name || user?.username || 'User';
  const initial = displayName.trim().charAt(0) || '?';
  const navItems = NAV_ITEMS.filter((n) => !n.admin || user?.role === 'admin');

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Main navigation">
        <div className="sidebar-brand">
          {BRAND_LOGO_URL && <img src={BRAND_LOGO_URL} alt="" />}
          <span>{BRAND_NAME}</span>
        </div>

        <nav className="sidebar-nav">
          {navItems.map(({ to, end, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) => `sidebar-link ${isActive ? 'active' : ''}`}
            >
              <Icon size={17} /> {label}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <div className="sidebar-user">
            <div className="sidebar-avatar">{initial}</div>
            <div style={{ minWidth: 0 }}>
              <div className="sidebar-user-name">{displayName}</div>
              <div className="sidebar-user-role">{user?.role || 'user'}</div>
            </div>
          </div>
          <button
            onClick={() => setShowChangePassword(true)}
            className="btn btn-ghost btn-sm"
            title="Change your password"
          >
            <KeyRound size={14} /> Change password
          </button>
          <button onClick={handleLogout} className="btn btn-ghost btn-sm">
            <LogOut size={14} /> Logout
          </button>
        </div>
      </aside>

      <main className="app-main">
        {children}
      </main>

      <ChangePasswordModal
        open={showChangePassword}
        onClose={() => setShowChangePassword(false)}
      />
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={ROUTER_BASENAME}>
        <AuthProvider>
          <Layout>
            <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/" element={<ProtectedRoute><Dashboard /></ProtectedRoute>} />
            <Route path="/changes/:id" element={<ProtectedRoute><ChangeDetail /></ProtectedRoute>} />
            <Route path="/settings" element={<AdminRoute><Settings /></AdminRoute>} />
            <Route path="/users" element={<AdminRoute><Users /></AdminRoute>} />
            <Route path="/knowledge" element={<AdminRoute><KnowledgeBase /></AdminRoute>} />
            </Routes>
          </Layout>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
