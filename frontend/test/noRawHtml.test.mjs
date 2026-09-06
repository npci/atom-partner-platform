// Guard: no raw-HTML injection sinks anywhere in the frontend source.
//
// Zero-dependency by design, matching test/safeUrl.test.mjs: this frontend has
// no test runner, and adding eslint + eslint-plugin-react purely for one rule
// would pull ~100 packages into a codebase that is itself under SCA/SBOM
// scanning. Fixing a finding by adding dependencies that generate new findings
// is a bad trade. Node's stdlib is enough.
//
//   Run with:  npm run test:noRawHtml
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// This is the enforcement half of the `react` SBOM annotation
// (sonatype-2017-0717, Security-Low). That advisory is about React's
// `dangerouslySetInnerHTML` being an XSS vector when fed untrusted HTML. It
// applies to EVERY React version ever released — note the 2017 in the ID, while
// we are on 19.2.5 — and ships no fixed version, so it can never be cleared by
// upgrading. It is cleared by asserting that the dangerous API is not used.
//
// Our VEX statement says: "dangerouslySetInnerHTML does not appear anywhere in
// the application source; markdown is rendered via react-markdown without
// rehype-raw, so raw HTML is escaped to text." That is a claim about the code as
// it is TODAY. The realistic way it becomes false is mundane: a ticket arrives
// like "render the authority's rich-text notes with formatting", someone adds
// `rehype-raw` in one line, and authority-supplied content starts flowing into
// the DOM as live HTML. The annotation would then be a false audit record, which
// in a regulated NPCI context is worse than an open low-severity finding.
//
// So this test is what keeps the annotation honest. If it fails, either remove
// the sink or withdraw the annotation — do not weaken the test.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', 'src');
const PKG = join(HERE, '..', 'package.json');

// ── Banned patterns ─────────────────────────────────────────────────────────
//
// Each entry explains WHY it is banned, because a future reader deciding whether
// to add an exception needs the reasoning, not just the rule.
const BANNED = [
  {
    // The advisory's actual subject. React will not escape this content; it goes
    // to innerHTML verbatim.
    pattern: /dangerouslySetInnerHTML/,
    what: 'dangerouslySetInnerHTML',
    why: 'React does not escape this content — it is assigned to innerHTML verbatim. '
       + 'This is the exact sink sonatype-2017-0717 describes.',
  },
  {
    // react-markdown is SAFE BY DEFAULT precisely because it builds a React
    // element tree and drops raw HTML. rehype-raw re-enables parsing of embedded
    // HTML, which turns every markdown surface (agent output, NPCI-supplied
    // documents) into an HTML injection point.
    pattern: /\brehype-raw\b/,
    what: 'rehype-raw',
    why: 'Re-enables raw HTML parsing inside react-markdown, converting every '
       + 'markdown surface into an HTML injection point. react-markdown escapes '
       + 'raw HTML to text without it — that default is what makes the React '
       + 'annotation defensible.',
  },
  {
    // Same effect as rehype-raw, different package.
    pattern: /\brehype-dangerous/,
    what: 'rehype-dangerous*',
    why: 'Same raw-HTML effect as rehype-raw.',
  },
  {
    // Direct DOM escape hatches bypass React entirely, so the React-level
    // reasoning above would no longer cover them.
    pattern: /\.innerHTML\s*=/,
    what: 'element.innerHTML assignment',
    why: 'Bypasses React\'s escaping entirely. Use text content or JSX instead.',
  },
  {
    pattern: /\.outerHTML\s*=/,
    what: 'element.outerHTML assignment',
    why: 'Bypasses React\'s escaping entirely.',
  },
  {
    pattern: /insertAdjacentHTML\s*\(/,
    what: 'insertAdjacentHTML()',
    why: 'Parses its argument as HTML — same risk as innerHTML.',
  },
  {
    // document.write is both an XSS sink and a rendering hazard.
    pattern: /document\s*\.\s*write(ln)?\s*\(/,
    what: 'document.write()',
    why: 'Parses its argument as HTML.',
  },
];

// ── Approved exceptions ─────────────────────────────────────────────────────
//
// `srcDoc` on the prototype-preview iframes is the ONE place this application
// intentionally renders generated HTML, and it is not covered by the patterns
// above (it is an attribute, not a DOM sink). It is hardened in
// src/utils/safeHtmlFrame.js by two independent controls: a `sandbox` attribute
// WITHOUT `allow-same-origin` (opaque origin — cannot reach parent DOM, cookies
// or storage) and an injected CSP of `default-src 'none'; connect-src 'none';
// form-action 'none'` (no network egress at all).
//
// It is listed here so the file is checked for CONTINUED PRESENCE of those
// controls, rather than being silently ignored.
const CSP_GUARDED_FILES = ['utils/safeHtmlFrame.js'];

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...walk(full));
    } else if (/\.(jsx?|mjs|tsx?)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

// Strip comments so a pattern named inside an explanatory comment (like the ones
// in safeHtmlFrame.js, which discuss the risk at length) is not reported as a
// use. Crude but adequate: this only needs to avoid false positives on prose.
// Newlines are PRESERVED so that reported line numbers still match the real
// file. Deleting comment lines outright shifted every subsequent line number,
// which made failures point at unrelated code and cost real debugging time.
function stripComments(code) {
  return code
    // Block comments collapse to their own newlines, keeping the line count.
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ''))
    // `[ \t]*` NOT `\s*`: \s matches newlines, so `^\s*//.*$` greedily ate the
    // blank lines above each comment and shifted line numbers anyway.
    .replace(/^[ \t]*\/\/.*$/gm, '');   // whole-line comments (newline kept by ^..$)
}

