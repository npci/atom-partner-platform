#!/usr/bin/env bash
# Repository hygiene gate — domain-coupling ratchet.
#
# This platform is domain-general: anything specific to one payments network
# belongs in a domain pack, not in the source. The gate is a RATCHET rather
# than a hard zero — the count must not EXCEED a recorded baseline. That locks
# in the current state without blocking merges. Lower a baseline whenever you
# reduce a count; the gate then holds the new floor.
#
# Run locally exactly as CI does:   bash scripts/ci/hygiene-check.sh
#
# ⚠️ NEVER USE `\b` IN A RULE. `git grep -E` does not support word boundaries and
# matches NOTHING silently — a rule written with `\b` reports a permanent, cheery
# zero. The canary below exists so that cannot pass unnoticed.
set -uo pipefail
cd "$(dirname "$0")/../.."

fail=0

# ── Canary ───────────────────────────────────────────────────────────────────
# A rule that MUST match. If the regex engine silently stops matching, every
# other rule below would report a false, reassuring zero. Fail loudly instead.
# Matches "Copyright" rather than the licence NAME, so a relicence does not turn
# a working scanner into a fatal abort.
canary=$(git grep -lIE 'Copyright' -- LICENSE 2>/dev/null | wc -l | tr -d ' ')
if [ "$canary" -eq 0 ]; then
  echo "FATAL: canary rule matched nothing — the scanner is broken, not the tree."
  echo "       Every result below would be a false negative. Refusing to pass."
  exit 2
fi

# ── What counts as domain leakage ────────────────────────────────────────────
# Two families: the payment RAILS and participants (NPCI, UPI, VPA, IFSC, bank,
# PSP/TPAP, NEFT/IMPS/RTGS/NACH/AEPS), and the surrounding CONCEPT vocabulary
# (MCC, AML, KYC, CBS, AutoPay, payer/payee, INR, the rupee sign, Aadhaar).
# The concept terms matter as much as the rail names: a prompt asking about MCC
# codes and AML rules steers the model toward one industry just as hard as one
# naming UPI, and reads clean under a rails-only pattern.
#
# `switch` and `mandate` are deliberately ABSENT: both are ordinary English in
# this codebase ("feature switch", "mandated by the spec"), and a rule that
# mostly matches innocent prose gets its failures waved through — worse than no
# rule.
#
# `AML` is written `(^|[^Y])AML` for that same reason: bare `AML` matches the
# word YAML, and this repository talks about YAML constantly — domain packs are
# YAML files. Every uppercase `AML` hit outside data/ was the word YAML, 18 of
# them, and two of those broke the docs ratchet in a paragraph explaining that a
# DOMAIN_PACK names a YAML FILE. A gate that fails a de-branding doc for saying
# "YAML" is the "innocent prose" failure above, so exclude it at the pattern
# rather than absorb it into a baseline. RE2 has no lookbehind and `git grep -E`
# has no `\b` (see the warning at the top), so the preceding character is
# consumed instead; `^` keeps a line-initial AML counted. data/ has the only two
# real AML hits and they still count. Settlement is included lowercase+capitalised; it survives one legitimate
# use (the generic pack's vendor taxonomy names "settlement or fulfilment"),
# which the baseline absorbs.
DOMAIN_TERMS='(NPCI|npci|UPI|upi_|BHIM|bhim|payment|Payment|VPA|vpa|IFSC|ifsc|REMITTER|Remitter|remitter|BENEFICIARY|Beneficiary|beneficiary|BANK|Bank|bank|PSP|TPAP|NEFT|IMPS|RTGS|NACH|AEPS|RuPay|rupay|Rupay|MCC|(^|[^Y])AML|KYC|CBS|AutoPay|autopay|PAYER|Payer|payer|PAYEE|Payee|payee|INR|₹|Aadhaar|aadhaar|AADHAAR|Settlement|settlement)'

# The copyright header sits in ~254 files and contains "National Payments
# Corporation of India". It is legal text, it is required by the licence, and it
# is not going anywhere — but "Payments" inside it matches DOMAIN_TERMS, so
# counting it would bury the signal under a constant that cannot be reduced.
# Excluded by LINE, not by file, so a genuine hit elsewhere in a file that also
# carries the header is still counted.
COPYRIGHT_LINE='Copyright [0-9]{4} National Payments Corporation of India'

# ── Ratchet ──────────────────────────────────────────────────────────────────
# Domain packs are EXCLUDED from every count. A pack file's entire purpose is
# to hold one domain's vocabulary, so `backend/app/packs/upi/upi.yaml` saying
# UPI, VPA and IFSC is the seam working, not leakage — the same reasoning that
# pins `frontend/labels.upi.json`. Counting them would also erode the gate over
# time: every new domain pack would raise the baseline, buying back headroom
# for real leakage elsewhere.
PACK_EXCLUDE=':!backend/app/packs'

# This file is excluded from every count for the same reason as COPYRIGHT_LINE:
# DOMAIN_TERMS spells out the vocabulary, so the scanner matches its own pattern
# definition. Counting it means every term ADDED here raises the measured
# coupling, which reads as a regression and — worse — makes widening the pattern
# look like it made the tree dirtier. Excluded by PATH; nothing else under
# scripts/ is exempt.
SELF_EXCLUDE=':!scripts/ci/hygiene-check.sh'

