// Guard: no loop in the frontend takes its iteration bound from untrusted data.
//
// Zero-dependency by design, matching test/noRawHtml.test.mjs and
// test/safeUrl.test.mjs: this frontend has no test runner, and adding
// eslint purely for one rule would pull ~100 packages into a codebase that is
// itself under SCA/SBOM scanning.
//
//   Run with:  npm run test:boundedLoops
//
// ── WHY THIS EXISTS ─────────────────────────────────────────────────────────
//
// Checkmarx `UncheckedInputForLoopCondition` (JavaScriptMediumThreat, v3)
// reported:
//
//     getChange (services/api.js:226) reads user input from `data`. That value
//     flows through the code without being validated and is eventually used in
//     a loop condition in _detailField (pages/ChangeDetail.jsx:134).
//
// The finding is about ITERATION COUNT, not content. A cert-test-cases document
// arrives over A2A, the component splits it into lines, and the line count — a
// number chosen by whoever produced the document — decided how many times the
// loop ran. That is unbounded work on the render thread: a large or malformed
// payload freezes the tab (OWASP A4 Insecure Design / API4 Unrestricted
// Resource Consumption).
//
// ── WHY THE OBVIOUS FIX DID NOT CLEAR THE SCAN ──────────────────────────────
//
// The code ALREADY had a cap when this was reported:
//
//     const limit = Math.min(lines.length, MAX_DETAIL_LINES);
//     for (let i = 0; i < limit; i += 1)
//
// The trip count was genuinely bounded, and it was still reported — correctly,
// by the rules of the query. `limit` is derived from `lines.length`, which is
// derived from the API response, so the loop condition still READS a tainted
// value. `Math.min` is not a sanitiser in Checkmarx's model; it is ordinary
// value propagation, so the source→sink edge survived it. This is the same
// lesson already documented at the top of src/utils/safeUrl.js: a taint engine
// tracks where bytes came from, and "this value was compared against a
// constant" is not something it can recognise.
//
// The shape that actually clears it inverts the two:
//
//     for (let i = 0; i < MAX_DETAIL_LINES; i += 1) {
//       if (i >= lines.length) break;
//
// The loop condition now reads only a build-time constant, and the tainted
// length appears in a `break` guard in the body. Runtime behaviour is
// identical. The difference is that the bound is now provable by inspection —
// to the scanner and to a human reviewer — instead of requiring you to trace
// where `limit` came from.
//
// This test enforces that shape so the finding cannot return by drift: the next
// person to write a `for (let i = 0; i < items.length; i++)` over server data
// gets a failing build instead of a Checkmarx ticket months later.

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, relative } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..', 'src');

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (/\.(jsx?|mjs|tsx?)$/.test(entry)) out.push(full);
  }
  return out;
}

// Strip comments so a pattern discussed in prose is not reported as code.
// Newlines are PRESERVED so reported line numbers still match the real file —
// the same approach, and for the same reason, as noRawHtml.test.mjs.
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ''))
    .replace(/^[ \t]*\/\/.*$/gm, '');
}

