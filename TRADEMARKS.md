# Trademarks

The MIT License covers **code**. It grants copyright permissions and nothing
else — unlike Apache-2.0, which this project previously used, it contains no
trademark clause at all. That is not a loosening: a licence that says nothing
about trademarks conveys no trademark rights, so names, logos and marks remain
reserved under ordinary trademark law rather than under the licence.

The practical consequence is that this document, not the LICENSE file, is now
the only place the trademark position is stated. Read it as binding.

## Marks not licensed here

"NPCI", "UPI", "BHIM", "RuPay" and related marks are trademarks of the National
Payments Corporation of India. Marks belonging to banks, payment providers and
other third parties are the property of their respective owners — and NPCI
could not sublicense those to you even if it wished to.

## No logo is bundled

**No mark of any third party is bundled in this repository.** Branding is
supplied by whoever deploys the software:

| Where | How |
|---|---|
| Application logo | `VITE_BRAND_LOGO_URL` — a path served by the frontend, e.g. `/brand-logo.png` |
| Application name | `VITE_BRAND_NAME` — defaults to "Partner Platform" |
| Domain wording | `VITE_LABEL_OVERRIDES` / `LABEL_PROFILE` — see `frontend/src/strings.js` |

With none set, the interface renders a wordmark as text and uses neutral
labels — "the Authority" rather than any named organisation. Nothing breaks.

A previous revision of the frontend bundled a bank's logo. It was removed
precisely because this project has no right to sublicense it to forks. Do not
reintroduce a third-party image asset into this repository; point the
environment variable at one you are entitled to use.

## Naming inside the code and defaults

Two categories of name appear in this repository, and the distinction matters:

- **Wire-contract values** — values a counterparty matches byte-for-byte.
  Renaming one of these in a single repository desynchronises the wire. They are
  technical values, not branding, and they are not user-visible. The real list
  is short:
  - the `Direction` enum's VALUES, `"npci_to_bank"` / `"bank_to_npci"`
    (`app/a2a_common/protocol.py`) — the members are named
    `AUTHORITY_TO_PARTNER` / `PARTNER_TO_AUTHORITY`; only the strings are pinned;
  - `npci_change_id` in the certification RIG's exchange
    (`adcn-operator-app/cert-rig`), a separately deployed artifact. This
    platform now emits and accepts both spellings until the rig is updated;
  - the `NPCI_*` environment-variable names, still accepted as aliases because
    they live in operators' compose files and `.env`s.

  An EARLIER VERSION of this section also listed `npci_change_id` as a column,
  `npci_counter_open`, and the module `app/npci_client.py` as wire contract.
  That was wrong, and checking cost one `git grep`: the Authority platform uses
  neither identifier anywhere, `npci_counter_open` is a frontend event kind
  local to a single component, and a module name cannot cross a wire at all.
  All three have been renamed (`authority_change_id`,
  `authority_counter_open`, `app/authority_client.py`) and the certification
  round re-run end-to-end unchanged to prove it.
- **User-visible copy** — held in `frontend/src/strings.js` with neutral
  defaults. `frontend/labels.upi.json` is an **example** override profile that
  fills those labels in with NPCI/UPI wording for a deployment operating in that
  ecosystem. It is a sample input, not baked-in branding, and is deliberately
  left as-is: it is the demonstration that the vocabulary is supplied by the
  deployment rather than compiled in.

If you fork this for a different authority, override the labels. You do not need
to rename the wire VALUES above, and you should not.

## The example partner profiles

`data/examples/` carries two worked examples of the partner profile:

- `example_bank_profile.md` — "Meridian Commercial Bank"
- `example_library_profile.md` — "Anna Memorial Library"

Both organisations are FICTIONAL, as are every vendor, figure and date in them.
They name no real institution and make no statement about any real
institution's capabilities.

An earlier revision of this repository shipped a worked example that named a
real bank and relied on a nominative-use rationale. That file was replaced by
the fictional Meridian profile, so no nominative-use defence is needed for
anything under `data/` today.

`data/partner_profile.template.md` names no one and is the starting point for
your own profile.

## What you may and may not do

You **may** state that your product is built with, based on, or compatible with
this software. Forking and adapting it is the intended use — that is what
reference base code is for.

You **may not** use NPCI's or any third party's name, logo or marks in a way
that suggests endorsement, affiliation, or that your fork is an official
distribution. In particular, do not present your fork as an officially certified
or Authority-endorsed partner integration. **If you fork it, name it something
of your own.**

Note that certification artefacts generated by this platform default to a
neutral `Certification` filename prefix, deliberately: a fork must not emit
files named after someone else's organisation.

## Questions

Trademark use: **`atom.support@npci.org.in`**, subject line
`[TRADEMARK]`.

The maintainers will route these to the copyright holder's legal team; this
document is not itself legal advice.
