// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Runtime context-path inference.
// <base href="/${CONTEXT_PATH}/"> is injected by nginx sub_filter at serve time.
// In dev (Vite dev server, no sub_filter) falls back to '/a2a-partner/'.

function _getBasePath() {
  const base = document.querySelector('base')?.getAttribute('href');
  if (!base) return '/a2a-partner/';
  return base.endsWith('/') ? base : base + '/';
}

export const BASE_PATH = _getBasePath();
export const API_BASE = BASE_PATH + 'api';

export function wsUrl(path) {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}${BASE_PATH}${path}`;
}

export const ROUTER_BASENAME = BASE_PATH.replace(/\/$/, '') || '/';
export const LOGIN_PATH = ROUTER_BASENAME + '/login';

/**
 * Resolve a static asset shipped in `public/` to a URL under the deployed
 * context path (e.g. 'preview-shell.html' -> '/a2a-partner/preview-shell.html').
 *
 * Used for the prototype-preview shell, whose iframe `src` must resolve
 * correctly whether the app is served at the root or under /a2a-partner/.
 *
 * The argument is expected to be a build-time literal naming a file we ship.
 * It is deliberately NOT a general-purpose URL builder: the leading-slash strip
 * keeps a caller from accidentally producing a protocol-relative '//host' URL,
 * and any value containing a scheme or authority is rejected outright so this
 * can never become an open-redirect or remote-script sink. Anything dynamic or
 * server-supplied belongs in safeHref(), not here.
 */
export function withBasePath(assetPath) {
  const raw = String(assetPath ?? '');
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith('//')) {
    throw new Error('withBasePath expects a relative asset path, not a URL');
  }
  return BASE_PATH + raw.replace(/^\/+/, '');
}
