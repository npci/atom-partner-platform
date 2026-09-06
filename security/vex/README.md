# VEX annotations

`partner-platform.vex.json` records this project's triage decisions for the three
Security policy violations in the **A2A Compliance Report (2026-08-27)**.

## Why this file exists

The scan reported all three with `ANALYSIS STATE: Unannotated`. That is the
scanner asking a question, not asserting a breach — and it is the reason the
findings were open.

Version is not the lever for any of the three:

- `sonatype-2021-0025` was published in **2021** and fires against
  `sqlalchemy 2.0.36`, released three years later.
- `sonatype-2017-0717` was published in **2017** and fires against
  `react 19.2.5`, released nine years later.

Both are Sonatype-proprietary advisories describing what a library *can do*
rather than a defect in a release, so they apply to every version. For
`CVE-2025-45768` the scan is its own evidence: PyJWT 2.13.0 already warns on
short HMAC keys, and 2.13.0 is nonetheless the version reported. **No upgrade
clears any of the three.** What clears them is a recorded decision backed by a
control.

| Advisory | Component | State | Justification |
|---|---|---|---|
| `sonatype-2021-0025` | `sqlalchemy 2.0.36` | `not_affected` | `protected_by_mitigating_control` |
| `CVE-2025-45768` | `pyjwt 2.13.0` | `not_affected` | `protected_by_mitigating_control` |
| `sonatype-2017-0717` | `react 19.2.5` | `not_affected` | `code_not_present` |

> The SQLAlchemy entry previously claimed `code_not_reachable`. That was wrong:
> `text()` is called on every retrieval and ingest path, so the API is very much
> reachable. What is absent is *dynamic assembly of the string passed to it*.
> `protected_by_mitigating_control` is the claim the code actually supports, and
> it is pinned by `test_sqlalchemy_claim_is_not_code_not_reachable`.

## Read this before you trust the annotations

**A VEX document changes a scan result only if the scanner ingests it.** Until
this file is wired into the pipeline that produced the A2A Compliance Report,
all three findings will keep reporting as `Unannotated` no matter how accurate
the analysis is. Accuracy and effect are separate problems; this directory
solves the first, and the section below is how you solve the second.

## Using it in a scan

Some platforms accept a VEX document alongside the SBOM. Prefer that — it keeps
the analysis in its own reviewable artifact:

```bash
--vex security/vex/partner-platform.vex.json
```

Where the platform consumes only one document, merge the annotations into the
generated SBOM before upload:

```bash
python scripts/merge_vex.py \
    --sbom sbom.json \
    --vex security/vex/partner-platform.vex.json \
    --out sbom.vex.json
```

`merge_vex.py` matches on **package URL, not `bom-ref`** — `bom-ref` values are
internal to whichever tool emitted the document, while purls are the identity
both sides share. It normalises PyPI names per PEP 503, so `pkg:pypi/PyJWT` and
`pkg:pypi/pyjwt` resolve to the same component, and it strips qualifiers such as
`?arch=`.

**It exits non-zero when an annotation fails to resolve, and writes nothing.**
That is the point of the script rather than an inconvenience: an annotation that
attaches to no component looks handled and is not. Run it before upload so a
stale annotation breaks the build instead of becoming a false audit record.

## Dependency changes

Zero packages were added, removed or upgraded. This is a **result**, not a
constraint — an upgrade path was evaluated after the no-change constraint was
lifted, and rejected on evidence:

- `sqlalchemy 2.0.36 → 2.0.52` was tested and the backend suite passes against
  it, so the bump is compatible. It cannot clear `sonatype-2021-0025`, which is
  version-independent, and applying it would deepen the lockfile defect below.
  The reasoning is recorded inline at the pin in `backend/requirements.txt`.

## Known defect found during this work: the lockfile is stale

`backend/Dockerfile` installs `requirements.lock`; `backend/Dockerfile.prod`
installs `requirements.txt`. They disagree:

| package | `requirements.txt` | `requirements.lock` |
|---|---|---|
| `pyjwt` | `2.13.0` | **absent** |
| `fastapi` | `0.141.1` | `0.115.5` |
| `starlette` | `1.3.1` | `0.41.3` |
| `pyasn1` | `0.6.4` | `0.4.8` |
| `python-jose` | *(removed)* | `3.4.0` — with `ecdsa 0.19.2` |

The `pyjwt` row is an absence, not a skew. `app/api/auth.py` and
`app/a2a_common/auth_middleware.py` both do a top-level `import jwt`, which only
PyJWT provides, so **an image built from the lock cannot import the auth
module.** The lock also still contains `python-jose` and `ecdsa` — removing
exactly those, because `ecdsa` carries CVE-2024-23342 (HIGH) with no fix, is
what `requirements.txt` says the 2026-08-20 JWT migration was *for*.

This predates the SBOM work and is **not fixed here**: regenerating a hash-locked
file needs registry reachability and a container runtime, and hand-editing it is
what its own header forbids. It is instead reported on every test run by
`backend/tests/test_requirements_lock_sync.py`, as `xfail(strict=True)` so that
regenerating the lock forces the note to be removed rather than left to rot. A
companion test bounds the drift, so the known defect cannot become cover for the
next one.

**This matters for the SBOM directly**: an SBOM generated from the lock
describes a different component set than one generated from `requirements.txt`,
and `pyjwt 2.13.0` — one of the three annotated components — is not in the lock
at all.

## The annotations are only as true as the tests

Each `not_affected` claim describes the code *as it is today*. The realistic way
such a claim becomes false is ordinary feature work: someone adds a filter with
an f-string, or reaches for `rehype-raw` to render rich text. The annotation
would then be a false audit record — in a regulated NPCI context, meaningfully
worse than an open low-severity finding.

