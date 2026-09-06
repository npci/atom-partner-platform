// Guard: the clickjacking frame buster must stay in frontend/index.html.
//
// Zero-dependency by design, matching test/noRawHtml.test.mjs and
// test/safeUrl.test.mjs — this frontend has no test runner, and adding one for
// a handful of assertions would pull packages into a codebase that is itself
// under SCA/SBOM scanning.
//
//   Run with:  npm run test:frameBuster
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// Checkmarx "Potential Clickjacking on Legacy Browsers" (Low, Result State:
// Confirmed) reports against frontend/index.html line 1. It is a STATIC check
// on the HTML document: it looks for a frame-busting script in the markup. It
// never issues an HTTP request, so the `X-Frame-Options` and CSP
// `frame-ancestors` headers in deploy/edge.nginx.conf — which are the controls
// that actually protect real users — are invisible to it and cannot close it.
//
// The finding was previously "fixed" with a <meta http-equiv> CSP tag carrying
// frame-ancestors. That fix does not work, for two independent reasons:
//
//   1. CSP Level 3 §3.3 says frame-ancestors "will be ignored" when delivered
//      in a <meta> tag. It only functions as an HTTP header. So the tag stopped
//      no framing whatsoever — it satisfied `grep frame-ancestors` and nothing
//      else.
//   2. Even if it had worked, it is not a frame-busting SCRIPT, which is what
//      the query looks for. The finding recurred on the next scan.
//
// So the actual fix is the <style> + <script> pair in index.html, and this test
// exists so that a future "cleanup" of that unusual-looking markup fails CI
// instead of silently reopening the finding on the next Checkmarx scan.
//
// The ordering property is the part most likely to be broken by accident and is
// the part that carries the security value: the document is hidden FIRST and
// revealed only after `self === top` passes. If someone reverses that, the page
// renders framed and clickable for however long the script takes to run, and
// every assertion about "the UI cannot be clicked" stops being true.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const INDEX = join(HERE, '..', 'index.html');

let pass = 0;
const failures = [];

function check(ok, msg) {
  if (ok) pass += 1;
  else failures.push(msg);
}

let html;
try {
  html = readFileSync(INDEX, 'utf8');
} catch {
  console.log('FAIL  frontend/index.html is missing — nothing to verify.');
  process.exit(1);
}

// Strip HTML comments before asserting. The file explains the whole mechanism
// at length in prose, so an un-stripped scan would happily match every pattern
// below against the commentary while the real markup was gone.
const markup = html.replace(/<!--[\s\S]*?-->/g, '');

// ── 1. The UI must be hidden by default ─────────────────────────────────────
// Without this, a browser that blocks the script renders the page framed and
// interactive, which is the exact condition being defended against.
check(
  /<style[^>]*>[\s\S]*?html\s*\{[^}]*display\s*:\s*none[^}]*\}[\s\S]*?<\/style>/i.test(markup),
  'index.html no longer hides <html> with display:none by default. The frame '
  + 'buster depends on the page starting hidden: if the script is blocked or '
  + 'fails, a visible page is a clickable page.'
);

// ── 2. The reveal must be conditional on not being framed ───────────────────
check(
  /self\s*!==\s*top|self\s*===\s*top|window\.self\s*!==\s*window\.top|window\.self\s*===\s*window\.top/.test(markup),
  'index.html no longer compares self against top. That comparison is the only '
  + 'thing distinguishing "top-level document" from "framed by an attacker".'
);

// ── 3. Hide-before-reveal ordering ──────────────────────────────────────────
// The security property is temporal, not just structural: the hiding style must
// appear BEFORE the script that can undo it. A script placed above the style
// would reveal a page that is not yet hidden and then hide it, producing a
// visible, clickable flash in the framed case.
const styleAt = markup.search(/<style[^>]*>[\s\S]*?display\s*:\s*none/i);
const selfTopAt = markup.search(/self\s*[!=]==\s*top|window\.self\s*[!=]==\s*window\.top/);
check(
  styleAt !== -1 && selfTopAt !== -1 && styleAt < selfTopAt,
  'index.html no longer hides the document BEFORE running the frame check. '
  + 'Reversing this order leaves a window in which a framed page is rendered '
  + 'and clickable.'
);

