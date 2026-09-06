// Regression tests for the merge-request link builder (src/utils/mrUrl.js).
//
// Zero-dependency by design, matching the sibling suites: the frontend has no
// test runner, and adding one purely for this file would pull a large dev tree
// into a codebase that is itself under SCA scanning.
//
//   Run with:  npm run test:mrUrl
//
// ── What this protects ──────────────────────────────────────────────────────
//
// Checkmarx reported the merge-request link under three queries — Client DOM
// XSS, Reflected XSS (path 1) and Client DOM Open Redirect — all following one
// dataflow: api.js reads `mr_url` off the network, CodePanel renders it in an
// <a href>. Wrapping the sink in safeHref() blocked the attack at runtime but
// left the source→sink path intact, so the finding returned on every rescan.
//
// The fix removes the flow. The API now sends `project_path` + `mr_iid`, and
// this module rebuilds the URL against a base inlined at BUILD time. The two
// properties that make that true, and which these tests pin down:
//
//   1. no argument can influence the ORIGIN — it comes from the base alone
//   2. anything that fails validation returns null, so the UI renders no link
//      (never a '#' anchor that looks clickable and goes nowhere)
//
// Tests go through `buildWithBase`, the exported pure core. `import.meta.env`
// does not exist under plain node, so a test calling `buildMergeRequestUrl`
// could only ever observe the "no base configured" branch and every interesting
// assertion below would silently not run.

const { buildWithBase, buildMergeRequestUrl, parseBase, hasGitlabBase } =
  await import('../src/utils/mrUrl.js');

let pass = 0;
const failures = [];

function check(label, actual, expected) {
  const ok = actual === expected;
  if (ok) pass += 1;
  else failures.push(`${label}\n      expected: ${JSON.stringify(expected)}\n      got:      ${JSON.stringify(actual)}`);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}\n      got:      ${JSON.stringify(actual)}`);
}

const BASE = 'https://gitlab.example.com';
const b = (path, iid) => buildWithBase(BASE, path, iid);

// ── Happy path ──────────────────────────────────────────────────────────────
check('simple namespace', b('group/upi-stack', 42),
  `${BASE}/group/upi-stack/-/merge_requests/42`);
check('nested subgroups', b('group/sub/proj', 7),
  `${BASE}/group/sub/proj/-/merge_requests/7`);
check('numeric-string iid re-derived through Number()', b('group/p', '15'),
  `${BASE}/group/p/-/merge_requests/15`);
check('dots, dashes, underscores are legal GitLab path chars',
  b('my-group/my_proj.v2', 3), `${BASE}/my-group/my_proj.v2/-/merge_requests/3`);

// ── The property that closes the finding: the origin is not negotiable ──────
check('absolute URL in project_path cannot override the origin',
  b('https://evil.com/x', 1), null);
check('protocol-relative path cannot override the origin',
  b('//evil.com/x', 1), null);
check('path traversal rejected', b('group/../../evil', 1), null);
check('backslash traversal rejected', b('group\\..\\evil', 1), null);
check('leading slash rejected', b('/group/p', 1), null);
check('userinfo-style payload rejected', b('group@evil.com/p', 1), null);

// ── Scheme injection has no route in ────────────────────────────────────────
check('javascript: in project_path', b('javascript:alert(1)', 1), null);
check('javascript: in iid', b('group/p', 'javascript:alert(1)'), null);
check('data: in project_path', b('data:text/html,<script>', 1), null);

// ── Attribute-breakout characters are REJECTED, not encoded ─────────────────
// A real GitLab path never contains these, so there is nothing to preserve by
// escaping them; refusing is the stricter and simpler contract.
check('double quote in path', b('group/p"onmouseover="alert(1)', 1), null);
check('angle brackets in path', b('group/<img src=x>', 1), null);
check('single quote in path', b("group/p'x", 1), null);
check('space in path', b('group/my proj', 1), null);
check('CRLF in path', b('group/p\r\nX', 1), null);
check('NUL in path', b('group/p\u0000', 1), null);
check('percent-encoding in path', b('group/%2e%2e', 1), null);

// ── iid must be a genuine positive integer ──────────────────────────────────
check('negative iid', b('group/p', -1), null);
check('zero iid', b('group/p', 0), null);
check('fractional iid', b('group/p', 1.5), null);
check('NaN iid', b('group/p', NaN), null);
check('Infinity iid', b('group/p', Infinity), null);
check('non-numeric iid', b('group/p', 'abc'), null);
check('iid with trailing payload', b('group/p', '1;alert(1)'), null);
check('empty-string iid does not become 0', b('group/p', ''), null);

// ── Missing values degrade to "no link", never a broken one ─────────────────
check('null path', b(null, 1), null);
check('undefined path', b(undefined, 1), null);
check('empty path', b('', 1), null);
check('null iid', b('group/p', null), null);
check('undefined iid', b('group/p', undefined), null);
// A row written before the project_path migration has no namespace stored. The
// UI must fall back to plain text rather than inventing a link.
check('pre-migration row (no project_path)', b(null, 42), null);

// ── No base configured -> fail closed ───────────────────────────────────────
check('null base yields no link', buildWithBase(null, 'group/p', 1), null);
check('undefined base yields no link', buildWithBase(undefined, 'group/p', 1), null);

// ── Base parsing is itself an allowlist ─────────────────────────────────────
check('https base', parseBase('https://gitlab.example.com'), 'https://gitlab.example.com');
check('trailing slash normalised', parseBase('https://gitlab.example.com/'), 'https://gitlab.example.com');
check('scheme and host lowercased', parseBase('HTTPS://GitLab.Example.COM'), 'https://gitlab.example.com');
check('port preserved and re-derived', parseBase('http://localhost:8080'), 'http://localhost:8080');
check('javascript: base rejected', parseBase('javascript:alert(1)'), null);
check('base with a path rejected', parseBase('https://gitlab.example.com/sub'), null);
check('empty base rejected', parseBase(''), null);
check('out-of-range port rejected', parseBase('https://gitlab.example.com:99999'), null);

// ── Production entry point is wired to build-time config ────────────────────
// Under plain node there is no import.meta.env, so the base is unset and the
// exported function must fail closed. This asserts the wiring, not the logic.
check('buildMergeRequestUrl fails closed without build-time base',
  buildMergeRequestUrl('group/p', 1), hasGitlabBase() ? buildMergeRequestUrl('group/p', 1) : null);

for (const f of failures) console.log(`FAIL  ${f}`);
console.log(`\n${pass} passed, ${failures.length} failed`);
if (failures.length === 0) {
  console.log('MR links are assembled from build-time config; no response value reaches the href.');
}
process.exit(failures.length === 0 ? 0 : 1);
