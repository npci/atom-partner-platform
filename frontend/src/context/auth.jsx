// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Auth state for the SPA. The session JWT now lives in an httpOnly cookie
// (set by the backend on login), so it is NOT readable from JavaScript and
// nothing auth-related is kept in localStorage. The current user is held in
// React state, rehydrated on load via GET /auth/me (which authenticates off
// the cookie).
import { createContext, useContext, useEffect, useState } from 'react';
import { getMe } from '../services/api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  // status: 'loading' | 'authed' | 'anon'
  const [status, setStatus] = useState('loading');
  const [user, setUser] = useState(null);

  useEffect(() => {
    let alive = true;
    getMe()
      .then((u) => { if (alive) { setUser(u); setStatus('authed'); } })
      .catch(() => { if (alive) { setUser(null); setStatus('anon'); } });
    return () => { alive = false; };
  }, []);

  // Called by the Login page after a successful POST /auth/login.
  function signIn(u) {
    setUser(u);
    setStatus('authed');
  }

  // Local clear after POST /auth/logout (cookie is cleared server-side).
  function clear() {
    setUser(null);
    setStatus('anon');
  }

  return (
    <AuthContext.Provider value={{ status, user, signIn, clear }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>');
  return ctx;
}