// Extract the header of every `for (...)` in a file by scanning for the
// matching close paren, rather than regex-matching up to the first `;`. A naive
// `for\s*\(([^;]*);([^;]*);` runs straight past the header of a `for...of` loop
// (which contains no semicolons) and into the statements of its body, then
// reports the body text as an unbounded condition. That produced five false
// positives on the first run of this guard.
function forHeaders(code) {
  const out = [];
  for (const m of code.matchAll(/\bfor\s*\(/g)) {
    const open = m.index + m[0].length - 1;
    let depth = 0;
    let close = -1;
    for (let i = open; i < code.length; i += 1) {
      const c = code[i];
      if (c === '(') depth += 1;
      else if (c === ')') {
        depth -= 1;
        if (depth === 0) { close = i; break; }
      }
    }
    if (close < 0) continue;   // unbalanced — not parseable, skip
    out.push({
      header: code.slice(open + 1, close),
      line: code.slice(0, m.index).split('\n').length,
    });
  }
  return out;
}

// Split a for-header on its TOP-LEVEL semicolons only, so a `;` inside a nested
// call or string does not create a phantom clause.
function topLevelClauses(header) {
  const parts = [];
  let depth = 0;
  let cur = '';
  for (const c of header) {
    if (c === '(' || c === '[' || c === '{') depth += 1;
    else if (c === ')' || c === ']' || c === '}') depth -= 1;
    if (c === ';' && depth === 0) { parts.push(cur); cur = ''; continue; }
    cur += c;
  }
  parts.push(cur);
  return parts;
}

let pass = 0;
const failures = [];

const files = walk(SRC);
if (files.length < 10) {
  failures.push(`Only ${files.length} source files scanned — the scan target `
    + `may have moved. A guard that examines nothing passes vacuously.`);
}

// ── 1. No loop condition may read a length, a call, or a lowercase variable ──
//
// A conforming condition compares the counter against a numeric literal or an
// UPPER_SNAKE_CASE constant. Anything else is either tainted or unprovable
// without tracing, which is exactly the state that produced the finding.
const OK_BOUND = /^[A-Z][A-Z0-9_]*$|^\d+$/;

for (const file of files) {
  const rel = relative(join(HERE, '..'), file).replace(/\\/g, '/');
  const code = stripComments(readFileSync(file, 'utf8'));

  for (const { header, line } of forHeaders(code)) {
    const clauses = topLevelClauses(header);

    // `for...of` / `for...in` have a single clause and no counter. They iterate
    // an in-memory collection that has ALREADY been fully materialised — the
    // allocation, not the loop, is the bound — and their header contains no
    // condition for a taint engine to flag. The reported finding was a counted
    // loop, and that is what this check governs.
    if (clauses.length < 3) { pass += 1; continue; }

    const condition = clauses[1].trim();

    // An empty condition (`for (;;)`) is an infinite loop — never acceptable
    // here, and not something a `break` guard makes provable by inspection.
    if (condition === '') {
      failures.push(`${rel}:${line} is an unconditional \`for (;;)\` loop. `
        + `Give it an explicit constant bound.`);
      continue;
    }

    // Descending loops (`i >= 0`, `i > 0`) terminate at a constant. Their START
    // may read a length, but the condition itself carries no tainted value and
    // the count is bounded by the array that already exists in memory.
    if (/^[A-Za-z_$][\w$]*\s*>=?\s*(0|\d+)$/.test(condition)) { pass += 1; continue; }

    const cmp = condition.match(/^[A-Za-z_$][\w$]*\s*(?:<|<=|!==|!=)\s*(.+)$/);
    if (!cmp) {
      failures.push(`${rel}:${line} has a for-loop condition this guard cannot `
        + `prove bounded: "${condition}". Rewrite it as `
        + `\`i < SOME_CONSTANT\` with an \`if (i >= data.length) break;\` guard.`);
      continue;
    }

    const bound = cmp[1].trim();
    if (OK_BOUND.test(bound)) { pass += 1; continue; }

    failures.push(`${rel}:${line} loops until "${bound}", which is not a `
      + `build-time constant.\n`
      + `      why banned: Checkmarx UncheckedInputForLoopCondition reads the `
      + `loop CONDITION. A bound derived from a server response — even via `
      + `Math.min(x.length, MAX) — keeps the source→sink path alive and will be `
      + `re-reported.\n`
      + `      fix: for (let i = 0; i < MAX_THING; i += 1) { if (i >= x.length) break; ... }`);
  }

  // `while` loops are the same hazard with different syntax. None exist today;
  // this keeps it that way.
  for (const m of code.matchAll(/\bwhile\s*\(([^)]*)\)/g)) {
    const condition = m[1].trim();
    const line = code.slice(0, m.index).split('\n').length;
    if (/\.length\b/.test(condition)) {
      failures.push(`${rel}:${line} has a while-loop whose condition reads `
        + `.length ("${condition}"). Convert it to a counted loop bounded by a `
        + `constant, with the length as a break guard.`);
    } else {
      pass += 1;
    }
  }
}

// ── 2. The specific caps that close the reported paths must remain ──────────
//
// Check 1 is structural — it would still pass if someone raised a cap to
// Number.MAX_SAFE_INTEGER, which is a constant but not a bound. These assert
// that each cap exists AND is a sane size for the data it describes.
const REQUIRED_CAPS = [
  {
    file: 'pages/ChangeDetail.jsx',
    name: 'MAX_DETAIL_LINES',
    max: 5000,
    why: 'Bounds the DETAILS-block line scan in _detailField — the exact sink '
       + 'named in the Checkmarx finding (ChangeDetail.jsx:134, source '
       + 'services/api.js getChange).',
  },
  {
    file: 'pages/ChangeDetail.jsx',
    name: 'MAX_TEST_CASES',
    max: 5000,
    why: 'Bounds parseCertTestCasesMd over the same A2A markdown. Each case '
       + 'runs eight regexes plus seven _detailField scans, so this is the '
       + 'multiplier on the sink above.',
  },
  {
    file: 'utils/safeUrl.js',
    name: 'MAX_URL_LENGTH',
    max: 65536,
    why: 'Bounds the per-character rebuild of a server-supplied mr_url / '
       + 'artifact_ref in rebuildTail.',
  },
  {
    file: 'pages/Settings.jsx',
    name: 'MAX_FRONTMATTER_LINES',
    max: 5000,
    why: 'Bounds the frontmatter parse of an operator-uploaded profile .md.',
  },
];

