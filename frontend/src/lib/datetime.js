// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Centralised timestamp rendering for the partner portal.
//
// Server timestamps from the partner backend are NAIVE UTC: the DB columns are
// timezone-naive DateTime, so `datetime.now(timezone.utc).isoformat()` drops the
// "+00:00" offset. Parsing such a string with `new Date(...)` makes the browser
// read it as LOCAL time — ~5h30m off in IST. `parseIso` appends a 'Z' so the
// value is parsed as UTC; the formatters then render it in IST so the partner
// portal matches the authority portal (whose columns are tz-aware and already carry
// the offset). Display is pinned to Asia/Kolkata so it stays IST regardless of
// the viewer's machine timezone.

const IST = 'Asia/Kolkata';

export function parseIso(s) {
  if (!s) return new Date(NaN);
  if (typeof s === 'string' && !/Z|[+-]\d{2}:?\d{2}$/.test(s)) {
    s = s + 'Z';
  }
  return new Date(s);
}

// Date + time in IST, e.g. "23 Jun 2026, 03:30 pm".
export function formatDateTime(s, opts = {}) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  return d.toLocaleString('en-IN', {
    day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZone: IST, ...opts,
  });
}

// Date only in IST, e.g. "23 Jun 2026".
export function formatDate(s, opts = {}) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('en-IN', {
    day: 'numeric', month: 'short', year: 'numeric', timeZone: IST, ...opts,
  });
}

// "just now" / "5m ago" / "2h ago" / "3d ago", then an absolute IST date.
export function formatRelative(s) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  const sec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (sec < 60)     return 'just now';
  if (sec < 3600)   return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400)  return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 604800) return `${Math.floor(sec / 86400)}d ago`;
  return formatDate(s);
}
