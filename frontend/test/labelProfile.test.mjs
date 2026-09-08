// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT
//
// The label catalogue and its UPI override profile must stay in lockstep.
//
// `src/strings.js` holds neutral defaults; `labels.upi.json` is the override
// profile a UPI deployment ships via LABEL_PROFILE / VITE_LABEL_OVERRIDES. A key
// added to the defaults but not to the profile does not fail anything at build
// or at runtime — `t()` silently falls back to the neutral English default, so
// the UPI deployment quietly starts rendering "the Authority" where it should
// say "NPCI". That is the exact failure this file exists to catch: a label
// change that looks complete because nothing went red.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');

const stringsSrc = fs.readFileSync(path.join(root, 'src/strings.js'), 'utf8');
const labelsRaw = fs.readFileSync(path.join(root, 'labels.upi.json'), 'utf8');

// ── labels.upi.json must be valid JSON ──────────────────────────────────────
// It is read by JSON.parse at runtime via VITE_LABEL_OVERRIDES; a malformed
// file degrades to "console.warn and use defaults", which is silent in prod.
let labels;
assert.doesNotThrow(() => { labels = JSON.parse(labelsRaw); },
  'labels.upi.json is not valid JSON');

// ── Extract the DEFAULTS keys from strings.js ───────────────────────────────
// Parsed from source rather than imported: strings.js reads import.meta.env,
// which is a Vite construct that plain node cannot evaluate.
const defaultsBlock = stringsSrc.match(/const DEFAULTS = \{([\s\S]*?)\n\}/);
assert.ok(defaultsBlock, 'could not locate the DEFAULTS block in strings.js');
const keys = [...defaultsBlock[1].matchAll(/^\s*'([\w.]+)':/gm)].map((m) => m[1]);

assert.ok(keys.length >= 10,
  `expected the label catalogue to have grown; found only ${keys.length} keys`);

// ── Parity in both directions ───────────────────────────────────────────────
const missing = keys.filter((k) => !(k in labels));
assert.deepEqual(missing, [],
  `labels.upi.json is missing keys that exist in DEFAULTS: ${missing.join(', ')}. ` +
  'A UPI deployment would silently render the neutral default for these.');

const extra = Object.keys(labels).filter((k) => !keys.includes(k));
assert.deepEqual(extra, [],
  `labels.upi.json overrides keys that no longer exist in DEFAULTS: ${extra.join(', ')}. ` +
  'These are dead entries — the key was renamed or removed.');

// ── The defaults must actually be neutral ───────────────────────────────────
// The whole point of the split is that domain vocabulary lives in the profile,
// not in the code. A domain term reaching DEFAULTS means an unconfigured
// deployment renders it.
const domainTerms = ['NPCI', 'UPI', 'VPA', 'IFSC', 'PSP', 'TPAP', 'npci', 'upi'];
for (const [key, value] of Object.entries(
  Object.fromEntries(keys.map((k) => {
    const m = defaultsBlock[1].match(new RegExp(`'${k.replace('.', '\\.')}':\\s*([\\s\\S]*?),\\n`));
    return [k, m ? m[1] : ''];
  })),
)) {
  for (const term of domainTerms) {
    assert.ok(!value.includes(term),
      `DEFAULTS['${key}'] contains the domain term "${term}" — neutral defaults ` +
      'must not name a domain; put it in labels.upi.json instead.');
  }
}

// ── And the UPI profile must actually say something domain-specific ─────────
// If it had drifted to neutral wording, every check above would still pass
// while the override profile had quietly stopped doing anything.
const upiText = Object.values(labels).join(' ');
assert.ok(/NPCI|UPI/.test(upiText),
  'labels.upi.json no longer contains any UPI/NPCI wording — the override ' +
  'profile has stopped overriding anything.');

console.log(`labelProfile: ok (${keys.length} keys, parity with labels.upi.json)`);
