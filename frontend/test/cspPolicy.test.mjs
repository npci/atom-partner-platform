// Guard: the Content-Security-Policy stays strict, and stays in step with the
// markup it authorises.
//
// Zero-dependency, matching the other tests in this directory: this frontend
// has no test runner, and pulling one in purely for this would add SBOM
// components to a codebase under SCA scanning. Node's stdlib is enough.
//
//   Run with:  npm run test:cspPolicy
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// Checkmarx "Permissive Content Security Policy"
// (Python\Cx\PythonLowVisibility v2, path 26, scanid 1016302) reported
// frontend/index.html line 11:
//
//     <meta http-equiv="Content-Security-Policy" content="frame-ancestors 'none'" />
//
// Two things were wrong with it. It was permissive — one framing directive,
// nothing about script, object or base URIs. And it did not work at all:
// frame-ancestors is ignored in a <meta> tag (CSP L3 §3.3), verified in Chrome
// by successfully framing a page whose only protection was that tag.
//
// The fix moved the policy to the nginx response header and made it strict.
// This test defends the three ways that fix can silently rot:
//
//   1. Someone adds a CSP <meta> tag back to index.html — the exact line the
//      scanner flagged, and an easy thing to re-add "for defence in depth".
//   2. Someone weakens the header — most likely by adding 'unsafe-inline' to
//      script-src to make some inline snippet work, which would restore the
//      finding and remove the app's main XSS control.
//   3. The inline frame buster in index.html is edited without regenerating
//      the hash. This is the dangerous one: the CSP would block the script,
//      the script is what removes `html { display: none }`, and the app would
//      ship as a BLANK PAGE with no error in the UI. Caught here instead.

import { readFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  collectInlineHashes,
  buildPolicy,
  stripHtmlComments,
  sriHash,
} from '../scripts/generate-csp.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');
const REPO = join(ROOT, '..');
const SRC_INDEX = join(ROOT, 'index.html');
const DIST_INDEX = join(ROOT, 'dist', 'index.html');
const NGINX = join(REPO, 'deploy', 'edge.nginx.conf');
const SHELL = join(ROOT, 'public', 'preview-shell.html');

let pass = 0;
const failures = [];
const skips = [];

const ok = (n) => { pass += n === undefined ? 1 : n; };
const fail = (m) => failures.push(m);

