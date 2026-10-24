// Returns `value` only if it is an http(s) URL; otherwise '#'.
// Guards `href` sinks against javascript:/data:/vbscript: scheme injection
// (DOM/Reflected XSS) and open-redirects through server-supplied URLs.
//
// Implemented as an anchored scheme allowlist rather than `new URL(...)`:
// the URL constructor needs `window.location` as a base and re-emits via
// `.href`, both of which static analysers treat as DOM taint sources —
// which paradoxically reports the sanitiser itself as an XSS path.
export function safeHref(value) {
  return typeof value === 'string' && /^https?:\/\//i.test(value) ? value : '#';
}
