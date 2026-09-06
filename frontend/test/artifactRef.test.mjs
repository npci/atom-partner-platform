// Guard: `artifact_ref` must never be rendered into an href.
//
// Zero-dependency by design, matching test/safeUrl.test.mjs and
// test/noRawHtml.test.mjs — no test runner, no JSX transform. This is a source
// assertion, not a render test: it reads the JSX as text and checks the shape
// of the code, which is exactly the level a taint scanner reasons at.
//
//   Run with:  npm run test:artifactRef
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// Checkmarx reported ChangeDetail.jsx:2218 under THREE queries — Client DOM
// XSS, Reflected XSS, and Client DOM Open Redirect — all following the same
// dataflow: `getChange` in api.js returns partner-supplied data, and
// `artifact_ref` from that response reached an <a href>.
//
// `artifact_ref` arrives over A2A with no schema. In practice it is an opaque
// identifier, not a URL — our own backend fixture uses `doc://patch.pdf`, and
// `s3://` and bare `CR-1024/evidence.pdf` shapes also occur. Because none of
// those are http(s), the old `safeHref()` wrapper returned '#' for essentially
// every real value: the link was dead AND still flagged.
//
// The fix removes the sink instead of filtering it. Rendering the value as
// text means there is no `data` -> `href` edge left for any of the three
// queries to follow. If someone later "restores" the link, all three findings
// come back — so this test fails loudly instead.

import { readFile } from 'node:fs/promises';

const files = {
  changeDetail: new URL('../src/pages/ChangeDetail.jsx', import.meta.url),
  artifactRef:  new URL('../src/components/ArtifactRef.jsx', import.meta.url),
};

let pass = 0, fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass += 1; console.log(`PASS  ${name}`); }
  else    { fail += 1; console.log(`FAIL  ${name}${detail ? `\n      ${detail}` : ''}`); }
};

const changeDetail = await readFile(files.changeDetail, 'utf8');
const artifactRef  = await readFile(files.artifactRef, 'utf8');

// Strip block comments so prose mentioning `href` does not trip the scan.
const stripComments = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const cdCode = stripComments(changeDetail);
const arCode = stripComments(artifactRef);

// 1. No artifact reference may appear on the same JSX element as an href.
for (const [label, code] of [['ChangeDetail.jsx', cdCode], ['ArtifactRef.jsx', arCode]]) {
  const offenders = [];
  for (const m of code.matchAll(/<a\b[^>]*>/g)) {
    if (/artifact/i.test(m[0])) offenders.push(m[0].slice(0, 120));
  }
  check(`${label}: no <a> tag carries an artifact reference`,
    offenders.length === 0, offenders.join('\n      '));
}

// 2. The component itself must contain no href sink at all.
check('ArtifactRef.jsx: contains no href attribute',
  !/\bhref\s*=/.test(arCode));

// 3. ...and no raw-HTML escape hatch, which would reintroduce XSS by another route.
check('ArtifactRef.jsx: contains no dangerouslySetInnerHTML',
  !/dangerouslySetInnerHTML/.test(arCode));

// 4. Both render sites must go through the component.
const usages = [...cdCode.matchAll(/<ArtifactRef\b/g)].length;
check('ChangeDetail.jsx: both artifact sites use <ArtifactRef>', usages === 2,
  `found ${usages}, expected 2`);

// 5. The value must reach the component as a prop, not be re-wrapped in safeHref.
//    safeHref returning '#' for `doc://...` is what made these links dead.
check('ChangeDetail.jsx: artifact values are not passed through safeHref',
  !/safeHref\s*\(\s*[^)]*artifact/i.test(cdCode));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail === 0 ? 0 : 1);
