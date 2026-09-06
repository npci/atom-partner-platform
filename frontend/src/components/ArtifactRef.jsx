// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState } from 'react';
import { Copy, Check } from 'lucide-react';

/**
 * Renders a blocker resolution's `artifact_ref` as selectable TEXT, never as a
 * link.
 *
 * ── Why this is not an <a href> ──────────────────────────────────────────────
 *
 * `artifact_ref` arrives from a partner agent over A2A. It has no schema and is
 * not validated anywhere on the way in, and in practice it is usually an opaque
 * identifier rather than a web address — the canonical example in our own test
 * fixtures is `doc://patch.pdf`. Other observed shapes are `s3://bucket/x.pdf`
 * and bare document references like `CR-1024/evidence.pdf`.
 *
 * None of those are http(s) URLs, so the previous `<a href={safeHref(...)}>`
 * rendered every one of them as a link to '#' — a control that looks clickable,
 * is not, and hides the actual reference behind link styling. Showing the value
 * as text is both truthful and more useful: the operator can read it and copy
 * it into whichever system actually resolves it.
 *
 * ── Security consequence ─────────────────────────────────────────────────────
 *
 * Removing the `href` deletes the sink. Checkmarx reported this one line under
 * three separate queries — Client DOM XSS, Reflected XSS, and Client DOM Open
 * Redirect (path 2 of each, ChangeDetail.jsx:2218) — all tracing the same
 * `data` → `href` dataflow out of `getChange` in api.js. With no `href` there is
 * no dataflow for those queries to follow, so all three paths are eliminated at
 * the source rather than filtered at the sink.
 *
 * React escapes text children, so interpolating the raw value here cannot
 * inject markup. A `javascript:` payload in this field now renders as the
 * literal, inert string `javascript:...` instead of becoming a navigable
 * target.
 */
export default function ArtifactRef({ value, tone = 'default' }) {
  const [copied, setCopied] = useState(false);

  if (!value) return null;
  const text = String(value);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch (_e) { /* clipboard blocked (insecure origin / permission) — silent */ }
  };

  const color = tone === 'resolution' ? '#155724' : 'var(--text-secondary)';

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, maxWidth: '100%' }}>
      <span style={{ color: 'var(--text-muted)', flexShrink: 0 }}>Artifact:</span>
      {/* `user-select: all` makes a single click select the whole reference,
          which is the main thing an operator wants to do with it. */}
      <code
        title={text}
        style={{
          fontFamily: 'monospace',
          color,
          userSelect: 'all',
          overflowWrap: 'anywhere',
          minWidth: 0,
        }}
      >
        {text}
      </code>
      <button
        type="button"
        onClick={onCopy}
        title="Copy artifact reference"
        aria-label="Copy artifact reference"
        style={{
          display: 'inline-flex', alignItems: 'center', flexShrink: 0,
          background: 'none', border: 'none', padding: 2, cursor: 'pointer',
          color: copied ? '#155724' : 'var(--text-muted)', lineHeight: 0,
        }}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
      </button>
    </span>
  );
}