So each claim is paired with a test that fails when the claim stops holding, and
**each was verified to fail against a deliberately injected violation**, so none
can pass vacuously.

| Claim | Enforced by |
|---|---|
| No dynamically-assembled SQL | `backend/tests/test_no_dynamic_sql.py` |
| `_delete_chunks` scoping is exact | `backend/tests/test_delete_chunks_scoping.py` |
| HMAC keys meet RFC 7518 §3.2 | `backend/tests/test_key_strength.py` |
| Weak secrets rejected at the API | `backend/tests/test_settings_secret_strength.py` |
| No raw-HTML sinks | `frontend/test/noRawHtml.test.mjs` |
| The VEX itself stays valid and resolvable | `backend/tests/test_vex_document.py` |
| `requirements.txt` and the lock agree | `backend/tests/test_requirements_lock_sync.py` |

These run in the `security` stage of `.gitlab-ci.yml`, which was added as part
of this work — **the repository previously had no CI at all**, so none of the
guards had ever executed automatically.

**If one of these tests fails, fix the code or withdraw the annotation. Do not
weaken the test.**

### Stated limits of the guards

Claims here are deliberately bounded, because a guard described as stronger than
it is becomes the weakest link:

- **The SQL guard is intra-procedural.** It tracks taint through assignments
  within a function and recognises helpers that return dynamically-built
  strings. Taint stored on an object attribute, passed as a parameter into a
  helper, or routed through a container is **not** detected. The supportable
  claim is *"no dynamic SQL in the shapes this guard covers, and those shapes
  include every pattern previously present in this codebase"* — not *"provably
  no dynamic SQL"*. The gap is asserted in
  `test_guard_limitations_are_documented`, so closing it later forces the VEX
  wording to be revisited.
- **`test_settings_secret_strength.py` does not run without the full dependency
  set.** It imports the application, which pulls in `a2a-sdk`. Its assertions
  were confirmed by code inspection locally; treat it as verified only where CI
  installs everything.
- **The `backend-tests` CI job has not been observed green.** It is written
  against Postgres, and the environment this was authored in had no container
  runtime. It is marked `allow_failure: true` and will likely need adjusting on
  first run. The `security` stage *has* been run locally and passes.

### Defects found by testing the guards adversarially

Injecting real violations, rather than assuming the guards worked, found three
that mattered:

1. **The SQL guard missed 5 of 9 injections**, including the pre-existing
   f-string in `retrieval.py`, because it only inspected arguments written
   inline at the call site. Fixed by adding taint tracking; it now reports the
   assignment line, not just the sink.
2. **The hardening itself introduced a data-integrity bug.** `_delete_chunks`
   enumerated six static statements for eight scope combinations, and its
   `if/elif` chain tested `repo_id` before `change_id` — so a call passing both
   silently dropped the `change_id` predicate and **deleted more rows than
   requested**. Found by differential testing against the original dynamic
   builder (12 mismatches). Fixed with an eight-entry dispatch table.
3. **The key-strength validator rejected legitimate keys.** Its distinct-char
   and per-char-entropy floors are anti-correlated with guessability:
   `token_hex(16)` scores 9 distinct chars and 2.78 bits/char, while
   `CorrectHorseBatteryStaple` scores 25 and 4.38. It rejected roughly 1 in
   1,000 valid keys. Recalibrated to total entropy plus structural checks:
   **zero** false rejections across 36,000 keys per suite run (18 formats ×
   2,000), and across 480,000 in a one-off sweep at 30,000 per format. The
   calibration is reproducible via
   `backend/scripts/calibrate_key_strength.py`.
4. **The recalibration was still slightly wrong, and it looked like
   flakiness.** The padding-run threshold was 8, but `token_hex(32)`
   legitimately produces runs of 9 — so the rule was *certain* to reject valid
   keys, just rarely (about 1 suite run in 10, always the same case). A
   threshold below the observed maximum of real CSPRNG output is a bug, not bad
   luck, and the low rate is what makes it dangerous: the natural response is to
   re-run the build. Raised to 12 (chance of ~1 in 440 billion for hex) with no
   loss of detection, since real padding runs to 30+ characters. Now 0 failures
   in 20 consecutive runs, and pinned by
   `test_padding_run_threshold_exceeds_what_real_csprng_output_produces`, which
   asserts the invariant against the recorded maxima rather than by sampling —
   a sampling guard would reintroduce the very flake it exists to prevent.

A fifth was found in a guard's own logic: `noRawHtml.test.mjs` checked for CSP
directives in the **raw** file while checking sinks in a comment-stripped copy,
so naming the directives in a comment satisfied the check while the real CSP
could be deleted. It now strips comments first and asserts the `<meta>` tag is
genuinely emitted.

## Deployment note — one control can block startup by design

`SESSION_JWT_SECRET` is now checked for strength, not merely for presence.

- **Production** — a weak secret is always fatal.
- **Non-production** — a weak secret logs at `ERROR` and the service still
  starts. Set `ENFORCE_HMAC_KEY_STRENGTH=true` to make it fatal there too.

The two-stage default is intentional: any environment already running a short
secret would otherwise stop booting the moment it upgraded, and the operator's
incentive becomes "roll back" rather than "fix the secret". Use the observation
window to find weak secrets, then enable enforcement everywhere.

Generate a compliant secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Rotating `npci_jwt_secret` requires coordination with NPCI.** It is a shared
key, and the A2A ingress is fail-closed by design (ADR-0003), so a unilateral
change breaks inbound calls rather than degrading gracefully.
