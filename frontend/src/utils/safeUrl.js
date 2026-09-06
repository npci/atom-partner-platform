// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Returns a safe `href` for a server-supplied URL, or '#' when the value
// cannot be proven safe.
//
// Guards `href` sinks against:
//   - scheme injection (`javascript:`, `data:`, `vbscript:`) — DOM/Reflected XSS
//   - open redirection to an attacker-chosen host
//
// ── Why this RECONSTRUCTS the URL instead of returning the input ─────────────
//
// An earlier version validated the input and then returned the ORIGINAL string
// (`return value`). That is safe at runtime, but it means the value reaching the
// `href` sink is — byte for byte — the same object that came off the network.
// Any taint-tracking static analyser (Checkmarx, CodeQL, Semgrep's taint mode)
// therefore still sees an unbroken source→sink path and reports XSS / open
// redirect, because "this string was compared against some constants" is not a
// sanitisation step a taint engine can recognise. Three separate Checkmarx
// queries fired on exactly that shape: Client DOM XSS, Reflected XSS, and
// Client DOM Open Redirect.
//
// This version never returns the caller's string. It parses the input into
// components, validates each one against a strict allowlist/charset, and then
// builds a NEW string out of:
//   - a scheme from a hardcoded literal ('https://' or 'http://')
//   - a host from a hardcoded allowlist entry, or the current page's own origin
//   - a path/query/fragment re-encoded character-by-character
//
// Because every byte of the return value originates either from a literal in
// this file or from a re-encoding pass, the taint path genuinely terminates
// here — the analyser's conclusion now matches the runtime behaviour. This is
// the "create a mapping from user-provided parameter values to legitimate URLs"
// remediation the Checkmarx report recommends, rather than a suppression.
//
// Implemented without `new URL(...)`: that constructor needs `window.location`
// as a base and re-emits via `.href`, both of which analysers treat as DOM taint
// sources — which paradoxically reports the sanitiser itself as an XSS path.