let pass = 0;
const failures = [];

// ── 1. No banned sink in any source file ────────────────────────────────────
const files = walk(SRC);
if (files.length < 10) {
  failures.push(`Only ${files.length} source files scanned — the scan target `
    + `may have moved. A guard that examines nothing passes vacuously.`);
}

for (const file of files) {
  const rel = relative(join(HERE, '..'), file).replace(/\\/g, '/');
  const code = stripComments(readFileSync(file, 'utf8'));
  for (const { pattern, what, why } of BANNED) {
    const m = code.match(pattern);
    if (m) {
      // Report the line number so the failure is immediately actionable.
      const line = code.slice(0, m.index).split('\n').length;
      failures.push(`${rel}:${line} uses ${what}\n      why banned: ${why}`);
    } else {
      pass += 1;
    }
  }
}

// ── 2. rehype-raw must not be a declared dependency either ──────────────────
// Catching it only in source would miss the state where it has been installed
// and is one import away.
const pkg = JSON.parse(readFileSync(PKG, 'utf8'));
const allDeps = {
  ...(pkg.dependencies || {}),
  ...(pkg.devDependencies || {}),
};
for (const name of Object.keys(allDeps)) {
  if (/^rehype-raw$|^rehype-dangerous/.test(name)) {
    failures.push(`package.json declares ${name} — remove it. It re-enables raw `
      + `HTML in react-markdown and invalidates the React SBOM annotation.`);
  } else {
    pass += 1;
  }
}

// ── 3. The srcDoc iframe hardening must still be in place ───────────────────
// The React annotation leans on these controls, so their removal has to break a
// test rather than pass review as a cleanup.
for (const rel of CSP_GUARDED_FILES) {
  const full = join(SRC, rel);
  let raw;
  try {
    raw = readFileSync(full, 'utf8');
  } catch {
    failures.push(`${rel} is missing — the srcDoc CSP hardening it provides is `
      + `relied upon by the React 'not affected' annotation.`);
    continue;
  }

  // ── Comments MUST be stripped before checking for the directives ──────────
  //
  // This block previously read the file raw while the sink scan above used
  // stripComments(). That inconsistency was a real bypass: deleting a directive
  // from the CSP array but leaving its name in a comment —
  //
  //     // removed connect-src 'none' to allow telemetry
  //
  // — satisfied `code.includes("connect-src 'none'")` while the emitted CSP no
  // longer contained it. The iframe regained network egress and this guard
  // still reported 197 checks passed, 0 failed. Verified as an actual bypass
  // before this fix, and as detected after it.
  //
  // The directive must be present in EXECUTABLE code, so the same normalisation
  // the sink scan uses applies here too.
  const code = stripComments(raw);

  const required = [
    ["default-src 'none'", 'denies every fetch directive not re-granted'],
    ["connect-src 'none'", 'blocks fetch / XHR / WebSocket exfiltration'],
    ["form-action 'none'", 'blocks POSTing data out'],
    ["object-src 'none'", 'blocks plugin/object embedding'],
    ["base-uri 'none'", 'stops <base> rewriting relative URLs to an attacker host'],
  ];
  for (const [needle, purpose] of required) {
    if (!code.includes(needle)) {
      failures.push(`${rel} no longer contains "${needle}" (${purpose}) in `
        + `executable code. The prototype-preview iframe would regain network `
        + `egress. NOTE: mentioning the directive in a comment does not count.`);
    } else {
      pass += 1;
    }
  }

  // The CSP is only a control if it is actually injected into the frame markup.
  // An intact directive array whose value is never inserted would pass every
  // check above while protecting nothing.
  if (!/<meta[^>]+http-equiv/i.test(code) && !/http-equiv/i.test(code)) {
    failures.push(`${rel} no longer emits a <meta http-equiv> CSP tag — the `
      + `directive list is present but never applied to the frame.`);
  } else {
    pass += 1;
  }

  // sandbox without allow-same-origin is what keeps the frame in an opaque
  // origin. Its presence is checked in the JSX that renders the frame, below.
  if (/allow-same-origin/.test(stripComments(code))) {
    failures.push(`${rel} grants allow-same-origin, which collapses the opaque-`
      + `origin protection: the frame could read parent DOM and cookies.`);
  } else {
    pass += 1;
  }
}

