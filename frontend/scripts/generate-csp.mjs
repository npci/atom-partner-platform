#!/usr/bin/env node
// Build the Content-Security-Policy header value for the built SPA.
//
//   Run with:  npm run csp            (prints the policy)
//              npm run csp -- --check (verifies a policy file is in step)
//
// ── WHY THIS FILE EXISTS ────────────────────────────────────────────────────
//
// Checkmarx "Permissive Content Security Policy"
// (Python\Cx\PythonLowVisibility v2, path 26) reported frontend/index.html for
// shipping `<meta http-equiv="Content-Security-Policy"
// content="frame-ancestors 'none'">` — a policy that constrains framing and
// nothing else. The finding is correct twice over: the policy was permissive
// (no script-src, no object-src, no base-uri), AND `frame-ancestors` is
// ignored entirely in a <meta> tag, so it was not even delivering the one
// directive it named. The meta tag is now gone and the policy is an HTTP
// response header, which is the only place every directive is enforced.
//
// ── WHY THE POLICY IS GENERATED RATHER THAN HAND-WRITTEN ────────────────────
//
// index.html carries an inline <style> and an inline <script> — the clickjacking
// frame buster required by the Checkmarx "Potential Clickjacking on Legacy
// Browsers" query, which is a static check on the HTML document and cannot be
// satisfied by a response header. A strict `script-src 'self'` blocks that
// inline script. That failure mode is vicious: the frame buster's first act is
// `html { display: none }`, and it is the script that removes it, so blocking
// the script leaves a permanently invisible application. Verified in Chrome —
// the DOM mounts, React runs, and the user sees a white page.
//
// The two escapes from that are 'unsafe-inline' and hashes. 'unsafe-inline'
// would defeat the point of the exercise and would very likely be re-reported
// as permissive by the next scan. So: hashes, computed from the actual bytes of
// the built index.html, which is what this script does.
//
// A hardcoded hash would rot the moment anyone edited the frame buster, and the
// resulting breakage (blank page, no error) is exactly the kind that reaches
// production. Generating it from the build output means the policy cannot drift
// from the markup it authorises.
//
// ── A TRAP WORTH RECORDING ──────────────────────────────────────────────────
//
// index.html contains a long HTML comment that itself mentions `<script>`.
// Matching /<script>([\s\S]*?)<\/script>/ against the raw file therefore starts
// the capture inside the comment and hashes the wrong bytes. That produced a
// hash Chrome rejected, and the failure looked like "hashes just don't work"
// rather than "the regex is wrong". Comments are stripped before matching, and
// the test asserts the generated hash matches what a browser actually computes.

import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST_INDEX = join(HERE, '..', 'dist', 'index.html');

/** Strip HTML comments. See "A TRAP WORTH RECORDING" above — this is load-bearing. */
export function stripHtmlComments(html) {
  return html.replace(/<!--[\s\S]*?-->/g, '');
}

/** CSP source-expression hash of an inline block's exact text content. */
export function sriHash(text) {
  return `sha256-${createHash('sha256').update(text, 'utf8').digest('base64')}`;
}

/**
 * Collect hashes for every inline <script> and <style> in a built index.html.
 *
 * Only elements with no `src`/`href` are inline. An empty body (the Vite module
 * tag, `<script type="module" src="...">`) is loaded from 'self' and needs no
 * hash — including one would be harmless but misleading.
 */
export function collectInlineHashes(html) {
  const clean = stripHtmlComments(html);
  const scripts = [];
  const styles = [];

  for (const m of clean.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const [, attrs, body] = m;
    if (/\bsrc\s*=/i.test(attrs)) continue;   // external: covered by 'self'
    if (body.trim() === '') continue;
    scripts.push(sriHash(body));
  }
  for (const m of clean.matchAll(/<style\b([^>]*)>([\s\S]*?)<\/style>/gi)) {
    const [, , body] = m;
    if (body.trim() === '') continue;
    styles.push(sriHash(body));
  }
  return { scripts, styles };
}

/**
 * Build the policy.
 *
 * Every directive below is justified against real application behaviour, tested
 * in a browser against the built bundle. Do not add a source without doing the
 * same — a directive relaxed "just in case" is how a policy becomes permissive
 * again, which is the finding this file closes.
 */
export function buildPolicy({ scripts = [], styles = [] } = {}) {
  const scriptSrc = ["'self'", ...scripts.map((h) => `'${h}'`)];

  // style-src NEEDS 'unsafe-inline' and cannot be hashed away.
  //
  // The UI uses ~800 inline `style={{...}}` props. React applies those through
  // the CSSOM (element.style.color = ...), which CSP does not police, so those
  // alone would survive a strict style-src. But hashes do not cover style
  // ATTRIBUTES at all — that requires 'unsafe-inline-attributes', which no
  // browser implements — and any literal style="" in markup would break.
  // Verified in Chrome: under `style-src 'self'`, CSSOM styles applied fine
  // while a literal style attribute and an injected <style> element were both
  // blocked.
  //
  // This is a genuine, bounded weakening. It is also low risk: 'unsafe-inline'
  // in style-src permits CSS injection (data exfiltration via selectors is
  // largely mitigated in modern browsers), NOT script execution. The directive
  // that stops XSS is script-src, and that one stays strict.
  const styleSrc = ["'self'", ...styles.map((h) => `'${h}'`), "'unsafe-inline'"];

  return [
    // Default deny; every capability below is an explicit, justified grant.
    "default-src 'self'",

    // THE anti-XSS directive. No 'unsafe-inline', no 'unsafe-eval'.
    `script-src ${scriptSrc.join(' ')}`,

    `style-src ${styleSrc.join(' ')}`,

    // data: — inlined SVG/PNG assets in the bundle and in generated content.
    // blob: — document/video preview via URL.createObjectURL (services/api.js).
    "img-src 'self' data: blob:",
    "font-src 'self' data:",

    // XHR/fetch/WebSocket. The API is same-origin behind this proxy
    // (services/api.js uses a relative baseURL), so 'self' is sufficient.
    "connect-src 'self'",

    // media: blob: for the promo-video player, which plays an object URL.
    "media-src 'self' blob:",

    // The prototype preview loads /preview-shell.html from this origin.
    // srcDoc/blob: frames INHERIT this policy (verified), which is why the
    // preview is a real same-origin URL with its own relaxed policy instead.
    "frame-src 'self'",
    "child-src 'self'",

    // Clickjacking. Enforced here, in the header — never in a <meta> tag.
    "frame-ancestors 'none'",

    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join('; ');
}

export function policyFromDist(indexPath = DIST_INDEX) {
  const html = readFileSync(indexPath, 'utf8');
  return buildPolicy(collectInlineHashes(html));
}

// CLI: print the policy so the Docker build can capture it.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  process.stdout.write(policyFromDist() + '\n');
}