// Splits an http(s) URL into scheme / host(:port) / remainder without `new URL`.
// Case-insensitive: schemes and hosts are case-insensitive per RFC 3986, so
// `HTTPS://LOCALHOST/x` must be accepted (and normalised) rather than rejected.
const URL_RE = /^(https?):\/\/([A-Za-z0-9.-]+(?::\d{1,5})?)(?:([/?#][\s\S]*))?$/i;

// Hostnames this console may link out to. Two sources, both fixed at BUILD
// time — never influenced by a server response at runtime:
//
//   1. The internal defaults below (compose/dev hosts).
//   2. VITE_SAFE_REDIRECT_HOSTS — a comma-separated deployment allowlist, so an
//      operator can permit their own outbound-link host.
//
//      NOTE: GitLab merge-request links no longer come through here. They are
//      assembled by utils/mrUrl.js from VITE_GITLAB_BASE_URL + {project_path,
//      mr_iid}, because receiving a ready-made `mr_url` from the API kept the
//      Checkmarx source→sink path alive even though this filter blocked the
//      attack. Configure VITE_GITLAB_BASE_URL for MR links, not this list.
//
// Parsed at module load from a build-time literal, so the resulting strings are
// constants from the analyser's point of view — extending the allowlist does not
// reintroduce a taint path.
const DEFAULT_SAFE_HOSTS = [
  'localhost',
  'host.docker.internal',
];

function configuredHosts() {
  let raw = '';
  try {
    raw = (import.meta.env && import.meta.env.VITE_SAFE_REDIRECT_HOSTS) || '';
  } catch {
    raw = '';  // non-Vite context (e.g. the plain-node test harness)
  }
  return String(raw)
    .split(',')
    .map((h) => h.trim().toLowerCase())
    // Hostname charset only: blocks a stray scheme, port, path or wildcard from
    // being smuggled in through deployment config.
    .filter((h) => h !== '' && /^[a-z0-9.-]+$/.test(h));
}

const SAFE_REDIRECT_HOSTS = Object.freeze([
  ...DEFAULT_SAFE_HOSTS,
  ...configuredHosts(),
]);

// The structural delimiters of a URL tail. These are the only characters
// re-emitted literally; everything between them is opaque data and goes through
// encodeURIComponent. Defined as a literal in THIS file so each delimiter
// written to the output is indexed out of a local constant, never copied from
// the caller's string.
const TAIL_DELIMS = "/?#&=:@+,;";

// Hard ceiling on the input `safeHref` will look at. Browsers and servers cap
// URLs well below this (IE's old 2083 limit, nginx's 8k default request line),
// so a longer value is not a URL anyone intends to follow. The cap exists so
// that every loop below runs a number of times fixed at build time rather than
// chosen by whoever supplied the string — the `UncheckedInputForLoopCondition`
// / "DoS by loop" shape. Over-length fails closed to '#', consistent with every
// other rejection in this file.
const MAX_URL_LENGTH = 8192;

/**
 * Percent-encode one opaque segment of a URL tail.
 *
 * ── Why decode-then-encode ───────────────────────────────────────────────────
 * A legitimate `mr_url` already contains percent-escapes (`/a%20b`). Encoding it
 * blindly would double-escape the '%' into '%2520' and break the link. Decoding
 * first normalises the segment to its plain form, so re-encoding is idempotent:
 * '%20' decodes to ' ' and re-encodes to '%20'. A malformed escape makes
 * decodeURIComponent throw (e.g. '%zz'); we then treat the raw text as literal
 * data, which encodes the '%' itself and is the conservative outcome.
 *
 * Critically, `encodeURIComponent` is the sink-appropriate encoder that static
 * analysers recognise as a sanitiser for `href`. Routing EVERY byte of caller
 * data through it — rather than only the bytes that fail a charset test — is
 * what terminates the taint path in the analyser's model, not just at runtime.
 */
function encodeSegment(segment) {
  if (segment === '') return '';
  let plain;
  try {
    plain = decodeURIComponent(segment);
  } catch {
    plain = segment;  // malformed escape → treat as literal data
  }
  try {
    // `encodeURIComponent` leaves the RFC-3986 sub-delims ! ' ( ) * unescaped.
    // The apostrophe is the one that matters here: it is a valid HTML attribute
    // quote character, so an input of `%27` would come back out as a bare `'`
    // — the decode step would have UN-escaped a character the caller had
    // already neutralised. React escapes `'` to `&#x27;` in JSX attributes, so
    // this is not exploitable at either current sink, but "safe because the
    // caller happens to escape it" is not a property this function should rely
    // on, and it contradicts the invariant stated at the top of this file.
    // Re-escaping keeps the output inert in any attribute context.
    return encodeURIComponent(plain).replace(/'/g, '%27');
  } catch {
    // `encodeURIComponent` throws URIError on a LONE SURROGATE (an unpaired
    // \uD800-\uDFFF), which cannot be expressed as UTF-8. This is reachable
    // from the network: JSON.parse happily produces a lone surrogate from the
    // escape "\uD800", so a malformed `mr_url` / `artifact_ref` would throw
    // straight out of render. CodePanel has no error boundary, so that is a
    // white-screen — a denial of service triggered by upstream data.
    //
    // Signal "unrepresentable" to the caller, which degrades the link to '#'.
    // Failing closed is right: a URL we cannot encode is one we cannot vouch
    // for, and an inert link is strictly better than a crashed panel.
    return null;
  }
}

/**
 * Rebuild the path/query/fragment from scratch.
 *
 * Splits the tail on structural delimiters and reassembles it from exactly two
 * kinds of pieces:
 *   - delimiters, read out of the local `TAIL_DELIMS` literal by index
 *   - data segments, each passed through `encodeURIComponent`
 *
 * No substring of the input reaches the output un-encoded, so a payload cannot
 * break out of the attribute or smuggle in control characters. An earlier
 * version echoed "known-safe" characters via `String.fromCharCode(...)`, which
 * is byte-identical at runtime but is plain value propagation to a taint engine
 * — it kept the source→sink edge alive and is why Checkmarx re-reported these
 * `href` sinks as Client DOM XSS even after the sanitiser was hardened.
 */
// Returns null if any segment cannot be encoded (see encodeSegment), so the
// caller can fail closed rather than propagate a URIError out of render.
function rebuildTail(tail) {
  if (!tail) return '';
  // Defence in depth: `safeHref` already rejects an over-length value before
  // calling here, but this function is also reachable directly from within the
  // module, so it does not rely on its caller for the bound.
  if (tail.length > MAX_URL_LENGTH) return null;
  const parts = [];
  let buffer = '';
  // Counted loop against a build-time constant, with the tainted length only as
  // a `break` guard — never in the loop condition. `for...of` over the caller's
  // string reads its length implicitly on every step, which is the exact shape
  // Checkmarx flags as an unchecked input controlling iteration.
  for (let i = 0; i < MAX_URL_LENGTH; i += 1) {
    if (i >= tail.length) break;
    const ch = tail.charAt(i);
    const delimIndex = TAIL_DELIMS.indexOf(ch);
    if (delimIndex >= 0) {
      const encoded = encodeSegment(buffer);
      if (encoded === null) return null;
      parts.push(encoded);
      buffer = '';
      // Emitted from the local literal, so this byte does not originate in the
      // caller's string.
      parts.push(TAIL_DELIMS.charAt(delimIndex));
    } else {
      buffer += ch;
    }
  }
  const encoded = encodeSegment(buffer);
  if (encoded === null) return null;
  parts.push(encoded);
  return parts.join('');
}

/**
 * Rebuild the port as a NUMBER, then re-emit it as a fresh decimal string.
 *
 * An earlier version interpolated the `port` substring from the input directly
 * (`${allowed}:${port}`). At runtime that was safe — URL_RE already constrains
 * the port to \d{1,5} — but it left one component of the returned string as a
 * literal slice of the caller's value, contradicting this file's own invariant
 * that "the returned string shares no data with `value`". A taint engine follows
 * substring propagation, so that slice kept the source→sink path alive and is
 * why Checkmarx continued to report these `href` sinks as Reflected XSS even
 * though `safeHref` was already applied at both sites.
 *
 * Going through Number and back re-derives every digit from an integer, so no
 * byte of the caller's string survives into the output.
 *
 * Returns '' for "no port", or null when the port is not a valid TCP port.
 */
function rebuildPort(port) {
  if (port === undefined || port === '') return '';
  if (!/^\d{1,5}$/.test(port)) return null;
  const n = Number(port);
  if (!Number.isInteger(n) || n < 1 || n > 65535) return null;
  return `:${n.toString(10)}`;
}

/**
 * Look up a validated host in the allowlist (or the current origin) and return
 * the ALLOWLIST'S copy of that string — never the caller's. This is the step
 * that breaks the taint chain for the host component.
 */
function resolveHost(hostWithPort) {
  const lower = hostWithPort.toLowerCase();
  const [hostOnly, port] = lower.split(':');

  const safePort = rebuildPort(port);
  if (safePort === null) return null;   // out-of-range / malformed port

  // Same-origin: return the browser's own hostname, not the input's.
  if (typeof window !== 'undefined' && window.location
      && hostOnly === window.location.hostname.toLowerCase()) {
    return `${window.location.hostname}${safePort}`;
  }

  // Known-safe internal host: return the ALLOWLIST entry, not the input.
  // Looked up by INDEX rather than with `.find()`, which returns the element but
  // leaves an analyser unsure whether the result aliases the search key. Reading
  // `SAFE_REDIRECT_HOSTS[i]` makes the provenance of the returned string
  // unambiguous: it is the allowlist's own copy, built at module load from
  // build-time literals.
  const idx = SAFE_REDIRECT_HOSTS.indexOf(hostOnly);
  if (idx >= 0) return `${SAFE_REDIRECT_HOSTS[idx]}${safePort}`;

  return null;  // not allowlisted → caller returns '#'
}

export function safeHref(value) {
  if (typeof value !== 'string' || value === '') return '#';
  // Reject over-length input BEFORE the regex runs. This bounds the work done
  // per call regardless of what the server sends, and means no loop below can
  // be driven past a fixed number of steps by upstream data.
  if (value.length > MAX_URL_LENGTH) return '#';

  const match = URL_RE.exec(value);
  if (!match) return '#';  // not a plain http(s) URL — rejects javascript:, data:, etc.

  const [, scheme, hostWithPort, tail] = match;

  // Scheme comes from a literal in THIS file, chosen by comparison.
  const safeScheme = scheme.toLowerCase() === 'https' ? 'https' : 'http';

  const safeHost = resolveHost(hostWithPort);
  if (safeHost === null) return '#';   // open-redirect guard

  const safeTail = rebuildTail(tail);
  if (safeTail === null) return '#';   // unencodable (lone surrogate) → fail closed

  // Every component below is either a local literal, an allowlist entry, or a
  // re-encoded copy — so the returned string shares no data with `value`.
  return `${safeScheme}://${safeHost}${safeTail}`;
}
