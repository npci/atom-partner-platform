// Guard: the merge-request link must be BUILT locally, never taken from the API.
//
// Zero-dependency by design, matching the sibling guards — no test runner, no
// JSX transform. These are source assertions, not render tests: they read the
// code as text and check its shape, which is the level a taint scanner reasons
// at. A behavioural test cannot catch this regression, because restoring the
// old `<a href={safeHref(mr.mr_url)}>` would still render a working link.
//
//   Run with:  npm run test:mergeRequestLink
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// Checkmarx reported CodePanel.jsx:352 under THREE queries — Client DOM XSS,
// Reflected XSS (path 1) and Client DOM Open Redirect — all following one
// dataflow: `getCodeMergeRequest` in api.js reads `mr_url` off the network and
// the component rendered it in an <a href>.
//
// Three consecutive scans reported it, and each response hardened `safeHref`
// rather than removing the flow. The sanitiser genuinely blocked the attack —
// its own suite proves `javascript:` payloads return '#' — but a filter on a
// tainted value leaves the source→sink edge in place, so the finding kept
// coming back on identical code.
//
// The fix follows the ArtifactRef precedent and removes the flow at the source:
// the backend sends `project_path` + `mr_iid`, and utils/mrUrl.js assembles the
// URL from a build-time base. The assertions below pin the parts of that which
// are easy to undo by accident.

import { readFile } from 'node:fs/promises';

const files = {
  codePanel: new URL('../src/components/CodePanel.jsx', import.meta.url),
  mrUrl:     new URL('../src/utils/mrUrl.js', import.meta.url),
  api:       new URL('../src/services/api.js', import.meta.url),
  backendMr: new URL('../../backend/app/api/dashboard/code.py', import.meta.url),
};

let pass = 0, fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass += 1; console.log(`PASS  ${name}`); }
  else    { fail += 1; console.log(`FAIL  ${name}${detail ? `\n      ${detail}` : ''}`); }
};

// Strip comments so prose mentioning `mr_url` or `href` does not trip the scan.
// Newlines are preserved so any future line-number reporting stays accurate.
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ''))
  .replace(/^[ \t]*\/\/.*$/gm, '');
const stripPyComments = (src) => src
  .replace(/"""[\s\S]*?"""/g, (m) => m.replace(/[^\n]/g, ''))
  .replace(/^[ \t]*#.*$/gm, '');

const codePanel = stripComments(await readFile(files.codePanel, 'utf8'));
const mrUrl     = stripComments(await readFile(files.mrUrl, 'utf8'));
const api       = stripComments(await readFile(files.api, 'utf8'));

// ── 1. The tainted field must not appear in the frontend at all ─────────────
// This is the single most important assertion: if `mr_url` is not read from the
// response anywhere, the dataflow the three queries follow cannot exist.
check('CodePanel does not reference mr_url',
  !/\bmr_url\b/.test(codePanel),
  'Reading mr_url off the API response recreates the exact source→sink path '
  + 'Checkmarx reported three times. Use project_path + mr_iid instead.');

check('no frontend source reads mr_url',
  !/\bmr_url\b/.test(api),
  'api.js must not surface mr_url; the browser builds the link itself.');

// ── 2. The href must come from the builder, not from response data ──────────
const hrefs = [...codePanel.matchAll(/href\s*=\s*\{([^}]*)\}/g)].map((m) => m[1].trim());
check('every href in CodePanel is a locally built URL',
  hrefs.every((h) => /^url$/.test(h) || /buildMergeRequestUrl\s*\(/.test(h)),
  `found: ${JSON.stringify(hrefs)}`);

check('CodePanel imports the builder',
  /import\s*\{[^}]*buildMergeRequestUrl[^}]*\}\s*from\s*'\.\.\/utils\/mrUrl'/.test(codePanel),
  'The link must be assembled by utils/mrUrl.js.');

// safeHref is the FILTER approach this change replaced at this sink. Re-adding
// it here would mean a response value is being passed through again.
check('CodePanel no longer imports safeHref',
  !/safeHref/.test(codePanel),
  'safeHref filters a tainted value; this sink is fixed by not having one.');

// ── 3. The builder must not be handed a whole URL ───────────────────────────
check('builder takes (projectPath, iid), not a URL',
  /export function buildMergeRequestUrl\(projectPath, mrIid\)/.test(mrUrl),
  'Changing the signature to accept a URL string would reopen the path.');

// The origin must come from build-time config. If the base ever starts coming
// from an argument or a fetch, every guarantee above collapses.
check('base URL is read from import.meta.env (build time)',
  /import\.meta\.env[\s\S]{0,80}VITE_GITLAB_BASE_URL/.test(mrUrl),
  'The origin must be inlined at build time, never received at runtime.');

check('builder validates the path against a charset allowlist',
  /SEGMENT_RE\s*=\s*\/\^/.test(mrUrl),
  'Without a strict charset the path could carry an attribute break.');

check('builder re-derives the iid through Number()',
  /Number\(String\(mrIid\)/.test(mrUrl) || /typeof mrIid === 'number'/.test(mrUrl),
  'Interpolating the raw iid would put response text back in the output.');

// ── 4. Fail closed: no link rather than a dead '#' anchor ───────────────────
// Returning '#' is what made the old artifact_ref link look clickable and do
// nothing. The builder must return null and the component must render text.
check('builder never returns a "#" placeholder',
  !/return\s*'#'/.test(mrUrl) && !/return\s*"#"/.test(mrUrl),
  'Return null so the caller can render plain text instead of a dead link.');

check('CodePanel renders text when the URL cannot be built',
  /if\s*\(!url\)/.test(codePanel),
  'A missing base or a pre-migration row must degrade to a non-link.');

// ── 5. The backend must not serialise mr_url to the browser ─────────────────
// The frontend assertions are only durable if the value stops being sent.
let backend = null;
try {
  backend = stripPyComments(await readFile(files.backendMr, 'utf8'));
} catch {
  console.log('SKIP  backend _mr_view check (backend/ not present in this tree)');
}

if (backend !== null) {
  const view = /def _mr_view\([\s\S]*?\n    \}/.exec(backend);
  if (!view) {
    check('_mr_view found in backend', false, 'Could not locate _mr_view() to inspect.');
  } else {
    check('_mr_view does not serialise mr_url',
      !/"mr_url"/.test(view[0]),
      'Sending mr_url to the browser recreates the source of all three findings.');
    check('_mr_view serialises project_path',
      /"project_path"/.test(view[0]),
      'The UI needs project_path to rebuild the link.');
    check('_mr_view serialises mr_iid',
      /"mr_iid"/.test(view[0]),
      'The UI needs mr_iid to rebuild the link.');
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
if (fail === 0) {
  console.log('MR links are built from build-time config; no API value reaches the href.');
}
process.exit(fail === 0 ? 0 : 1);
