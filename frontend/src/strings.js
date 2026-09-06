// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// UI label catalogue — neutral defaults in code, domain wording supplied by the
// deployment. Mirrors the authority-side frontend's `src/strings.js`; see it for
// the full rationale.
//
// The dominant term on this side is the AUTHORITY the partner is dealing with.
// It appears throughout user-visible copy ("Awaiting response from …",
// "frozen by …"), so it is a label, not an identifier.
//
// Two keys rather than one, because English needs them: `term.authority` is the
// mid-sentence form and `term.authorityCap` the sentence-initial one. A single
// key defaulting to "the Authority" would render "the Authority has proposed…"
// at the start of a sentence. Both collapse to the same value for a deployment
// whose authority has a proper name.
//
// API fields (npci_counter_open, npci_change_id) are NOT here — they are wire
// contract with the backend and renaming them in the UI alone would desync it.

const DEFAULTS = {
  'term.authority':    'the Authority',
  'term.authorityCap': 'The Authority',
  'term.authorityOrg': 'Change Management',
  'ph.repo.name':      'e.g. Core Platform',
  'ph.kb.title':       'Title (e.g. 2.0 Technical Spec)',
  'cert.reportTitle':  'Certification Report',
  // Prefix for downloaded certification artefacts. A fork must not emit
  // files named after someone else's organisation.
  'cert.filePrefix':   'Certification',
}

let overrides = {}
try {
  overrides = JSON.parse(import.meta.env.VITE_LABEL_OVERRIDES || '{}')
} catch (err) {
  console.warn('VITE_LABEL_OVERRIDES is not valid JSON; using default labels', err)
  overrides = {}
}

/** Look up a UI label. Unknown keys return the key, so typos are visible. */
export function t(key) {
  return overrides[key] ?? DEFAULTS[key] ?? key
}
