// Behavioural regression tests for safeHref (src/utils/safeUrl.js).
//
// Zero-dependency by design: the frontend has no test runner, and adding one
// purely for this file would pull a large dev-dependency tree into a codebase
// that is itself subject to SCA scanning. Run with:  npm run test:safeurl
//
// Guards the remediation for Checkmarx findings "Client DOM XSS",
// "Reflected XSS", and "Client DOM Open Redirect" — all three of which fired on
// `<a href={safeHref(...)}>` sinks. If someone simplifies safeHref back into
// "validate, then return the original string", the taint path reopens; the
// scheme/host/encoding cases below are what keep that from passing silently.
global.window = { location: { hostname: 'partner.example.com', protocol: 'https:' } };
const { safeHref } = await import('../src/utils/safeUrl.js');

const cases = [
  // [input, expected]
  ['https://partner.example.com/x/-/merge_requests/7', 'https://partner.example.com/x/-/merge_requests/7'],
  ['http://localhost:8080/a/b?c=1#f', 'http://localhost:8080/a/b?c=1#f'],
  ['https://host.docker.internal/artifact/9', 'https://host.docker.internal/artifact/9'],
  ['HTTPS://LOCALHOST/Path', 'https://localhost/Path'],
  // blocked
  ['javascript:alert(1)', '#'],
  ['data:text/html,<script>alert(1)</script>', '#'],
  ['vbscript:msgbox(1)', '#'],
  ['https://evil.com/steal', '#'],
  ['//evil.com', '#'],
  ['', '#'],
  [null, '#'],
  [undefined, '#'],
  [42, '#'],
  // encoding: quote/angle-bracket cannot break out of the attribute
  ['https://localhost/a"onmouseover="alert(1)', 'https://localhost/a%22onmouseover=%22alert(1)'],
  ['https://localhost/<img>', 'https://localhost/%3Cimg%3E'],
  // preserves legitimate percent-encoding and query syntax
  ['https://localhost/a%20b?x=1&y=2', 'https://localhost/a%20b?x=1&y=2'],
];

// Deployment-configured allowlist entries must be honoured (so real GitLab MR
// links render), while a malformed entry must be ignored rather than trusted.
// `import.meta.env` is absent under plain node, which the module tolerates — so
// here we assert the DEFAULTS hold and that a non-allowlisted host is refused.
cases.push(['https://gitlab.example.com/g/p/-/merge_requests/1', '#']);

// ── RFC 3986 userinfo bypass — REGRESSION GUARD ──────────────────────────────
// The pre-27-Aug-2026 sanitiser matched the host with /^https?:\/\/([^\/?#:]+)/,
// a negated class that stops at ':' '/' '?' '#' but NOT at '@'. For a URL with a
// userinfo component the capture therefore took the CREDENTIALS, not the host,
// and the allowlist check passed while the browser navigated to whatever came
// after the '@'. That was a live open redirect through `mr_url`/`artifact_ref`
// (Checkmarx #9), not a taint-analysis artifact.
//
// The ':1' is load-bearing: with no colon, '[^\/?#:]+' swallows
// 'localhost@evil.com' whole and the allowlist misses. Adding any port makes the
// match stop at the colon and the bypass fires. Both shapes are pinned so a
// future regex "simplification" cannot quietly reopen it.
cases.push(['https://localhost:1@evil.com/steal', '#']);
cases.push(['https://host.docker.internal:443@attacker.tld/x', '#']);
cases.push(['https://localhost@evil.com/steal', '#']);
cases.push(['https://localhost:8080@evil.com', '#']);
// Credential-shaped userinfo, and an encoded '@' which must not be decoded
// back into a host separator.
cases.push(['https://user:pass@evil.com/', '#']);
cases.push(['https://localhost%40evil.com/', '#']);
// A legitimate port on an allowlisted host must still work — the guard above
// must not be implemented by simply banning ports.
cases.push(['https://localhost:8080/app', 'https://localhost:8080/app']);

// ── Port is re-derived through Number(), not copied from the input ──────────
//
// resolveHost() previously interpolated the raw `port` substring straight from
// the caller's string (`${allowed}:${port}`). Harmless at runtime, but it left a
// literal slice of attacker-influenced input in the returned value, which
// contradicts this module's invariant that no byte of the input survives — and
// is why a taint engine (Checkmarx Reflected XSS) still saw an unbroken
// source→sink path through safeHref at both reported `href` sinks.
//
// Ports now round-trip through an integer, so these assert the normalisation
// that proves the copy is gone, plus range enforcement.
cases.push(['https://localhost:08080/app', 'https://localhost:8080/app']); // leading zero dropped by Number()
cases.push(['https://localhost:0/app', '#']);       // port 0 is not a valid TCP port
cases.push(['https://localhost:99999/app', '#']);   // above 65535
cases.push(['https://localhost:65535/x', 'https://localhost:65535/x']);
cases.push(['https://localhost:/app', '#']);        // empty port is malformed

