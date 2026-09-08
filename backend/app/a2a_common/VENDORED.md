# Vendored a2a-core files — provenance record

Five files in this package carry a `>>> a2a-core vendored header >>>` block
declaring them GENERATED and pointing at `packages/a2a-core/` in the Authority
repository, to be re-synced with `scripts/ci/sync-a2a-core.sh`.

**None of those three things exist in this repository.** There is no
`packages/` directory, no `scripts/ci/sync-a2a-core.sh`, and no
`tests/a2a_common/test_protocol_contract.py` (which `protocol.py`'s own
docstring cites as its drift test).

The practical consequence this file exists to address: without a recorded
upstream coordinate, **you cannot determine from this repository whether any
vendored file still matches upstream** — for code that signs and verifies the
A2A wire.

## What is recorded here

`tests/test_vendored_a2a_core.py` pins the SHA-256 of each vendored file. The
hashes below are the CURRENT content of this repository's copies — they are a
**drift tripwire, not an upstream attestation**. Changing a vendored file fails
a test and forces the change to be explained, which is the property that would
otherwise be missing. They do not and cannot prove the content matches the
Authority.

| file | sha256 |
|---|---|
| `hmac_signer.py` | `b11d631b576ce6bc509fcd3e740f62ce6f30f996bc0851fbbe2ef519bd122dc1` |
| `protocol.py` | `c9128a126ab639679980ea5b12f545701adfe1bc3382fae4862b1d6fdbb11d27` |
| `integration_contract.py` | `7757207d5bd757028f59ebc11d8cd6f7a1daf212877caf427cb08576306b5e12` |
| `integration_allowlist.py` | `d837cb73ef21e53b99e3adcd0e8ea7a48f7a3bf5fdd75083e332b23a63a93aa4` |
| `executor_base.py` | `cdf1cd6440a63970228a798c4efdcab1a04588e6428d44de314917254f32e860` |

All five files have been verified byte-identical to the Authority's
`packages/a2a-core/a2a_common/` below the vendored header. Any edit is applied
**UPSTREAM first**, then propagated here and re-verified.

## Wire compatibility notes

Two things in this package are deliberately asymmetric and must not be
"cleaned up" locally:

- **Header namespace.** `hmac_signer.py` defines `LEGACY_HEADER_PREFIX` as the
  single retargetable home for the superseded header namespace, and exposes a
  public `emit_legacy_headers()` so `client.py`'s observability headers ride the
  same `A2A_EMIT_LEGACY_HEADERS` switch as the envelope headers. The authority
  side of that rollout is **not yet applied** — its outbound calls still carry
  only the legacy spelling. The partner half is safe alone: the correlation
  headers are emit-only, nothing on either side reads them, so dual-emitting is
  purely additive.

- **Direction / ErrorCode values.** `_missing_` aliases let the neutral
  spellings (`authority_to_partner`, `partner_identity_mismatch`, …) PARSE,
  while the established values stay what is EMITTED. The cert rig matches the
  emitted strings byte-for-byte and partner SOC rules key on the error strings,
  so emission flips only after those consumers move. The identical block lands
  upstream via `apply-authority-wire-dual-accept.py`; until it runs there, the
  alias is dormant here (nothing on this side constructs these enums from wire
  strings).

Note the asymmetry that remains: the authority's `MANIFEST` states that keeping
`hmac_signer.py` byte-identical across the A2A trust boundary is a
**release-coordination responsibility** — its sync script cannot see this
repository, and this repository's hash test cannot see upstream. Neither side
can detect the other drifting. Re-verify by hand whenever either moves.

## Open items

1. Diff each file against `packages/a2a-core/` in the Authority repository at
   a known commit and record that commit SHA here.
2. Either vendor `scripts/ci/sync-a2a-core.sh` into this repository or amend
   the five vendored headers to name a script that actually exists.

## Updating a vendored file

Re-sync from canonical, then update the matching hash above and in
`tests/test_vendored_a2a_core.py` in the same commit. Do not update the hash
alone; the point is that the two move together and the reason is stated.