// ── 4. Every srcDoc usage must be sandboxed without allow-same-origin ───────
for (const file of files) {
  const rel = relative(join(HERE, '..'), file).replace(/\\/g, '/');
  const code = stripComments(readFileSync(file, 'utf8'));
  if (!/srcDoc/.test(code)) continue;

  if (!/sandbox\s*=/.test(code)) {
    failures.push(`${rel} renders srcDoc without a sandbox attribute — the `
      + `frame would run in the app's own origin.`);
  } else {
    pass += 1;
  }
  if (/sandbox\s*=\s*["'{][^}"']*allow-same-origin/.test(code)) {
    failures.push(`${rel} renders srcDoc with allow-same-origin — this defeats `
      + `the opaque-origin protection.`);
  } else {
    pass += 1;
  }
}

// ── 5. Every dynamic href/src must go through safeHref() ────────────────────
//
// WHY THIS EXISTS
//
// Checkmarx reported Reflected XSS (CWE-79) against two `href` sinks:
//   - components/CodePanel.jsx:352   ← api.js getCodeMergeRequest (mr.mr_url)
//   - pages/ChangeDetail.jsx:2218    ← api.js getChange (artifact_ref)
//
// Both were ALREADY wrapped in safeHref() at the scanned commit, and
// test/safeUrl.test.mjs proves `javascript:` payloads return '#'. The finding
// is a false positive on exploitability — but the underlying risk it points at
// is real and structural: nothing stopped someone adding a THIRD link with a
// bare `href={someServerValue}`. The sanitiser is only as good as its
// application, and "remember to call safeHref" is a code-review convention, not
// a control.
//
// This turns the convention into a build failure for the careless case: a bare
// `href={value}` now breaks the test suite rather than shipping and reappearing
// in the next Checkmarx scan.
//
// ── KNOWN LIMITS — read before trusting this ────────────────────────────────
//
// This is a REGEX over JSX, not an AST analysis, and it only inspects the first
// token after the opening brace. It is a backstop against the common mistake,
// NOT a proof of absence. Verified evasions that pass this check:
//
//     href={safeHref(a) + tainted}        // concatenation after an approved call
//     href={BRAND_LOGO_URL + tainted}     // concatenation after an exempt value
//     href={videoUrl || tainted}          // exempt value short-circuiting
//     <a {...{ href: tainted }} />        // spread props — no `href=` token
//
// Anyone writing one of those is past the point a lint can help; code review and
// the Checkmarx scan itself are the controls there. Do not read a green run as
// "no unsafe href exists" — read it as "no obviously unsafe href exists".
//
// Only `{...}` expression values are checked. Static string literals
// (href="/docs", href="#") are inert and allowed.
const URL_ATTRS = /\b(href|src)\s*=\s*\{/g;

// Wrappers that terminate taint:
//   safeHref            rebuilds the URL from literals + a build-time allowlist
//   buildMergeRequestUrl  assembles a GitLab MR link from a build-time base URL,
//                         a charset-validated path and an integer iid, so no
//                         response string reaches the output (utils/mrUrl.js)
//   withBasePath/basePath emit app-relative paths from build config
const APPROVED_URL_WRAPPERS =
  /^(safeHref|buildMergeRequestUrl|withBasePath|basePath)\s*\(/;

// Values that are provably not server-controlled, so safeHref would add nothing.
// Each entry is an exception with a stated reason — keep this list short, and
// only add a value whose origin you have actually traced to a non-network source.
const NON_NETWORK_URL_VALUES = [
  {
    // Build-time Vite env constant (src/brand.js reads VITE_BRAND_LOGO_URL).
    // Baked into the bundle at compile time; no API response can influence it.
    pattern: /^BRAND_LOGO_URL\b/,
    why: 'build-time env constant, not a server response',
  },
  {
    // URL.createObjectURL(blob) result — a local blob: URL minted in-page from
    // an already-downloaded Blob. It never contains remote text, and safeHref
    // would reject the blob: scheme and break video preview.
    pattern: /^videoUrl\b/,
    why: 'local URL.createObjectURL blob handle, minted in-page',
  },
];

for (const file of files) {
  const rel = relative(join(HERE, '..'), file).replace(/\\/g, '/');
  const code = stripComments(readFileSync(file, 'utf8'));

  for (const m of code.matchAll(URL_ATTRS)) {
    const attr = m[1];
    // Take what follows the opening brace, up to a reasonable window, and check
    // the first thing in the expression.
    const expr = code.slice(m.index + m[0].length).trimStart();
    const line = code.slice(0, m.index).split('\n').length;

    // A bare template/string literal inside braces is still static content.
    const isStaticLiteral = /^(["'`])(?:(?!\1)[^\\$])*\1\s*\}/.test(expr);

    // `href={url}` where `url` is a local const assigned from an approved
    // wrapper is safe, and extracting to a variable is the natural way to write
    // a component that renders a link conditionally. Resolve a bare identifier
    // back to its `const <name> = ...` in the same file and judge THAT.
    //
    // Deliberately one level deep and same-file only: this is a lint, not an
    // analyser, and a chain long enough to defeat it is long enough that the
    // limitation note above applies.
    let resolved = expr;
    const bareIdent = /^([A-Za-z_$][\w$]*)\s*\}/.exec(expr);
    if (bareIdent) {
      const decl = new RegExp(
        `\\b(?:const|let|var)\\s+${bareIdent[1]}\\s*=\\s*([^;\\n]+)`
      ).exec(code);
      if (decl) resolved = decl[1].trim();
    }

    const exempt = NON_NETWORK_URL_VALUES.some(
      (e) => e.pattern.test(expr) || e.pattern.test(resolved)
    );

    // A conditional that wraps EVERY branch is safe, and is a natural way to
    // write "one of two links". Flagging it would be a false positive, and false
    // positives are how a guard gets weakened or deleted. Accept it only when no
    // branch is left bare: extract the branch expressions and require each to be
    // an approved wrapper, an exempt value, or an inert literal such as '#'.
    let allBranchesSafe = false;
    if (/^[^;{}]*\?/.test(expr)) {
      const cond = expr.slice(0, expr.indexOf('}') === -1 ? expr.length : expr.indexOf('}'));
      const branches = cond.split(/[?:]/).slice(1).map((b) => b.trim()).filter(Boolean);
      allBranchesSafe = branches.length > 0 && branches.every((b) =>
        APPROVED_URL_WRAPPERS.test(b)
        || NON_NETWORK_URL_VALUES.some((e) => e.pattern.test(b))
        || /^(["'`])[^"'`]*\1$/.test(b)     // inert literal branch, e.g. '#'
        || b === 'undefined' || b === 'null');
    }

    if (isStaticLiteral || APPROVED_URL_WRAPPERS.test(expr)
        || APPROVED_URL_WRAPPERS.test(resolved) || exempt || allBranchesSafe) {
      pass += 1;
    } else {
      failures.push(
        `${rel}:${line} sets ${attr}={...} without safeHref()\n`
        + `      why banned: server-supplied URLs reaching an ${attr} sink allow `
        + `javascript:/data: scheme injection (XSS, CWE-79) and open redirection. `
        + `This is the exact sink pattern Checkmarx flagged. `
        + `Wrap the value: ${attr}={safeHref(value)}.`
      );
    }
  }
}

// The guard above is meaningless if safeHref itself stops sanitising, so assert
// its core behaviours here too — this file is the one CI always runs.
{
  const su = join(SRC, 'utils', 'safeUrl.js');
  let code;
  try {
    code = stripComments(readFileSync(su, 'utf8'));
  } catch {
    code = null;
    failures.push('utils/safeUrl.js is missing — every href sink in the app '
      + 'depends on it.');
  }
  if (code) {
    // It must reject anything that is not http(s) by construction, rather than
    // blocklisting `javascript:` (which is bypassable via case/whitespace).
    if (!/\^\(https\?\)/.test(code)) {
      failures.push('utils/safeUrl.js no longer pins the scheme to an ^(https?) '
        + 'allowlist pattern. A blocklist approach is bypassable — restore the '
        + 'anchored allowlist regex.');
    } else {
      pass += 1;
    }
    // The default-deny return is what makes an unrecognised URL inert.
    if (!/return\s+'#'/.test(code)) {
      failures.push("utils/safeUrl.js no longer returns '#' as its default-deny "
        + 'value. Unvalidated URLs must degrade to an inert link.');
    } else {
      pass += 1;
    }
  }
}

// ── report ──────────────────────────────────────────────────────────────────
for (const f of failures) console.log(`FAIL  ${f}`);
console.log(`\n${pass} checks passed, ${failures.length} failed`);
if (failures.length === 0) {
  console.log(
    'No raw-HTML sinks. The "not affected" annotation for react '
    + '(sonatype-2017-0717) remains accurate.'
  );
}
process.exit(failures.length === 0 ? 0 : 1);