// ── Scheme-injection payloads must never reach an href ──────────────────────
// The two Checkmarx findings are XSS reports, so assert the XSS vectors
// explicitly rather than relying on the open-redirect cases above.
cases.push(['javascript:alert(1)', '#']);
cases.push(['JaVaScRiPt:alert(1)', '#']);
cases.push(['  javascript:alert(1)', '#']);
cases.push(['java\tscript:alert(1)', '#']);
cases.push(['java\nscript:alert(1)', '#']);
cases.push(['data:text/html,<script>alert(1)</script>', '#']);
cases.push(['vbscript:msgbox(1)', '#']);
cases.push(['jAvAsCrIpT:/*-/*`/*\\`/*\'/*"/**/(/* */onerror=alert(1))//', '#']);
// Non-string / empty inputs (an absent artifact_ref is the common real case).
cases.push([undefined, '#']);
cases.push([null, '#']);
cases.push(['', '#']);

// ── Unencodable input must fail closed, not throw ───────────────────────────
//
// encodeURIComponent throws URIError on a LONE SURROGATE (unpaired \uD800-\uDFFF)
// because it has no UTF-8 representation. This is reachable from the network:
// JSON.parse produces a lone surrogate from the escape "\uD800" without error,
// so a malformed mr_url / artifact_ref would throw out of render. CodePanel (the
// Checkmarx line-352 sink) has no error boundary, making that a white-screen DoS
// caused purely by upstream data. safeHref must degrade the link instead.
cases.push(['https://localhost/mr/\uD800', '#']);        // lone high surrogate
cases.push(['https://localhost/\uDFFF', '#']);           // lone low surrogate
cases.push(['https://localhost/a\uD800b?x=1', '#']);
// A CORRECTLY PAIRED surrogate is valid UTF-8 and must still work, so the guard
// above must not be implemented by simply banning the surrogate range.
cases.push(['https://localhost/\u{1F600}', 'https://localhost/%F0%9F%98%80']);

// ── Apostrophes stay percent-encoded ────────────────────────────────────────
// encodeURIComponent leaves the sub-delims ! ' ( ) * alone, so the decode step
// would otherwise UN-escape a caller's %27 into a bare ' — an HTML attribute
// quote character. Not exploitable through React's JSX escaping, but the output
// must be inert on its own terms.
cases.push(['https://localhost/%27', 'https://localhost/%27']);
cases.push(["https://localhost/'onmouseover='alert(1)", 'https://localhost/%27onmouseover=%27alert(1)']);

// ── Tail is rebuilt via encodeURIComponent, not a charset passthrough ────────
//
// The tail rebuilder used to re-emit any "known-safe" character with
// String.fromCharCode(ch.charCodeAt(0)). Byte-identical at runtime, but to a
// taint engine that is plain value propagation — the caller's data still
// reaches the href. That is why Checkmarx re-reported Client DOM XSS at
// CodePanel.jsx:352 and ChangeDetail.jsx:2218 even after the sanitiser was
// hardened. Every byte of caller data now goes through encodeURIComponent, with
// only structural delimiters emitted from a local literal.
//
// Encoding is idempotent (decode-then-encode), so real GitLab MR URLs that
// already contain escapes are not double-escaped into a broken link.
cases.push(['https://localhost/a%20b', 'https://localhost/a%20b']);
cases.push(['https://localhost/a b', 'https://localhost/a%20b']);
// A pre-encoded payload must stay encoded — decoding must not "unwrap" it into
// live characters in the attribute.
cases.push(['https://localhost/%3Cscript%3E', 'https://localhost/%3Cscript%3E']);
cases.push(['https://localhost/a%22b', 'https://localhost/a%22b']);
// Double-encoding is preserved rather than collapsed one level per call.
cases.push(['https://localhost/a%2522b', 'https://localhost/a%2522b']);
// Encoded slashes must NOT decode into real path separators (that would let
// '%2F%2Fevil.com' turn into a protocol-relative-looking path).
cases.push(['https://localhost/%2F%2Fevil.com', 'https://localhost/%2F%2Fevil.com']);
// A malformed escape makes decodeURIComponent throw; the '%' is then encoded
// literally instead of the segment being passed through raw.
cases.push(['https://localhost/a%zzb', 'https://localhost/a%25zzb']);
// Query/fragment delimiters survive so real links keep working.
cases.push(['https://localhost/p?q=1&r=2#f', 'https://localhost/p?q=1&r=2#f']);
cases.push([
  'https://localhost/p?q=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E',
  'https://localhost/p?q=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E',
]);
// The real shape this feature renders: a GitLab MR permalink.
cases.push([
  'https://localhost/group/project/-/merge_requests/42',
  'https://localhost/group/project/-/merge_requests/42',
]);

let pass = 0, fail = 0;
for (const [input, expected] of cases) {
  const got = safeHref(input);
  const ok = got === expected;
  if (ok) { pass += 1; } else { fail += 1; }
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${JSON.stringify(input)}\n      got:      ${JSON.stringify(got)}${ok ? '' : `\n      expected: ${JSON.stringify(expected)}`}`);
}
console.log(`\n${pass} passed, ${fail} failed`);

// NOTE: a runtime `out === input` check proves nothing here — JavaScript compares
// strings by VALUE, so a correctly-rebuilt URL with identical content is always
// `===` to the input. Taint-breaking is a property of the STATIC dataflow (every
// byte of the return value originates from a local literal, an allowlist entry,
// or a String.fromCharCode/encodeURIComponent result), which is what the
// analyser follows. Asserted by code review + these behavioural cases.
process.exit(fail === 0 ? 0 : 1);