for (const { file, name, max, why } of REQUIRED_CAPS) {
  let code;
  try {
    code = stripComments(readFileSync(join(SRC, file), 'utf8'));
  } catch {
    failures.push(`${file} is missing — it holds the ${name} cap.`);
    continue;
  }

  const decl = code.match(new RegExp(`const\\s+${name}\\s*=\\s*(\\d+)`));
  if (!decl) {
    failures.push(`${file} no longer declares ${name} as a numeric constant.\n`
      + `      why it matters: ${why}`);
    continue;
  }

  const value = Number(decl[1]);
  if (!Number.isInteger(value) || value <= 0 || value > max) {
    failures.push(`${file} sets ${name} = ${value}, outside the sane range `
      + `1..${max}. A cap that cannot be reached by legitimate data is not a `
      + `cap.\n      why it matters: ${why}`);
  } else {
    pass += 1;
  }

  // The cap only does anything if it is what the loop actually tests against.
  if (!new RegExp(`<\\s*${name}\\b`).test(code)) {
    failures.push(`${file} declares ${name} but no loop condition compares `
      + `against it. The constant is dead and the loop is bounded by something `
      + `else.`);
  } else {
    pass += 1;
  }
}

// ── 3. Math.min(...) must not be used to derive a loop bound ────────────────
//
// This is the specific pattern that was already in the code when Checkmarx
// filed the report. It looks like a fix and is not one, so it is worth naming
// explicitly rather than letting it fail check 1 with a generic message.
//
// TWO forms, because the reported code used the second one:
//
//   (a) for (let i = 0; i < Math.min(x.length, MAX); i++)   — inline
//   (b) const limit = Math.min(x.length, MAX);              — hoisted
//       for (let i = 0; i < limit; i++)
//
// Check 1 already fails (b) — `limit` is not UPPER_SNAKE_CASE — but with a
// generic "not a build-time constant" message that does not tell the reader
// they have just rewritten the original finding. Detecting the hoisted form by
// name gives them the real explanation.
for (const file of files) {
  const rel = relative(join(HERE, '..'), file).replace(/\\/g, '/');
  const code = stripComments(readFileSync(file, 'utf8'));

  const WHY = `      why banned: this is the exact code that was reported. `
    + `Math.min is value propagation, not sanitisation — the tainted length `
    + `still reaches the condition. Put the constant in the condition and the `
    + `length in a break guard.`;

  // (a) inline in the for-header.
  const inline = code.match(/for\s*\([^;]*;[^;]*Math\.min\s*\(/);
  if (inline) {
    const line = code.slice(0, inline.index).split('\n').length;
    failures.push(`${rel}:${line} uses Math.min() inside a loop condition.\n${WHY}`);
  } else {
    pass += 1;
  }

  // (b) hoisted into a variable that a loop condition then reads.
  for (const m of code.matchAll(
    /(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*Math\.min\s*\(/g
  )) {
    const name = m[1];
    if (!new RegExp(`for\\s*\\([^;]*;[^;]*\\b${name}\\b`).test(code)) continue;
    const line = code.slice(0, m.index).split('\n').length;
    failures.push(`${rel}:${line} assigns Math.min() to \`${name}\`, which a `
      + `loop condition then reads.\n${WHY}`);
  }
  pass += 1;
}

// ── report ──────────────────────────────────────────────────────────────────
for (const f of failures) console.log(`FAIL  ${f}`);
console.log(`\n${pass} checks passed, ${failures.length} failed`);
if (failures.length === 0) {
  console.log(
    'All loops are bounded by build-time constants. Checkmarx '
    + 'UncheckedInputForLoopCondition (ChangeDetail.jsx _detailField) stays closed.'
  );
}
process.exit(failures.length === 0 ? 0 : 1);
