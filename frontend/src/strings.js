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
// API fields (authority_counter_open, authority_change_id) are NOT here — they are wire
// contract with the backend and renaming them in the UI alone would desync it.

const DEFAULTS = {
  'term.authority':    'the Authority',
  'term.authorityCap': 'The Authority',
  'term.authorityOrg': 'Change Management',
  // Short badge label for rows the PARTNER side initiated (cert results
  // "Initiated by" column). Display only — the wire value the backend sends
  // for these rows is still the literal 'BANK' and comparisons keep using it.
  'term.partnerShort': 'PARTNER',
  'ph.repo.name':      'e.g. Core Platform',
  'ph.kb.title':       'Title (e.g. 2.0 Technical Spec)',
  'cert.reportTitle':  'Certification Report',
  // Prefix for downloaded certification artefacts. A fork must not emit
  // files named after someone else's organisation.
  'cert.filePrefix':   'Certification',

  // ── Form placeholders ───────────────────────────────────────────────────
  // Placeholders are easy to overlook because they are not "copy" — but they
  // are the first thing an operator reads on the first-run screen, and they
  // are the strongest hint about what kind of deployment this is. All three of
  // these previously named a specific payment network, one of its member banks,
  // and that domain's role vocabulary.
  'ph.authorityUrl':   'https://authority.example.com',
  'ph.repo.project':   'my-org/my-service',
  // Mirrors data/partner_profile.template.md's opening. Kept short — it is a
  // hint at the shape, not a substitute for the template.
  'ph.profile.stub':   '---\npartner: Your Organisation\nprofile_version: 1.0\n---\n\n# Partner Profile\n\n## Quick reference\n- Roles played: ...',
  // Sample question in the "ask the authority" textarea. Previously named a
  // specific regional holiday; the neutral default sticks to a change-freeze
  // framing any deployment recognises.
  'ph.queryExample':   'e.g. Do the certification windows in section 4.1 include the year-end change freeze, or is that excluded?',
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