ratchet_in_path() { # name, path, baseline, [extra pathspecs...]
  # Counts OCCURRENCES inside one path, not files: coupling is measured in
  # hits, and one file can hold dozens.
  #
  # Trailing arguments are passed to git as additional pathspecs, so a rule can
  # exclude a subtree that another rule already counts.
  local name="$1" path="$2" base="$3" n
  shift 3
  n=$(git grep -hIE "$DOMAIN_TERMS" -- "$path" "$PACK_EXCLUDE" "$SELF_EXCLUDE" "$@" 2>/dev/null \
        | grep -vE "$COPYRIGHT_LINE" \
        | grep -ohE "$DOMAIN_TERMS" | wc -l | tr -d ' ')
  n=${n:-0}
  if [ "$n" -gt "$base" ]; then
    printf 'FAIL  %-32s %5s hits, baseline %s — coupling INCREASED\n' "$name" "$n" "$base"
    # Top offenders by hit count, not the plain file list: every file under
    # backend/app matches, so listing them all printed 130 lines that told you
    # nothing about where the new hits landed.
    echo "        worst files (hits · path):"
    git grep -cIE "$DOMAIN_TERMS" -- "$path" "$PACK_EXCLUDE" "$SELF_EXCLUDE" "$@" 2>/dev/null \
      | awk -F: '{printf "%8d  %s\n", $2, $1}' | sort -rn | head -10
    fail=1
  elif [ "$n" -lt "$base" ]; then
    printf 'ok    %-32s %5s (baseline %s — improved, LOWER THE BASELINE)\n' "$name" "$n" "$base"
  else
    printf 'ok    %-32s %5s (at baseline)\n' "$name" "$n"
  fi
}

echo "── Domain coupling ─────────────────────────────────────────────────────"

# Baselines are read off the COMMITTED tree (`git grep` sees tracked files
# only), copyright headers and backend/app/packs excluded. Measure from a clean
# checkout: a baseline taken while a sweep is mid-edit locks in a floor that no
# committed code supports.
#
# ⚠️ MEASURE, DO NOT ESTIMATE. Every number here was re-measured on 2026-09-08 by
# running this script against a clean checkout of the commit that carries it.
# The previous set was not: it recorded backend/app 320 against a tree holding
# 322 and backend/tests 537 against a tree holding 556, so the gate failed on
# the very commit that wrote it and every branch cut afterwards inherited a red
# main. A baseline that has never been observed to pass is not a floor, it is a
# guess. If you change a baseline, run the script before you commit it.
#
# data/ carries the largest share by design — the fictional example profile is
# dense in payer/payee/₹ and is pack-adjacent sample data, counted here so it
# cannot quietly grow.
ratchet_in_path "domain terms in backend/app"  "backend/app"  310
ratchet_in_path "domain terms in frontend/src" "frontend/src"  98
ratchet_in_path "domain terms in data/"        "data"         169

# backend/tests is the single largest concentration in the repo — larger than
# backend/app. Test names, fixture data and sample payloads (`payerVpa`,
# `bank_code`, `NPCI-1`) are where the domain re-enters through the back door
# once the source is clean. Much of the count is the sanctioned category: a term
# named in order to PIN it or to assert its ABSENCE. Removing those to satisfy
# the count would delete the assertion.
#
# 553 is the only baseline here that rose rather than fell. The added hits are
# that sanctioned category: the readiness-delivery and certification-discovery
# tests name the wire values they PIN — `initiated_by: "NPCI"` / `"BANK"`,
# `sent_to_npci`, and the `PAYER_PSP` role the `upi` pack supplies to the
# fixture. Each is an input or an assertion; deleting it to buy back a hit would
# delete what the test checks.
ratchet_in_path "domain terms in backend/tests" "backend/tests" 553

# CODEOWNERS alone carries 36 hits (one repeated team handle), and the `*.md`
# docs rule below cannot see it because it has no extension.
ratchet_in_path "domain terms in repo config"  "CODEOWNERS"    48 \
  .github scripts deploy 'docker-compose*.yml' '.env.example' '.gitleaks.toml'

# Not a target to drive to zero. Roughly 21 of it is TRADEMARKS.md and NOTICE,
# which name the marks precisely BECAUSE they are legal notices about those
# marks; README.md carries the copyright attribution. Those must not move.
#
# backend/, frontend/ and data/ are excluded because they carry their own
# ratchets above — without the exclusions this double-counts them.
ratchet_in_path "domain terms in docs"         "*.md"          77 ':!backend' ':!frontend' ':!data'

echo
echo "── Sub-paths that must lead the cleanup ────────────────────────────────"

# Tracked separately so progress in one cannot be masked by drift in another —
# backend/app alone is coarse enough that a 40-hit regression in agents/ hides
# under a 40-hit improvement in api/.
ratchet_in_path "  core/"      "backend/app/core"      20
# The remaining hits here are comments explaining why a wire token is pinned,
# not vocabulary the agents actually speak.
ratchet_in_path "  agents/"    "backend/app/agents"    10
ratchet_in_path "  services/"  "backend/app/services"  18

# a2a_common is the WIRE CONTRACT with the authority platform: X-NPCI-Signature,
# X-NPCI-Timestamp, X-NPCI-Nonce, npci.platform.v1 and friends. The counterparty
# matches these byte-for-byte, so these hits are PINNED — this ratchet exists to
# stop the number GROWING, not to drive it to zero. Do not "fix" it.
# It is also a vendored copy: sync from the authority, never patch in place.
ratchet_in_path "  a2a_common/ (PINNED wire)" "backend/app/a2a_common" 147

echo
if [ "$fail" -ne 0 ]; then
  echo "hygiene-check: FAILED — domain coupling increased above baseline."
  echo "If the new hits are genuinely pinned (wire contract, legal text, a term"
  echo "named in order to assert its absence), say so in a comment at the site"
  echo "and raise the baseline here in the same commit."
  exit 1
fi
echo "hygiene-check: passed"