// ── 4. The frame buster must be inline, not an imported module ──────────────
// An external or `type="module"` script is deferred: it executes after the
// document has parsed and painted, which defeats the point entirely. It would
// also be invisible to the Checkmarx static check, so the finding would recur.
const bustingScript = /<script(?![^>]*\bsrc\s*=)(?![^>]*type\s*=\s*["']module["'])[^>]*>[\s\S]*?self\s*[!=]==\s*top[\s\S]*?<\/script>/i;
check(
  bustingScript.test(markup),
  'the frame-busting logic is no longer an inline, non-deferred <script> in '
  + 'index.html. A src= or type="module" script runs too late to prevent the '
  + 'framed page from painting, and is not visible to the Checkmarx static '
  + 'check that reports this finding.'
);

// ── 5. It must live in <head> ───────────────────────────────────────────────
// In <body> it runs after the markup above it has already been parsed and may
// have painted.
const headEnd = markup.search(/<\/head>/i);
check(
  headEnd !== -1 && selfTopAt !== -1 && selfTopAt < headEnd,
  'the frame buster is no longer inside <head>. Placed later, content above it '
  + 'can paint before the frame check runs.'
);

// ── 6. The breakout navigation should still be attempted ────────────────────
// Best-effort only — a cross-origin parent blocks it — but Checkmarx's guidance
// asks for the navigation attempt in addition to hiding the UI.
check(
  /top\.location\s*=|top\[\s*['"]location['"]\s*\]\s*=/.test(markup),
  'index.html no longer attempts to navigate the top window out of the frame. '
  + 'This is best-effort, but it is part of the recommended mitigation.'
);

// ── 7. The header-based controls must still exist at the edge ───────────────
// The frame buster is the LEGACY fallback. If someone deletes the nginx headers
// believing index.html now covers it, real users lose the control that actually
// protects them in every current browser.
const EDGE = join(HERE, '..', '..', 'deploy', 'edge.nginx.conf');
let conf = null;
try {
  conf = readFileSync(EDGE, 'utf8');
} catch {
  failures.push('deploy/edge.nginx.conf is missing — it carries the CSP '
    + 'frame-ancestors and X-Frame-Options headers that are the PRIMARY '
    + 'clickjacking control for modern browsers.');
}
if (conf) {
  const active = conf.split('\n').filter((l) => !/^\s*#/.test(l)).join('\n');

  // The policy is now built up in an nginx variable ($csp_app) rather than
  // written inline in the add_header line, because it grew far past the point
  // where one line was readable. So checking the add_header line alone is not
  // enough: resolve the variable and assert on the value that is actually sent.
  //
  // The earlier version of this check matched
  //   /add_header\s+Content-Security-Policy[^;]*frame-ancestors/
  // which broke on that refactor — `[^;]*` cannot cross the semicolons inside
  // a real multi-directive policy, and the directive now lives in the variable
  // rather than on the add_header line. It failed while the header was
  // completely correct. Resolving variables keeps the assertion honest without
  // making it brittle to how the value is assembled.
  const vars = {};
  for (const m of active.matchAll(/set\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s+"([^"]*)"\s*;/g)) {
    vars[m[1]] = m[2];
  }

  const cspHeaders = [...active.matchAll(/add_header\s+Content-Security-Policy\s+("([^"]*)"|\$[A-Za-z0-9_]+)/gi)]
    .map((m) => {
      const raw = m[1];
      return raw.startsWith('$') ? (vars[raw] ?? '') : (m[2] ?? '');
    });

  check(
    cspHeaders.length > 0,
    'deploy/edge.nginx.conf no longer sends a Content-Security-Policy header at all.'
  );

  // The APP policy is the one that must deny framing outright. The
  // preview-shell location intentionally sends frame-ancestors 'self' — it is
  // meant to be framed by this app — so requiring 'none' everywhere would be
  // wrong. Require that at least one policy denies framing, and that none of
  // them permit it from anywhere.
  check(
    cspHeaders.some((p) => /frame-ancestors\s+'none'/i.test(p)),
    "deploy/edge.nginx.conf no longer sends CSP frame-ancestors 'none' for the "
    + 'application. This is the control that actually stops framing in current '
    + 'browsers — the frame buster in index.html is only the legacy fallback.'
  );
  check(
    !cspHeaders.some((p) => /frame-ancestors\s+[^;"]*\*/i.test(p)),
    'a CSP in deploy/edge.nginx.conf allows framing from a wildcard origin.'
  );
  check(
    /add_header\s+X-Frame-Options/i.test(active),
    'deploy/edge.nginx.conf no longer sends X-Frame-Options.'
  );

  // ── 8. The preview-shell exception must stay an exception ─────────────────
  //
  // WHY THIS EXISTS
  //
  // public/preview-shell.html is the one page in this application that is
  // MEANT to be framed: it hosts AI-generated prototype markup inside a
  // sandboxed iframe, so it is framed by the app on every use. It therefore
  // cannot carry the frame buster asserted above — a `self !== top` check
  // would blank the feature on every legitimate render — and it is served with
  // `frame-ancestors 'self'` rather than 'none'.
  //
  // That exception is deliberate and defensible. The danger is that it is
  // stated only in prose. Checks 2-3 above accept ANY policy that is not a
  // wildcard, so widening this one location from 'self' to a foreign origin
  // would satisfy every assertion in this file while making the page framable
  // by an attacker. And this page is a worse clickjacking target than most: it
  // executes untrusted inline script by design, so an attacker who could frame
  // it from their own origin would be overlaying a surface that already runs
  // hostile markup.
  //
  // The narrow requirement is therefore: this location's framing policy must
  // permit exactly 'self', and nothing else.
  const shellBlock = (() => {
    const start = active.search(/location\s*=\s*\S*preview-shell\.html\s*\{/);
    if (start === -1) return null;
    // Brace-match rather than regex to the next '}': the block contains a CSP
    // string with no braces today, but a future directive could add one and a
    // lazy match would silently truncate the block and skip the assertions.
    let depth = 0;
    for (let i = active.indexOf('{', start); i < active.length; i += 1) {
      if (active[i] === '{') depth += 1;
      else if (active[i] === '}') {
        depth -= 1;
        if (depth === 0) return active.slice(start, i + 1);
      }
    }
    return null;
  })();

  // Absence is not a failure: the preview feature is in-flight at the time of
  // writing, and a repo without that location block is simply a repo without
  // the exception. Only assert once the block exists.
  if (shellBlock) {
    const shellCsp = (shellBlock.match(
      /add_header\s+Content-Security-Policy\s+("([^"]*)"|\$[A-Za-z0-9_]+)/i
    ) || [])[2] || '';
    const shellFa = (shellCsp.match(/frame-ancestors\s+([^;"]*)/i) || [])[1]?.trim();

    check(
      Boolean(shellFa),
      'the preview-shell location in deploy/edge.nginx.conf no longer sets '
      + 'frame-ancestors. It would then inherit nothing (nginx add_header does '
      + 'not merge with server-level directives), leaving the page — which runs '
      + 'untrusted inline script by design — framable by any origin.'
    );

    if (shellFa) {
      check(
        shellFa === "'self'",
        `the preview-shell location allows framing by ${shellFa} instead of `
        + "'self'. This page renders untrusted prototype markup and executes it; "
        + 'it must be embeddable only by this application. Widening it makes the '
        + 'one intentionally-framable page in the app an attacker-framable one.'
      );
    }

    check(
      /add_header\s+X-Frame-Options\s+"?SAMEORIGIN/i.test(shellBlock),
      'the preview-shell location no longer sends X-Frame-Options: SAMEORIGIN. '
      + 'Browsers predating CSP Level 2 would fall back to no framing control '
      + 'at all on this page.'
    );
  }
}

// ── 9. The framable page must not be given a frame buster by mistake ────────
//
// The mirror of check 8. A future reader who sees "every page needs a frame
// buster" — a reasonable reading of this very file — could add one to
// preview-shell.html and break prototype previews everywhere, because that page
// is ALWAYS framed in normal operation. The symptom would be a blank preview
// with no error, which is expensive to trace back to a security guard.
//
// Kept out of the `if (conf)` block above: this is a fact about the HTML file
// and holds whether or not the nginx config is present.
{
  const SHELL = join(HERE, '..', 'public', 'preview-shell.html');
  let shellHtml = null;
  try {
    shellHtml = readFileSync(SHELL, 'utf8');
  } catch {
    // Not present in this tree — the preview feature is in-flight. No failure.
  }
  if (shellHtml !== null) {
    const shellMarkup = shellHtml.replace(/<!--[\s\S]*?-->/g, '');
    check(
      !/self\s*!==\s*top|window\.self\s*!==\s*window\.top/.test(shellMarkup),
      'public/preview-shell.html contains a frame-busting self/top check. That '
      + 'page is DESIGNED to be framed by this application, so a frame buster '
      + 'there blanks every prototype preview. Its clickjacking control is the '
      + "frame-ancestors 'self' header asserted above, plus the embedder's "
      + 'sandbox attribute — not a buster.'
    );
  }
}

// ── report ──────────────────────────────────────────────────────────────────
for (const f of failures) console.log(`FAIL  ${f}`);
console.log(`\n${pass} checks passed, ${failures.length} failed`);
if (failures.length === 0) {
  console.log(
    'Clickjacking defence intact: headers at the edge, frame buster in the '
    + 'document for browsers that honour neither.'
  );
}
process.exit(failures.length === 0 ? 0 : 1);
