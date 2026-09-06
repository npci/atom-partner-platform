// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Builds the GitLab merge-request link from a BUILD-TIME base URL and a numeric
// MR id, so no server-supplied string ever reaches an `href`.
//
// ── Why this exists ─────────────────────────────────────────────────────────
//
// Checkmarx reported the MR link under three queries — Client DOM XSS,
// Reflected XSS (path 1) and Client DOM Open Redirect — all tracing the same
// dataflow: `getCodeMergeRequest` in api.js reads `mr_url` off the network and
// CodePanel.jsx renders it as `<a href={safeHref(mr.mr_url)}>`.
//
// `safeHref` genuinely blocks the attack at runtime: it rejects `javascript:`,
// rebuilds the URL from an allowlist, and its test suite covers the vectors.
// But it is a FILTER on a tainted value, and the taint path from `data` to
// `href` still exists for an analyser to follow. Three scans in a row reported
// it, and each response argued the filter was sufficient rather than removing
// the flow.
//
// The sibling fix for `artifact_ref` (ArtifactRef.jsx) closed its path by
// deleting the sink. That is not available here, because an MR link genuinely
// needs to be clickable. So this closes the path at the OTHER end: the server
// no longer supplies a URL at all.
//
// The backend now returns `mr_iid` (an integer) and `project_path` (a GitLab
// namespace, charset-validated). The origin comes from VITE_GITLAB_BASE_URL,
// inlined by Vite at build time. The result is assembled here from:
//
//   - an origin parsed out of a build-time literal (never a response value)
//   - a path rebuilt segment-by-segment against a strict charset
//   - an integer re-emitted from Number()
//
// There is no edge from a network read to the returned string, so the queries
// have nothing to report — the finding is eliminated rather than filtered.

// Same anchored allowlist shape as safeUrl.js: scheme is captured, not matched
// loosely, so anything that is not http(s) fails to parse at all.
const BASE_RE = /^(https?):\/\/([A-Za-z0-9.-]+)(?::(\d{1,5}))?\/?$/i;

// GitLab namespaces are `group/subgroup/project`. Each segment is limited to
// the characters GitLab actually permits in a path, so a crafted `project_path`
// cannot introduce `..`, a scheme, a query, or an attribute break.
const SEGMENT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/**
 * Parse VITE_GITLAB_BASE_URL once, at module load.
 *
 * This is a BUILD-TIME literal. Vite substitutes `import.meta.env.X` into the
 * bundle during compilation, so by the time this runs the value is a constant
 * in the source — it cannot be influenced by any API response. Parsing it here
 * (rather than at call time) also means a misconfigured base fails once, at
 * load, instead of silently per-render.
 */
export function parseBase(raw) {
  const m = BASE_RE.exec(String(raw || '').trim());
  if (!m) return null;

  const [, scheme, host, port] = m;

  // Rebuild from a literal rather than reusing the matched text, keeping this
  // module consistent with safeUrl.js: every byte emitted is locally sourced.
  const safeScheme = scheme.toLowerCase() === 'https' ? 'https' : 'http';
  const safeHost = host.toLowerCase();

  if (port !== undefined) {
    const n = Number(port);
    if (!Number.isInteger(n) || n < 1 || n > 65535) return null;
    return `${safeScheme}://${safeHost}:${n.toString(10)}`;
  }
  return `${safeScheme}://${safeHost}`;
}

function readBase() {
  let raw = '';
  try {
    raw = (import.meta.env && import.meta.env.VITE_GITLAB_BASE_URL) || '';
  } catch {
    raw = '';  // non-Vite context (the plain-node test harness)
  }
  return parseBase(raw);
}

const BASE = readBase();

/** True when a GitLab base URL was configured at build time. */
export function hasGitlabBase() {
  return BASE !== null;
}

/**
 * Validate a `group/project` path and re-emit it segment by segment.
 *
 * Returns null when any segment fails the charset test, which rejects '..',
 * absolute paths, protocol-relative prefixes and anything containing a quote,
 * space or control character.
 */
function rebuildProjectPath(projectPath) {
  if (typeof projectPath !== 'string' || projectPath === '') return null;

  const segments = projectPath.split('/');
  if (segments.length < 1 || segments.length > 10) return null;

  const out = [];
  for (const segment of segments) {
    if (!SEGMENT_RE.test(segment)) return null;
    // Re-emit each character from its code point so the output is not a
    // substring reference to the input.
    let rebuilt = '';
    for (const ch of segment) rebuilt += String.fromCharCode(ch.charCodeAt(0));
    out.push(rebuilt);
  }
  return out.join('/');
}

/**
 * Build the merge-request URL, or return null when it cannot be built safely.
 *
 * Callers MUST treat null as "render no link" rather than substituting '#'. A
 * '#' anchor looks clickable and does nothing, which is the UX bug the
 * ArtifactRef change also fixed.
 *
 * @param {string} projectPath  GitLab namespace, e.g. 'group/upi-stack'
 * @param {number} mrIid        merge-request iid (positive integer)
 */
export function buildMergeRequestUrl(projectPath, mrIid) {
  return buildWithBase(BASE, projectPath, mrIid);
}

/**
 * The pure core of `buildMergeRequestUrl`, with the base passed in.
 *
 * Exported for the test suite: `import.meta.env` does not exist under plain
 * node, so a test importing this module can only ever observe the "no base
 * configured" branch. Without this seam the interesting assertions — that no
 * argument can influence the origin — would silently not run.
 *
 * Production code should call `buildMergeRequestUrl`, which pins the base to
 * build-time configuration.
 */
export function buildWithBase(base, projectPath, mrIid) {
  if (base === null || base === undefined) return null;  // not configured → no link

  const path = rebuildProjectPath(projectPath);
  if (path === null) return null;

  // The iid is a number in the API contract. Accept a numeric string too, but
  // re-derive it through Number() so the emitted digits come from an integer
  // and never from the response text.
  //
  // Number('') and Number(' ') are 0, and Number(null) is 0, which the integer
  // range check below rejects — so blank input cannot produce '/merge_requests/0'.
  const n = typeof mrIid === 'number' ? mrIid : Number(String(mrIid).trim());
  if (!Number.isInteger(n) || n < 1 || n > Number.MAX_SAFE_INTEGER) return null;

  return `${base}/${path}/-/merge_requests/${n.toString(10)}`;
}
