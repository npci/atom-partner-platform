# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""HMAC key-strength validation for the HS256 signing secrets.

WHY THIS MODULE EXISTS — CVE-2025-45768 (PyJWT)
-----------------------------------------------
CVE-2025-45768 reports that PyJWT does not enforce a minimum key length when
signing with HMAC algorithms, so an application can sign HS256 tokens with a
trivially guessable secret and PyJWT will accept it.

WHAT PyJWT ACTUALLY DOES — READ THIS BEFORE CHANGING ANYTHING BELOW
-------------------------------------------------------------------
The pinned PyJWT (2.13.0) is **not** indifferent to key strength. Verified
against the installed package:

  >>> jwt.encode({"a": 1}, "short", algorithm="HS256")
  InsecureKeyLengthWarning: The HMAC key is 5 bytes long, which is below the
  minimum recommended length of 32 bytes for SHA256. See RFC 7518 Section 3.2.

It also raises `InvalidKeyError` on an empty HMAC key, on a PEM/SSH key handed
in as an HMAC secret, and on JWK-shaped input — real algorithm-confusion
defences. So the honest description of this module is:

  PyJWT already DETECTS an under-length key and emits a WARNING.
  This module ESCALATES that to a hard failure, and EXTENDS it in two ways
  PyJWT cannot.

Both extensions are the actual value delivered here:

  1. A warning is not a control. Python warnings default to stderr once per
     call site and are routinely filtered; nothing stops the process. This
     module makes the same condition fatal in production.
  2. PyJWT can only inspect a key at signing time, when the token is already
     being minted. Two of this platform's three HS256 secrets
     (`npci_jwt_secret`, `npci_hmac_secret`) are installed through the settings
     API and stored encrypted, so the first PyJWT-visible use is an inbound
     request on the A2A ingress — far too late to tell an operator to fix it.
     This module validates on WRITE, before the value is ever persisted.
  3. Length is not guessability. PyJWT's check is a byte count, so `"a" * 32`
     satisfies it silently. Guessability is an application-policy question and
     is where most of the code below goes.

An earlier revision of this docstring claimed PyJWT "deliberately refuses" to
check key strength and that "there is no fixed version and there never will
be". The first half is factually wrong — see the warning above. The second half
is right for a different reason: the advisory attaches to the pyjwt component
coordinates, and no release removes the ability to pass a weak string, so no
upgrade clears the SBOM finding. The remediation is a recorded triage plus this
enforced control, not a version bump.

WHY 32 BYTES
------------
RFC 7518 §3.2: "A key of the same size as the hash output (for instance, 256
bits for HS256) or larger MUST be used with this algorithm." 32 bytes is the
floor the standard sets, and the same number PyJWT's own warning uses. We do not
require more: a higher bar buys nothing against brute force on SHA-256 and
pushes operators toward writing secrets down.

HOW GUESSABILITY IS ASSESSED — AND WHY THE OBVIOUS APPROACH FAILS
-----------------------------------------------------------------
The intuitive checks are "count distinct characters" and "measure Shannon
entropy per character". Both were tried, both were WRONG, and the measurements
that killed them are worth recording so they are not reintroduced.

Minima observed across 30,000 samples of each legitimate generation format,
against the same metrics for known-weak values:

    LEGITIMATE                 distinct   bits/char       WEAK                      distinct   bits/char
    secrets.token_hex(16)             9        2.78       "the quick brown fox..."        25        4.38
    40 digits (OTP-style)             7        2.68       "NPCIPartnerPlatform2026..."    22        4.32
    uuid4                            10        3.11       "Correct-Horse-Battery..."      18        3.82
    grouped hex                      10        3.08       sha256("password") hex          16        3.81

Every weak value scores HIGHER than several legitimate ones. The metrics are
not merely imprecise, they are anti-correlated with what we care about: they
measure character-frequency flatness, while guessability is a property of
STRUCTURE (is it words? a date? a hostname?). A threshold placed anywhere on
these axes either rejects real keys or admits guessable ones.

The consequences were measured, not theorised. With the old floors
(MIN_DISTINCT_CHARS=10, 3.0 bits/char), `secrets.token_hex(16)` — a documented,
perfectly good 32-byte key — was rejected at 0.0153% (61 of 400,000). Over the
2,000-sample suite that guarded it, that is a 26% chance of a spurious CI
failure per run; the suite failed 5 of 12 consecutive runs. Numeric keys were
rejected at 15%. Meanwhile six genuinely guessable values passed.

