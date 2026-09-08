// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Centralised timestamp rendering for the partner portal.
//
// Server timestamps from the partner backend are NAIVE UTC: the DB columns are
// timezone-naive DateTime, so `datetime.now(timezone.utc).isoformat()` drops the
// "+00:00" offset. Parsing such a string with `new Date(...)` makes the browser
// read it as LOCAL time — hours off in any zone away from UTC. `parseIso`
// appends a 'Z' so the value is parsed as UTC; the authority portal's columns
// are tz-aware and already carry the offset, so both portals agree.
//
// Display defaults to the viewer's locale and timezone (`undefined` lets Intl
// use the browser's settings). Deployments that want one canonical zone across
// all viewers set VITE_DISPLAY_TIMEZONE / VITE_DISPLAY_LOCALE at build time.

const TZ = import.meta.env.VITE_DISPLAY_TIMEZONE || undefined;
const LOCALE = import.meta.env.VITE_DISPLAY_LOCALE || undefined;

export function parseIso(s) {
  if (!s) return new Date(NaN);
  if (typeof s === 'string' && !/Z|[+-]\d{2}:?\d{2}$/.test(s)) {
    s = s + 'Z';
  }
  return new Date(s);
}

// Date + time, e.g. "23 Jun 2026, 03:30 pm".
export function formatDateTime(s, opts = {}) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  return d.toLocaleString(LOCALE, {
    day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZone: TZ, ...opts,
  });
}

// Date only, e.g. "23 Jun 2026".
export function formatDate(s, opts = {}) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  return d.toLocaleDateString(LOCALE, {
    day: 'numeric', month: 'short', year: 'numeric', timeZone: TZ, ...opts,
  });
}

// Compact day + time without the year, e.g. "23 Jun, 03:30 pm". Used where
// horizontal space is tight (timeline stamps, table cells).
export function formatDayTime(s) {
  const d = parseIso(s);
  if (isNaN(d)) return '';
  return d.toLocaleString(LOCALE, {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: TZ,
  });
}

// "just now" / "5m ago" / "2h ago" / "3d ago", then an absolute date.
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