// ── 1. No CSP <meta> tag in index.html ──────────────────────────────────────
// The literal finding. A CSP meta tag cannot deliver frame-ancestors, and a
// second policy would intersect with the header, silently tightening it in
// ways nobody tested.
{
  const raw = readFileSync(SRC_INDEX, 'utf8');
  const withoutComments = stripHtmlComments(raw);
  const metaCsp = /<meta[^>]+http-equiv\s*=\s*["']Content-Security-Policy["'][^>]*>/i;

  if (metaCsp.test(withoutComments)) {
    fail(
      'index.html contains a <meta http-equiv="Content-Security-Policy"> tag.\n'
      + '      This is the exact line Checkmarx flagged (Permissive Content\n'
      + '      Security Policy, path 26). frame-ancestors is IGNORED in a meta\n'
      + '      tag, so it protects nothing, and a second policy intersects with\n'
      + '      the nginx header. Define the policy only in deploy/edge.nginx.conf.'
    );
  } else {
    ok();
  }

  // The frame buster is the reason the CSP needs a hash at all. If it is
  // deleted, the clickjacking finding returns; if it is kept, the hash must
  // track it. Either way its presence is load-bearing for the checks below.
  if (!/id="antiClickjack"/.test(withoutComments)) {
    fail('index.html no longer contains the antiClickjack frame buster — the '
      + 'Checkmarx clickjacking query (a static check on this file) would '
      + 'reopen. If it was removed deliberately, update this test and the CSP.');
  } else {
    ok();
  }
}

// ── 2. The generated policy is strict ───────────────────────────────────────
// Asserted against the generator's own output, so the rules hold for whatever
// it produces on any future build — not just against a snapshot string.
{
  const policy = buildPolicy({ scripts: ['sha256-x'], styles: ['sha256-y'] });
  const directive = (name) => {
    const m = policy.match(new RegExp(`(?:^|; )${name} ([^;]*)`));
    return m ? m[1] : null;
  };

  const scriptSrc = directive('script-src');
  if (!scriptSrc) {
    fail('generated policy has no script-src — default-src would have to carry '
      + 'it, which is too blunt to be safe here.');
  } else if (scriptSrc.includes("'unsafe-inline'")) {
    fail(
      "generated policy allows 'unsafe-inline' in script-src.\n"
      + '      This is the control that stops XSS, and removing it re-creates the\n'
      + '      "permissive CSP" finding. If an inline script needs to run, add its\n'
      + '      sha256 hash via scripts/generate-csp.mjs instead.'
    );
  } else if (scriptSrc.includes("'unsafe-eval'")) {
    fail("generated policy allows 'unsafe-eval' in script-src.");
  } else {
    ok();
  }

  // Wildcards would make any of these vacuous.
  for (const name of ['default-src', 'script-src', 'connect-src', 'frame-src']) {
    const v = directive(name);
    if (v && /(^|\s)\*(\s|$)/.test(v)) {
      fail(`generated policy uses a bare * in ${name} — that grants any origin.`);
    } else {
      ok();
    }
  }

  // Directives with no sane fallback to default-src, or whose absence is a
  // known bypass. base-uri is the subtle one: without it, an injected <base>
  // rewrites every relative script URL to an attacker host, defeating
  // script-src 'self'.
  const REQUIRED = {
    "default-src 'self'": 'nothing is permitted unless explicitly granted',
    "object-src 'none'": 'blocks plugin/object embedding',
    "base-uri 'self'": "stops an injected <base> redirecting relative script URLs, which would defeat script-src 'self'",
    "form-action 'self'": 'stops forms POSTing credentials off-origin',
    "frame-ancestors 'none'": 'clickjacking — and this only works as a header',
  };
  for (const [needle, why] of Object.entries(REQUIRED)) {
    if (!policy.includes(needle)) {
      fail(`generated policy is missing "${needle}" (${why}).`);
    } else {
      ok();
    }
  }
}

// ── 3. The nginx header matches the built markup ────────────────────────────
// The check that prevents a silent blank-page deploy.
{
  if (!existsSync(NGINX)) {
    fail(`deploy/edge.nginx.conf not found at ${NGINX} — the CSP header lives `
      + 'there; if the file moved, update this test.');
  } else {
    const conf = readFileSync(NGINX, 'utf8');

    // Strip nginx comments so a directive named in the prose above the config
    // cannot satisfy a check the live config no longer meets. This exact
    // comment-vs-code bypass was found in noRawHtml.test.mjs and fixed there;
    // it is not repeated here.
    const live = conf.replace(/^\s*#.*$/gm, '');

    if (!/add_header\s+Content-Security-Policy/i.test(live)) {
      fail('edge.nginx.conf does not set a Content-Security-Policy header in '
        + 'active configuration.');
    } else {
      ok();
    }

    // The app policy must not carry 'unsafe-inline' in script-src. The
    // preview-shell location deliberately does, so isolate the app policy:
    // it is the one bound to $csp_app.
    const appPolicy = (live.match(/set\s+\$csp_app\s+"([^"]+)"/) || [])[1];
    if (!appPolicy) {
      fail('could not find the $csp_app policy in edge.nginx.conf — this test '
        + 'locates the application policy by that variable name.');
    } else {
      const scriptSrc = (appPolicy.match(/script-src ([^;]*)/) || [])[1] || '';
      if (scriptSrc.includes("'unsafe-inline'")) {
        fail("the nginx app policy allows 'unsafe-inline' in script-src — the "
          + 'permissive-CSP finding would return.');
      } else {
        ok();
      }
      if (!appPolicy.includes("frame-ancestors 'none'")) {
        fail("the nginx app policy no longer sets frame-ancestors 'none'.");
      } else {
        ok();
      }

      // THE HASH CHECK. Only meaningful against a build, so it is skipped
      // (loudly) when dist/ is absent — a developer running `npm test` without
      // building should not get a red suite, but CI must not silently skip it.
      if (!existsSync(DIST_INDEX)) {
        skips.push(
          'dist/index.html not built — skipped verifying the nginx CSP hash '
          + 'matches the inline frame buster. Run `npm run build` first; CI '
          + 'must build before testing or this check is vacuous.'
        );
      } else {
        const builtHtml = readFileSync(DIST_INDEX, 'utf8');
        const { scripts, styles } = collectInlineHashes(builtHtml);

        if (scripts.length === 0) {
          fail('no inline <script> found in dist/index.html — the frame buster '
            + 'should be there. If it was intentionally removed, the CSP hash '
            + 'should be removed too.');
        } else {
          ok();
        }

        for (const h of scripts) {
          if (!appPolicy.includes(h)) {
            fail(
              `the nginx CSP does not authorise the inline script in the built\n`
              + `      index.html (expected ${h}).\n`
              + `      CONSEQUENCE: the browser blocks the frame buster, whose\n`
              + `      companion <style> hides the document — the app renders as a\n`
              + `      BLANK PAGE with no error. Regenerate with \`npm run csp\` and\n`
              + `      paste the result into deploy/edge.nginx.conf.`
            );
          } else {
            ok();
          }
        }

        // style-src keeps 'unsafe-inline' for React's inline styles, so a
        // missing style hash is not fatal — but a WRONG one means the config
        // was hand-edited and is drifting.
        for (const h of styles) {
          if (!appPolicy.includes(h)) {
            skips.push(
              `the inline <style> hash ${h} is not in the nginx policy. Not `
              + `fatal — style-src keeps 'unsafe-inline' — but it means the `
              + `generated policy and the config have drifted. Re-run \`npm run csp\`.`
            );
          } else {
            ok();
          }
        }
      }
    }

    // The preview shell needs its own location block, or it inherits the strict
    // app policy and the prototypes silently stop working.
    if (!/location\s*=\s*\/a2a-partner\/preview-shell\.html/.test(live)) {
      fail('edge.nginx.conf has no exact-match location for '
        + '/a2a-partner/preview-shell.html — the prototype preview would inherit '
        + 'the strict app CSP and render as a dead mockup.');
    } else {
      ok();
      // nginx add_header does not merge: a location that sets any header drops
      // every inherited one. Forgetting nosniff here is a real regression.
      const block = (live.match(
        /location\s*=\s*\/a2a-partner\/preview-shell\.html\s*\{[\s\S]*?\n {4}\}/
      ) || [])[0] || '';
      for (const needle of ['X-Content-Type-Options', 'Content-Security-Policy']) {
        if (!block.includes(needle)) {
          fail(`the preview-shell location does not re-set ${needle}. nginx `
            + `add_header does NOT inherit once a location sets any header of `
            + `its own, so this response would ship without it.`);
        } else {
          ok();
        }
      }
      for (const needle of ["connect-src 'none'", "default-src 'none'", "form-action 'none'"]) {
        if (!block.includes(needle)) {
          fail(`the preview-shell CSP is missing ${needle} — the sandboxed `
            + `prototype would regain network egress.`);
        } else {
          ok();
        }
      }
    }
  }
}

// ── 4. The preview shell's own controls are intact ──────────────────────────
{
  if (!existsSync(SHELL)) {
    fail('public/preview-shell.html is missing — the prototype preview depends '
      + 'on it, and without it the iframe 404s.');
  } else {
    const shell = readFileSync(SHELL, 'utf8');
    const code = shell.replace(/<!--[\s\S]*?-->/g, '');

    // It must not become same-origin-trusting, and must verify who is talking
    // to it. Without the source check any window could drive the render.
    if (!/event\.source\s*!==\s*window\.parent/.test(code)) {
      fail('preview-shell.html no longer verifies event.source === window.parent '
        + 'before rendering — any window could inject markup into it.');
    } else {
      ok();
    }

    if (/allow-same-origin/.test(code)) {
      fail('preview-shell.html mentions allow-same-origin in live code — that '
        + 'would collapse the opaque-origin protection.');
    } else {
      ok();
    }
  }

  // Every place that embeds the shell must sandbox it without allow-same-origin.
  const preview = join(ROOT, 'src', 'components', 'PrototypePreview.jsx');
  if (!existsSync(preview)) {
    fail('src/components/PrototypePreview.jsx is missing — prototype previews '
      + 'are rendered through it.');
  } else {
    const raw = readFileSync(preview, 'utf8');
    // Strip comments before looking at anything. This file *documents* the
    // sandbox rule in prose, and line 24 literally contains the string
    // `sandbox="allow-scripts"`. A whole-file scan therefore reports success
    // even when the real attribute has been weakened — exactly the false pass
    // that showed up while adversarially testing this suite. Assertions below
    // run against code only, and read the attribute value itself rather than
    // searching the file for a substring.
    const code = raw
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '');

    const sandboxAttrs = [...code.matchAll(/sandbox\s*=\s*"([^"]*)"/g)].map((m) => m[1]);
    if (sandboxAttrs.length === 0) {
      fail('PrototypePreview.jsx sets no sandbox attribute on the preview '
        + 'iframe — the prototype would run with full same-origin privileges.');
    } else if (!sandboxAttrs.every((v) => v.trim() === 'allow-scripts')) {
      fail('PrototypePreview.jsx sandbox attribute is not exactly '
        + `"allow-scripts" (found: ${sandboxAttrs.map((v) => `"${v}"`).join(', ')}). `
        + 'Any extra token widens the cage the prototype runs in.');
    } else {
      ok();
    }

    if (sandboxAttrs.some((v) => /\ballow-same-origin\b/.test(v))
      || /allow-same-origin/.test(code)) {
      fail('PrototypePreview.jsx grants allow-same-origin — the prototype could '
        + 'then read the parent DOM, cookies and the session token.');
    } else {
      ok();
    }
    // hardenFrameHtml stays as defence in depth: the shell's header is the
    // primary cage, but the markup should carry its own policy too.
    if (!/hardenFrameHtml/.test(code)) {
      fail('PrototypePreview.jsx no longer applies hardenFrameHtml() to the '
        + 'markup before rendering it.');
    } else {
      ok();
    }
  }
}

// ── 5. The generator's comment-stripping actually works ─────────────────────
// Regression test for a real bug hit while building this: index.html contains
// an HTML comment that mentions <script>, so matching the raw file captured
// from inside the comment and produced a hash the browser rejected. The
// symptom (blocked script, blank page) pointed nowhere near the cause.
{
  const html = [
    '<html><head>',
    '<!-- a comment that mentions <script>fake()</script> for documentation -->',
    '<style id="antiClickjack">html{display:none}</style>',
    '<script>real()</script>',
    '<script type="module" src="/assets/app.js"></script>',
    '</head></html>',
  ].join('\n');

  const { scripts, styles } = collectInlineHashes(html);

  if (scripts.length !== 1) {
    fail(`comment-stripping regression: expected exactly 1 inline script hash, `
      + `got ${scripts.length}. A <script> inside an HTML comment is being `
      + `hashed, or the external module tag is.`);
  } else if (scripts[0] !== sriHash('real()')) {
    fail('comment-stripping regression: the hash does not correspond to the '
      + 'real inline script. It is almost certainly capturing text from inside '
      + 'the HTML comment.');
  } else {
    ok();
  }

  if (styles.length !== 1 || styles[0] !== sriHash('html{display:none}')) {
    fail('inline <style> hashing is wrong.');
  } else {
    ok();
  }
}

// ── 6. The build strips index.html comments but keeps the defences ──────────
// index.html explains the clickjacking layers, the absent CSP meta tag and the
// Checkmarx finding IDs behind each choice. Useful to a maintainer, but handing
// the public a map of the app's security controls is Checkmarx's own
// "Information Exposure Through an HTML Comment". vite.config.js strips them at
// build time.
//
// The risk in that plugin is over-reach: it must remove the prose and nothing
// else. If it ever swallowed the antiClickjack <style> or the frame buster, the
// clickjacking finding reopens, and if it altered the inline bodies the hashes
// would stop matching and the app would ship blank. So this asserts both
// directions — comments gone, defences intact — against the real build output.
{
  if (!existsSync(DIST_INDEX)) {
    skips.push('dist/index.html not built — run `npm run build` to check that '
      + 'the production HTML is comment-free. Skipping, not failing, so the '
      + 'suite still runs on a clean checkout.');
  } else {
    const dist = readFileSync(DIST_INDEX, 'utf8');

    if (/<!--/.test(dist)) {
      fail('dist/index.html still contains HTML comments. index.html documents '
        + 'the security controls and the Checkmarx finding IDs; shipping that '
        + 'to browsers is Information Exposure Through an HTML Comment. Check '
        + 'the strip-html-comments plugin in vite.config.js is still applied.');
    } else {
      ok();
    }

    // Belt and braces: even if the comment syntax changed, these strings must
    // never reach a browser.
    const leaks = ['Checkmarx', 'scanid', 'PythonLowVisibility'];
    const found = leaks.filter((s) => dist.includes(s));
    if (found.length > 0) {
      fail(`dist/index.html leaks internal security detail (${found.join(', ')}) `
        + 'to every visitor.');
    } else {
      ok();
    }

    // Now the other direction: stripping must not have eaten the defences.
    if (!/id="antiClickjack"/.test(dist)) {
      fail('dist/index.html has no antiClickjack <style> — the comment stripper '
        + 'removed the first clickjacking layer, so a framed page would render '
        + 'and be clickable.');
    } else {
      ok();
    }

    if (!/self\s*!==\s*top|self\s*===\s*top/.test(dist)) {
      fail('dist/index.html has no frame-buster check — the comment stripper '
        + 'removed the script that reveals the UI, which would ship the app as '
        + 'a permanently blank page.');
    } else {
      ok();
    }
  }

  // preview-shell.html needs the same treatment and is easy to forget: files in
  // public/ bypass Vite's HTML pipeline and are copied byte-for-byte, so the
  // transformIndexHtml hook never sees them. The plugin handles them in a
  // separate closeBundle pass; this makes sure that pass keeps running.
  const distShell = join(ROOT, 'dist', 'preview-shell.html');
  if (!existsSync(distShell)) {
    skips.push('dist/preview-shell.html not built — skipping the comment check '
      + 'for the sandbox host page.');
  } else {
    const shell = readFileSync(distShell, 'utf8');

    if (/<!--/.test(shell)) {
      fail('dist/preview-shell.html still contains HTML comments. The source '
        + 'documents the sandbox arrangement and the controls caging untrusted '
        + 'prototype markup — a roadmap for anyone probing it. Files in public/ '
        + 'skip transformIndexHtml, so check the closeBundle pass in '
        + 'vite.config.js still rewrites them.');
    } else {
      ok();
    }

    // And it must still work: the handshake and the render path are what make
    // the preview appear at all.
    if (!/preview-shell-ready/.test(shell) || !/document\.write/.test(shell)) {
      fail('dist/preview-shell.html lost its postMessage handshake or render '
        + 'call — prototype previews would come up blank. The comment stripper '
        + 'is over-reaching into the inline <script>.');
    } else {
      ok();
    }
  }
}

// ── report ──────────────────────────────────────────────────────────────────
for (const s of skips) console.log(`SKIP  ${s}`);
for (const f of failures) console.log(`FAIL  ${f}`);
console.log(`\n${pass} checks passed, ${failures.length} failed, ${skips.length} skipped`);
if (failures.length === 0) {
  console.log(
    'CSP is strict, delivered as a header, and in step with the inline markup '
    + 'it authorises.'
  );
}
process.exit(failures.length === 0 ? 0 : 1);