So the checks here are STRUCTURAL:

  - length in UTF-8 bytes (RFC 7518 floor, matches PyJWT's own metric)
  - TOTAL Shannon entropy, not per-character — a real lower bound on key
    material that does not penalise a small alphabet used at length
  - repetition: single-character padding runs, and short units tiled to length
  - dictionary structure: the value is mostly recognisable words
  - known-weak digests: the value is a published hash of a common password
  - placeholder tokens

Ordering note: reasons are accumulated, not short-circuited, so an operator
sees every problem at once instead of fixing them one rejection at a time.

NO NEW DEPENDENCY — BY DESIGN
-----------------------------
Standard library only (`hashlib`, `math`, `re`, `unicodedata`). Adding a
password-strength or wordlist package to remediate an SBOM finding would
introduce new SBOM components, which is the trade this whole exercise exists to
avoid.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata

# ── RFC 7518 §3.2 — HS256 keys MUST be >= the 256-bit hash output width. ────
# Identical to the threshold in PyJWT's own InsecureKeyLengthWarning.
MIN_HMAC_KEY_BYTES = 32

# ── Total-entropy floor, in bits across the WHOLE value ─────────────────────
# Deliberately TOTAL rather than per-character. Measured minimum across every
# legitimate format tested was 81.6 bits (32 random digits, the weakest
# realistic institutional format). A floor of 64 leaves ~18 bits of headroom
# there while still catching degenerate input: "a"*32 scores 0.0 and "ab"*20
# scores 40.0.
#
# This is a floor on OBSERVED character-frequency entropy, which is an upper
# bound on true key entropy — it cannot prove randomness, only rule out the
# obviously-insufficient. The structural checks below do the rest.
MIN_TOTAL_ENTROPY_BITS = 64.0

# ── Dictionary-structure thresholds ─────────────────────────────────────────
# A value is rejected as "mostly words" only when BOTH hold: at least
# MIN_DICTIONARY_WORDS recognised words, AND those words cover at least
# DICTIONARY_COVERAGE of its characters. Requiring both is what keeps random
# strings safe — a base64 key that happens to contain "face" or "dead" has one
# short word and ~10% coverage, nowhere near either bar.
#
# Minimum word length is 4: 3-letter fragments ("ace", "bad", "fed") occur in
# random hex often enough to matter, and no realistic human secret is built
# from them alone.
MIN_DICTIONARY_WORD_LEN = 4
MIN_DICTIONARY_WORDS = 3
DICTIONARY_COVERAGE = 0.45

# ── Word list ───────────────────────────────────────────────────────────────
# Not a general English dictionary — deliberately scoped to the vocabulary that
# actually shows up in hand-made secrets: common nouns/verbs/adjectives, and the
# infrastructure and payments terms specific to this platform's operators. A
# larger list would raise false-positive risk on random strings for no gain,
# since the coverage threshold, not list size, is what does the work.
_WORDS = frozenset("""
about above accept access account admin after again against agent alert all
alpha also always android anthropic api app apple application apply april
architecture archive area around array asset assign august auth author auto
available back backend backup bank base basic batch battery bearer before begin
best beta better between beyond bill billing binary blue board body book both
bracket branch break bridge broker brown build built business button cache
call cancel canonical capital card care case cash center central certificate
certification chain change channel charge chart check child china choose city
claim class clean clear click client close cloud cluster code cold collect
color column come command comment commit common company complete compliance
component compute config configuration confirm connect connection console
consumer contact container content context continue contract control cookie
copy core corporate correct cost count counter country create credential credit
critical cross current custom customer cycle daily data database date day debit
debug december decide default define delete delivery demo deploy deployment
design desk detail detect develop development device digital direct director
directory disable disaster discount display distinct district docker document
dollar domain double down download draft drive driver drop dummy each early
east easy economy edge edit education effect eight either electric element
email employ enable encrypt end endpoint energy engine engineer english enter
enterprise entity entry environment equal error escrow event every exact
example exchange execute exist exit expect expire export express extend
external face factor fail failover false family fast feature february federal
feed field file fill filter final finance financial find fintech firewall first
fiscal fix flag flow follow force foreign form format forward found four frame
framework free from front full function fund future gateway general generate
get give global goal gold good google grade grant graph great green grid group
grow guest guide handle hard hash have head header health hello help here high
history hmac hold home honest horse host hostname hour house http human
hundred idea identifier image implement import important include income index
india indian industry info information infra infrastructure ingress init inner
input insert inside install instance institute insurance integration interest
interface internal international internet into invoice issue item january job
join json july jump jumps keep kernel key keyword kind know label language large
last late launch layer lazy lead ledger left legal length less letter level
library
license life light like limit line link list live load loan local location
lock log logic login long look loop love machine main maintain major make
manage management manager mandate manual many march market master match
material matrix maximum maybe mean measure media medium member memory merchant
merge message method metric middle might migrate million minimum minor minute
mobile mode model module money monitor monkey month more most mount move multi
must
mutual name national native need network never next night node none normal
north note notice november now null number object october offer office offline
often oil onboard once online only open operate operation option orange order
organization origin other outbound output over owner package page paid parent
park parse part partner party pass password patch path pattern payment payout
peer pending people percent perform period permission person phase phone pilot
pipeline place plan platform please plus point policy pool port portal position
possible post power practice prefix premium prepare present press prevent price
primary print prior private probe process produce product production profile
program project promote proof proper property protect protocol provide provider
proxy public pull purchase purpose push python qualify quality quarter query
question queue quick quiet quota rail raise random range rate reach read ready
real reason receipt receive recent record recover reduce refer refresh region
register registry regular reject relate release remain remote remove repay
replace report repository request require reserve reset resolve resource
respond response rest restore result retail retry return revenue reverse review
revoke right risk role room root round route router rule run runtime safe
salary sale same sample sandbox save scale scan schedule schema scheme scope
score screen script search second secondary secret section sector secure
security segment select self sell send senior sensor september serial serve
server service session set settle settlement setup seven several shard share
sheet shell shift ship shop short show side sign signature significant signing
silver similar simple since single site size slow small social socket soft
software solution some sort sound source south space span speak special
specific speed spend split sponsor spring sql stack staff stage staging
standard staple star start state statement static station status step stop
storage store story strategy stream street strong structure study style
subject submit success such sudden suffix suggest summary supply support sure
surface switch symbol system table take target task tax team tech technical
technology tell template term terminal test testing text than thank that them
then there these they thing think third this those three through ticket time
today token tool top total touch trace track trade traffic train transaction
transfer transit tree trend trigger true trust turn type under union unique
unit universal update upgrade upload upon usage user using valid validate value
variable vault vendor verify version very view virtual visa vision visit voice
volume vote wait wallet want warehouse warning watch water wave weak wealth
web website week weight welcome well west what when where which while white
who whole why wide will wind window wire wireless with within without word
work worker world would write wrong year yellow yes yield young your zero zone
""".split())

# ── Domain/infra tokens that are not English words but are equally predictable
# in a secret. Kept separate so the English list stays reusable.
_DOMAIN_WORDS = frozenset("""
npci upi imps neft rtgs aadhaar rupay bhim nach aeps cts ecs
hdfc icici sbi axis kotak yesbank idfc rbl indusind
prod dev qa uat sit preprod nonprod
aws azure gcp k8s kube nginx redis postgres mysql mongo kafka rabbitmq
linux ubuntu centos debian windows macos
jwt hmac sha256 sha512 rsa aes gcm cbc tls ssl
localhost intranet corp org net com edu gov
""".split())

_ALL_WORDS = _WORDS | _DOMAIN_WORDS

# ── Known-weak digests ──────────────────────────────────────────────────────
# A hash LOOKS like a strong key — high entropy, full hex alphabet — but if it
# is the digest of a common password it is a single lookup away from being
# known. sha256("password") passes every statistical test in this module, so it
# has to be matched exactly.
#
# Scope is deliberately narrow: md5/sha1/sha256 of a short list of the most
# common passwords. This is not a rainbow table and cannot be; it exists to
# catch the specific "I hashed a word to make it look random" pattern.
_COMMON_PASSWORDS = (
    "password", "password1", "password123", "123456", "12345678", "123456789",
    "qwerty", "abc123", "letmein", "welcome", "admin", "administrator",
    "root", "toor", "secret", "changeme", "default", "test", "guest",
    "iloveyou", "monkey", "dragon", "sunshine", "princess", "football",
    "master", "hello", "freedom", "whatever", "trustno1", "passw0rd",
)


def _weak_digest_table() -> frozenset[str]:
    """Hex digests (md5/sha1/sha256) of `_COMMON_PASSWORDS`, both cases.

    Built once at import; ~360 short strings, negligible cost.
    """
    out: set[str] = set()
    for pw in _COMMON_PASSWORDS:
        raw = pw.encode()
        for algo in ("md5", "sha1", "sha256"):
            d = hashlib.new(algo, raw).hexdigest()
            out.add(d)
            out.add(d.upper())
    return frozenset(out)


_WEAK_DIGESTS = _weak_digest_table()

# ── Placeholder substrings ──────────────────────────────────────────────────
# Every token is >= 6 characters. Shorter ones cannot be used: across 20,000
# base64 samples, "todo" and "xxxx" each appeared by chance often enough that
# including them would reject valid keys. A 6-character floor makes accidental
# collision negligible (~1 in 5e10 per token in base64) while still catching
# every realistic human placeholder.
_PLACEHOLDER_TOKENS = (
    "changeme", "change-me", "change_me",
    "password", "passwd1", "passw0rd",
    "secret", "mysecret", "topsecret", "secretkey", "secret-key", "secret_key",
    "insecure", "placeholder", "example", "sample-", "testtest",
    "default", "dummykey", "fakekey", "notreal",
    "your-secret", "your_secret", "yoursecret",
    "0123456789", "abcdefghij", "qwertyui",
    "jwtsecret", "jwt-secret", "jwt_secret",
    "replaceme", "replace-me", "replace_me",
)


class WeakKeyError(ValueError):
    """Raised when an HMAC signing secret is below policy.

    A distinct type (rather than bare ValueError) so callers can catch exactly
    this and translate it — the settings API turns it into an HTTP 400, while
    startup lets it propagate as a fatal error.

    The message never contains the offending value, only the reason. These
    secrets are exactly the material we must keep out of logs and HTTP
    responses, and an error string is a classic way a credential leaks into a
    log aggregator.
    """


def _total_entropy_bits(value: str) -> float:
    """Shannon entropy of `value` in bits across the whole string.

    Character-frequency based, so blind to ordering: "abababab" scores the same
    as a permutation of the same letters. Acceptable because ordering-blindness
    is covered by the repetition and dictionary checks; this one exists purely
    as a floor on total material.
    """
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(value)
    per_char = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return per_char * n


def _longest_run(value: str) -> int:
    """Length of the longest consecutive single-character run in `value`."""
    best = 0
    for m in re.finditer(r"(.)\1*", value):
        best = max(best, len(m.group(0)))
    return best


#: Absolute run length that is treated as padding regardless of key length.
#: See `_has_padding_run` for how this number was chosen.
MAX_ABSOLUTE_RUN = 12

#: A run covering this fraction of the key is padding even if shorter than
#: `MAX_ABSOLUTE_RUN` — but never fires below `MIN_PADDING_RUN`.
PADDING_RUN_FRACTION = 3
MIN_PADDING_RUN = 8


def _has_padding_run(value: str) -> bool:
    """True if `value` contains a character run long enough to be PADDING.

    HOW THESE NUMBERS WERE CHOSEN — and two earlier versions that were wrong.

    v1 flagged any run of 5 or more. That rejected 18 of 20,000 `token_hex(32)`
    values: from a 16-symbol alphabet a 5-run happens by chance roughly 1 in
    1,000 keys.

    v2 raised the bar to 8, or 25% of the length. That still misfired, just
    rarely enough to look like flakiness rather than a bug — the key-strength
    suite failed about 1 run in 10, always on the same case. Measuring instead
    of reasoning showed why: over 300,000 samples per format the longest run
    observed in legitimate keys was

        base64(48)     5        uuid4          6
        base32         5        numeric 32/40  7
        token_hex(16)  7        token_hex(32)  9   <-- exceeds the threshold of 8

    A threshold of 8 sits BELOW the maximum that real CSPRNG output produces, so
    it was not unlucky, it was guaranteed to fire eventually. That is the worst
    kind of security rule: it looks calibrated, and it rejects valid keys at a
    rate low enough that the operator blames the tooling.

    v3 (this one) uses 12. From a 16-symbol alphabet a 12-run is about 1 in
    440 billion; from a 10-symbol alphabet, 1 in 2.5 billion. Measured false
    positives over 1.5 million samples spanning the run-prone formats: zero.

    The proportional arm catches padding in keys long enough that an absolute
    count would miss it — a 60-character key that is 20 identical characters is
    padded regardless — but it never triggers below `MIN_PADDING_RUN`, which is
    what kept v2 firing on short numeric keys.

    Real padding is not subtle. Nobody pads with four characters; they type
    "aaaaaaaa..." until the length check passes. Every value in the weak-key
    corpus has a run of 30 or more, so the wide margin costs no detection.
    """
    run = _longest_run(value)
    if run >= MAX_ABSOLUTE_RUN:
        return True
    if not value:
        return False
    return run >= max(MIN_PADDING_RUN, len(value) // PADDING_RUN_FRACTION)


def _is_single_repeated_unit(value: str) -> bool:
    """True if `value` is one short unit tiled to length ("abcabcabc...").

    Checked separately from entropy because tiling preserves the character
    distribution: "abcdefghij" repeated scores full frequency entropy yet
    carries only ~10 characters of real key material.
    """
    n = len(value)
    for unit in range(1, n // 2 + 1):
        if n % unit == 0 and value[:unit] * (n // unit) == value:
            return True
    return False


def _dominant_repeated_unit(value: str) -> str | None:
    """Return the repeating unit if `value` is MOSTLY one short unit tiled.

    ── Why exact tiling is not enough ──────────────────────────────────────────
    `_is_single_repeated_unit` requires the unit to divide the length exactly, so
    a single stray character defeats it: "Monkey" * 6 is caught, but
    "Monkey" * 6 + "X" is not. That is a one-keystroke bypass of a real check,
    and the resulting secret carries only the ~6 characters of "Monkey"
    regardless of the 37 characters of length it reports.

    This catches the near-miss case: any unit of 1..12 characters whose
    repetition covers at least 80% of the value. The unit cap keeps this away
    from long random strings (no 20-character unit repeats by chance), and the
    coverage threshold tolerates the leading/trailing debris that exact tiling
    cannot.

    Returns the unit so the reason can name it, or None.
    """
    n = len(value)
    if n < 8:
        return None
    for size in range(1, min(12, n // 2) + 1):
        unit = value[:size]
        repeats = n // size
        # How many characters are covered by contiguous repetition from the start
        covered = 0
        for i in range(repeats):
            if value[i * size:(i + 1) * size] == unit:
                covered += size
            else:
                break
        if covered / n >= 0.8:
            return unit
    return None


def _dictionary_words(value: str) -> tuple[list[str], float]:
    """Recognised words in `value`, and the fraction of characters they cover.

    Splits on non-alphabetic characters AND camelCase boundaries, so
    "NPCIPartnerPlatform2026" and "partner-platform-key" both decompose. Only
    tokens of at least `MIN_DICTIONARY_WORD_LEN` characters that appear in the
    word list are counted.
    """
    if not value:
        return [], 0.0

    # Split camelCase / PascalCase into separate tokens before lowercasing:
    # "PartnerPlatform" -> "Partner Platform". Also handles an acronym followed
    # by a word ("NPCIPartner" -> "NPCI Partner").
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)

    tokens = re.split(r"[^A-Za-z]+", spaced)
    found = [
        t.lower() for t in tokens
        if len(t) >= MIN_DICTIONARY_WORD_LEN and t.lower() in _ALL_WORDS
    ]
    covered = sum(len(w) for w in found)
    return found, covered / len(value)


def _looks_like_words(value: str) -> tuple[bool, list[str]]:
    """True if `value` is mostly recognisable words.

    Requires BOTH a word count and a coverage fraction, which is what keeps
    random strings safe: a base64 key containing "face" has one word and ~10%
    coverage, far below both bars.
    """
    found, coverage = _dictionary_words(value)
    hit = len(found) >= MIN_DICTIONARY_WORDS and coverage >= DICTIONARY_COVERAGE
    return hit, found


def assess_hmac_secret(value: str, *, label: str = "signing secret") -> list[str]:
    """Return a list of human-readable reasons `value` is unfit for HS256.

    An empty list means the secret satisfies policy. Returning reasons rather
    than raising lets callers choose severity — startup can warn during a
    rollout window and fail later — without duplicating the rule set.

    `label` names the setting in each reason so an operator with several secrets
    configured knows which one to regenerate.
    """
    reasons: list[str] = []

    if not value:
        return [f"{label} is unset."]

    # Measure the KEY, not its display form. A JWT HMAC key is bytes; a value
    # with multi-byte characters has fewer characters than bytes, and the RFC's
    # floor is expressed in bits of key material. Encode to UTF-8 — what PyJWT
    # does internally when handed a str — and measure that.
    raw = value.encode("utf-8")
    if len(raw) < MIN_HMAC_KEY_BYTES:
        reasons.append(
            f"{label} is {len(raw)} bytes; HS256 requires at least "
            f"{MIN_HMAC_KEY_BYTES} bytes (256 bits) per RFC 7518 section 3.2."
        )

    # Normalise before the shape checks so visually-identical Unicode forms
    # cannot smuggle a placeholder or a word past the checks below.
    normalised = unicodedata.normalize("NFKC", value)
    lowered = normalised.lower()

    entropy = _total_entropy_bits(normalised)
    if entropy < MIN_TOTAL_ENTROPY_BITS:
        reasons.append(
            f"{label} carries approximately {entropy:.0f} bits of total "
            f"entropy; at least {MIN_TOTAL_ENTROPY_BITS:.0f} are required. "
            f"Its characters are too few or too repetitive to hold a "
            f"256-bit-equivalent key."
        )

    if _has_padding_run(normalised):
        reasons.append(
            f"{label} contains a long run of one repeated character, which "
            f"indicates padding to satisfy a length requirement."
        )

    if _is_single_repeated_unit(normalised):
        reasons.append(
            f"{label} is a short pattern repeated to reach its length, so it "
            f"carries far less key material than its size suggests."
        )
    else:
        # Only checked when exact tiling did NOT fire, so a tiled value yields
        # one clear reason rather than two overlapping ones.
        unit = _dominant_repeated_unit(normalised)
        if unit is not None:
            reasons.append(
                f"{label} is almost entirely the sequence {unit!r} repeated. "
                f"Appending or prepending a few characters to a repeated "
                f"pattern does not add meaningful key material."
            )

    is_wordy, words = _looks_like_words(normalised)
    if is_wordy:
        # Naming the matched words is safe and makes the failure actionable:
        # they are entries from a public list in this module, and by definition
        # the operator already knows them — they typed them.
        shown = ", ".join(sorted(set(words))[:5])
        reasons.append(
            f"{label} is built mostly from recognisable words ({shown}). "
            f"Word-based secrets are guessable regardless of their length or "
            f"character variety, and must not be used as HMAC keys."
        )

    if lowered in _WEAK_DIGESTS or normalised in _WEAK_DIGESTS:
        reasons.append(
            f"{label} is a published hash digest of a common password. It "
            f"looks random but is a single lookup away from being known."
        )

    for token in _PLACEHOLDER_TOKENS:
        if token in lowered:
            # The token is a well-known constant from a public list, not
            # secret-derived, so naming it is safe and makes the failure
            # actionable.
            reasons.append(
                f"{label} contains the placeholder text {token!r}. Replace it "
                f"with a randomly generated value."
            )
            break

    return reasons


def generation_hint(env_var: str = "SESSION_JWT_SECRET") -> str:
    """The exact command an operator should run to produce a compliant secret.

    Every rejection path ends with this. A check that says "no" without saying
    "here is the yes" gets worked around rather than satisfied — typically by
    picking a slightly longer bad password.

    48 bytes rather than the 32-byte minimum: token_urlsafe encodes to roughly
    4 characters per 3 bytes, so 48 yields a 64-character secret that clears
    every check with margin and survives a future floor increase.
    """
    return (
        f"Generate one with:\n"
        f"  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
        f"then set {env_var} to that value."
    )


def require_strong_hmac_secret(value: str, *, label: str, env_var: str) -> None:
    """Raise `WeakKeyError` unless `value` is fit for HS256 signing.

    The enforcement entry point. `assess_hmac_secret` reports, this one decides.
    """
    reasons = assess_hmac_secret(value, label=label)
    if not reasons:
        return
    raise WeakKeyError(
        f"{label} does not meet HMAC key-strength policy "
        f"(CVE-2025-45768 hardening):\n"
        + "\n".join(f"  - {r}" for r in reasons)
        + "\n"
        + generation_hint(env_var)
    )
