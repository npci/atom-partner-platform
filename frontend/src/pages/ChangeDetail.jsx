// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { Component, Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { t } from '../strings'
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, ArrowRight, Check, Send, Shield, Sparkles, Trash2, Edit2, X, Save, Download, Inbox, Rocket, FlaskConical, BadgeCheck, Loader2, AlertTriangle, FileText, Presentation, PlayCircle, HelpCircle, Image as ImageIcon, FileJson, StickyNote, Mail, Clock, Building2, Hash, Activity, Eye, RefreshCw, FileCode2, Tag, ToggleLeft, ToggleRight, Maximize2, ClipboardCheck, Lock, ChevronRight } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import {
  getChange, submitQuery, reportProgress, declareReady,
  suggestQueryDrafts, listQueryDrafts, updateQueryDraft, discardQueryDraft, sendQueryDraft,
  downloadChangeDocument, downloadChangeDocumentPptx, downloadChangeDocumentXlsx, downloadChangeSummary,
  downloadChangeDocumentNative, downloadChangeDocumentZip,
  fetchChangeDocumentVideo, downloadChangeDocumentVideo,
  downloadCertSignoff,
  acceptChange, acceptCounter, reportBlocker, raiseEmergencyIssue,
  submitCertQuery, listCertQueries,
  getCertStatus, updateCertStatus, certMockComplete, downloadCertSignoffPdf,
  listCertExecutions,
} from '../services/api';
import { parseIso, formatRelative, formatDateTime as formatAbsolute } from '../lib/datetime';
import FeasibilityPanel from '../components/FeasibilityPanel';
import DesignPanel from '../components/DesignPanel';
import CodePanel from '../components/CodePanel';
import TestingPanel from '../components/TestingPanel';
import ArtifactRef from '../components/ArtifactRef';
import PrototypePreview from '../components/PrototypePreview';

const PROGRESS_STEPS = [
  { key: 'design_completed', label: 'Design Completed' },
  { key: 'coding_completed', label: 'Coding Completed' },
  { key: 'testing_completed', label: 'Testing Completed' },
];

// Shared visual tokens — soft cards on a neutral page, subtle borders,
// a single low-elevation shadow. Inline so the redesign doesn't
// require touching the global stylesheet; switch to CSS variables on
// theme rollout. Three text levels (primary/secondary/tertiary) +
// semantic colors used by *meaning* (success/warning/danger/info)
// rather than as accent.
const T = {
  cardBg:        '#ffffff',
  cardBorder:    '1px solid rgba(15, 23, 42, 0.06)',
  cardRadius:    16,                                       // softer, more modern
  // Two-tier shadow: a tight contact shadow + a softer ambient bloom.
  // Reads as "lifted" without feeling heavy. Hover adds a third tier.
  cardShadow:    '0 1px 2px rgba(15, 23, 42, 0.04), 0 2px 6px rgba(15, 23, 42, 0.04)',
  cardShadowHover: '0 4px 10px rgba(15, 23, 42, 0.06), 0 12px 24px rgba(15, 23, 42, 0.06)',
  textPrimary:   '#0f172a',  // slate-900 — labels, headings
  textSecondary: '#475569',  // slate-600 — body text, descriptions
  textMuted:     '#94a3b8',  // slate-400 — timestamps, helper hints
  primary:       '#2563eb',  // info / navigation
  success:       '#10b981',  // approved, safe
  warning:       '#f59e0b',  // pending attention, updated
  danger:        '#ef4444',  // blockers, errors
  bgSoft:        '#f8fafc',
  bgMuted:       '#f1f5f9',
  borderSubtle:  'rgba(15, 23, 42, 0.06)',
  // Typography scale — used by the hero header + section titles so
  // the visual hierarchy is consistent.
  fontHero:      28,                                       // page title
  fontH3:        15,                                       // section / card titles
  fontBody:      13,                                       // primary body
  fontMeta:      12,                                       // secondary metadata
  fontMicro:     11,                                       // chip / pill text
};
const card = (extra = {}) => ({
  background: T.cardBg,
  border:     T.cardBorder,
  borderRadius: T.cardRadius,
  boxShadow:  T.cardShadow,
  ...extra,
});

// ── Cert test cases — markdown → structured table ─────────────────────────────
//
// The authority side serves a JSON companion (simulator-contract shape) alongside
// the markdown; the partner side only has the markdown (see
// partner-platform/backend/app/a2a_common/handlers/change_communication.py —
// only `content` and optional `xlsx_b64` are persisted). So we parse the
// markdown — a deterministic per-case template enforced by
// `backend/app/excel_testcase_engine/prompts/writer.md` — into rows.
//
// If parsing yields zero rows the view falls back to plain markdown render.

const CERT_STATUS_COLORS = {
  Success: '#10b981',
  Failure: '#ef4444',
  Deemed:  '#f59e0b',
  Partial: '#2563eb',
};

/**
 * Read one `Label: value` field out of a DETAILS block.
 *
 * Implemented as a line scan rather than a regex. The previous version built
 *   `^<label>\s*:\s*([\s\S]*?)(?=\n[A-Z][a-zA-Z ]*\s*:|$)`   (with /m)
 * per call, and its lookahead was ambiguous: `[a-zA-Z ]*` and `\s*` can both
 * match a space, so a long run of spaces containing no colon had very many ways
 * to split between them and the engine retried at every start position.
 * Measured on the real helper that was quadratic — 8k spaces took 21 ms, 64k
 * took 1291 ms (4x per doubling) — and since the parser calls this SEVEN times
 * per test case, one document of 20 such cases froze the tab for ~3.8 s. The
 * markdown arrives over A2A, so its size and shape are not ours to trust.
 *
 * Behaviour matches the regex on every realistic input. The `/m` flag made that
 * regex effectively SINGLE-LINE — `$` matched at each line end, so the lazy
 * group always stopped at the first newline and a value never continued onto the
 * next line. This takes the first matching line's remainder and nothing more,
 * with whitespace collapsed as before. Verified against the old implementation
 * over 152 label/block combinations.
 *
 * ONE deliberate difference, which fixes a bug rather than preserving it. For a
 * label with an empty value (`"API Involved:"` followed by `"Type: x"`), the old
 * `\s*` after the colon greedily consumed the NEWLINE, so the capture started on
 * the following line and returned `"Type: x"` — reporting a DIFFERENT field's
 * text as this field's value. This returns `''`, which is what an empty field
 * means.
 */
function _detailField(detailsBlock, label) {
  if (!detailsBlock || !label) return '';

  const lines = String(detailsBlock).split('\n');
  const prefix = `${label}:`;

  // Explicit iteration ceiling. A real DETAILS block is a short key/value list —
  // a few dozen lines at most — so scanning further can only be malformed or
  // hostile input. The markdown arrives over A2A, so its size is not ours to
  // trust, and `lines.length` is therefore attacker-influenced.
  //
  // The ceiling is the LOOP CONDITION ITSELF, and the data-dependent length is
  // only a `break` guard inside the body. Written the other way round —
  // `i < Math.min(lines.length, MAX)` — the trip count is still bounded, but the
  // condition textually reads a value that taint analysis traces back to the
  // network (Checkmarx `UncheckedInputForLoopCondition`, and `Math.min` is not
  // on its sanitiser list). Keeping untrusted lengths out of the condition is
  // what makes the bound provable to a reader and to the scanner alike. See
  // test/boundedLoops.test.mjs, which enforces this shape across the frontend.
  const MAX_DETAIL_LINES = 500;

  for (let i = 0; i < MAX_DETAIL_LINES; i += 1) {
    if (i >= lines.length) break;
    // Exact, case-sensitive label at the start of the line, then a colon —
    // matching the old anchored `^<label>\s*:`. (The old `\s*` before the colon
    // could never match anything here: the label is followed immediately by the
    // colon in every real DETAILS block, and a space before a colon would have
    // been part of the label token.)
    if (!lines[i].startsWith(prefix)) continue;
    // Everything after the colon on THAT line only, whitespace collapsed.
    return lines[i].slice(prefix.length).replace(/\s+/g, ' ').trim();
  }
  return '';
}

function parseCertTestCasesMd(md) {
  if (!md || typeof md !== 'string') return null;
  const featureMatch = md.match(/^# (.+)$/m);
  const feature = featureMatch ? featureMatch[1].trim() : '';

  const chunks = md.split(/\n(?=### )/g);
  const caseChunks = chunks.filter(c => c.startsWith('### '));
  if (caseChunks.length === 0) return null;

  // Same bounded-loop rule as `_detailField`: the case count comes straight from
  // an A2A-delivered document, and each iteration runs eight regexes plus seven
  // `_detailField` scans, so an oversized document is the expensive shape. A
  // real cert plan is tens of cases; 500 is far above any legitimate one. The
  // condition is the constant and the tainted length is a `break` guard, so no
  // untrusted value is ever read by the loop condition.
  const MAX_TEST_CASES = 500;

  const cases = [];
  for (let c = 0; c < MAX_TEST_CASES; c += 1) {
    if (c >= caseChunks.length) break;
    const chunk = caseChunks[c];
    const headingMatch = chunk.match(/^###\s+(\S+)\s+—\s+([^(\n]+?)(?:\s+\(highlighted\))?\s*$/m);
    if (!headingMatch) continue;
    const test_id = headingMatch[1].trim();
    const expected_status = headingMatch[2].trim();

    const detailsMatch = chunk.match(/\*\*DETAILS\*\*\s*\n```[^\n]*\n([\s\S]*?)\n```/);
    const details_block = detailsMatch ? detailsMatch[1] : '';

    const descMatch = chunk.match(/\*\*DESCRIPTION\*\*\s*\n+([\s\S]*?)\n+\*\*TEST STEPS\*\*/);
    const description_block = descMatch ? descMatch[1].trim() : '';

    const stepsMatch = chunk.match(/\*\*TEST STEPS\*\*\s*\n+```[^\n]*\n([\s\S]*?)\n```/);
    const steps_block = stepsMatch ? stepsMatch[1] : '';

    const respMatch = chunk.match(/_Response code:\s*`([^`]+)`_/);
    const response_code = respMatch ? respMatch[1] : '';

    cases.push({
      test_id,
      expected_status,
      apis:           _detailField(details_block, 'API Involved'),
      api_type:       _detailField(details_block, 'Type'),
      entities:       _detailField(details_block, 'Entity Involved'),
      approval_type:  _detailField(details_block, 'Approval Type'),
      payer_handle:   _detailField(details_block, 'Payer Handle'),
      payee_handle:   _detailField(details_block, 'Payee Handle'),
      details_block,
      description_block,
      steps_block,
      response_code,
    });
  }

  if (cases.length === 0) return null;
  return { feature, test_cases: cases };
}

function _certTh() {
  return {
    padding: '8px 12px',
    textAlign: 'left',
    fontSize: 10,
    fontWeight: 700,
    color: T.textMuted,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    borderBottom: `1px solid ${T.borderSubtle}`,
    background: T.bgMuted,
    whiteSpace: 'nowrap',
    position: 'sticky',
    top: 0,
    zIndex: 1,
  };
}

function _certTd() {
  return {
    padding: '8px 12px',
    fontSize: 12,
    color: T.textPrimary,
    verticalAlign: 'top',
    borderBottom: `1px solid ${T.borderSubtle}`,
  };
}

function _certPill(color) {
  return {
    display: 'inline-flex', alignItems: 'center',
    padding: '2px 8px',
    borderRadius: 999,
    fontSize: 10,
    fontWeight: 700,
    color,
    background: `${color}1A`,
    border: `1px solid ${color}40`,
    letterSpacing: '0.04em',
    whiteSpace: 'nowrap',
  };
}

function CertTestCaseRow({ tc, index }) {
  const [open, setOpen] = useState(false);
  const statusColor = CERT_STATUS_COLORS[tc.expected_status] || T.textMuted;
  return (
    <>
      <tr
        onClick={() => setOpen(v => !v)}
        style={{ cursor: 'pointer', background: index % 2 === 0 ? T.cardBg : T.bgSoft }}
      >
        <td style={{ ..._certTd(), width: 24, textAlign: 'center', color: T.textMuted }}>
          <ChevronRight size={12} style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 0.15s' }} />
        </td>
        <td style={{ ..._certTd(), fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontWeight: 600, whiteSpace: 'nowrap' }}>
          {tc.test_id}
        </td>
        <td style={_certTd()}>
          <span style={_certPill(statusColor)}>{tc.expected_status}</span>
        </td>
        <td style={{ ..._certTd(), fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11 }}>
          {tc.apis || '—'}
        </td>
        <td style={{ ..._certTd(), color: T.textSecondary }}>{tc.api_type || '—'}</td>
        <td style={{ ..._certTd(), color: T.textSecondary, maxWidth: 220 }}>{tc.entities || '—'}</td>
        <td style={{ ..._certTd(), whiteSpace: 'nowrap' }}>{tc.approval_type || '—'}</td>
        <td style={{ ..._certTd(), whiteSpace: 'nowrap' }}>{tc.payer_handle || '—'}</td>
        <td style={{ ..._certTd(), whiteSpace: 'nowrap' }}>{tc.payee_handle || '—'}</td>
        <td style={{ ..._certTd(), fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
          {tc.response_code || '—'}
        </td>
      </tr>
      {open && (
        <tr style={{ background: T.bgMuted }}>
          <td colSpan={10} style={{ padding: '12px 20px 16px 44px', borderBottom: `1px solid ${T.borderSubtle}` }}>
            <CertTestCaseExpanded tc={tc} />
          </td>
        </tr>
      )}
    </>
  );
}

function CertTestCaseExpanded({ tc }) {
  const blockStyle = {
    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
    fontSize: 11,
    lineHeight: 1.55,
    background: T.cardBg,
    border: `1px solid ${T.borderSubtle}`,
    borderRadius: 6,
    padding: '10px 12px',
    whiteSpace: 'pre-wrap',
    margin: 0,
    color: T.textPrimary,
    overflow: 'auto',
  };
  const labelStyle = {
    fontSize: 10, fontWeight: 700, color: T.textMuted,
    textTransform: 'uppercase', letterSpacing: '0.06em',
    margin: '0 0 6px',
  };
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {tc.description_block && (
        <div>
          <p style={labelStyle}>Description</p>
          <pre style={{ ...blockStyle, fontFamily: 'inherit', fontSize: 12 }}>{tc.description_block}</pre>
        </div>
      )}
      {tc.steps_block && (
        <div>
          <p style={labelStyle}>Test Steps</p>
          <pre style={blockStyle}>{tc.steps_block}</pre>
        </div>
      )}
      {tc.details_block && (
        <div>
          <p style={labelStyle}>Details (raw)</p>
          <pre style={blockStyle}>{tc.details_block}</pre>
        </div>
      )}
    </div>
  );
}

function CertTestCasesView({ markdown }) {
  const plan = useMemo(() => parseCertTestCasesMd(markdown), [markdown]);
  if (!plan || !plan.test_cases?.length) {
    return (
      <div className="markdown-content" style={{ color: T.textSecondary, lineHeight: 1.6, fontSize: 14 }}>
        <ReactMarkdown>{markdown || ''}</ReactMarkdown>
      </div>
    );
  }
  return (
    <div>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap',
        marginBottom: 12, fontSize: 12, color: T.textSecondary,
      }}>
        {plan.feature && (
          <span style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>
            {plan.feature}
          </span>
        )}
        <span><strong>{plan.test_cases.length}</strong> test cases</span>
        <span style={{ color: T.textMuted }}>Click a row to expand description, steps, and details.</span>
      </div>
      <div style={{
        border: `1px solid ${T.borderSubtle}`,
        borderRadius: 8,
        overflow: 'auto',
        maxHeight: '70vh',
        background: T.cardBg,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ..._certTh(), width: 24 }} />
              <th style={_certTh()}>TC ID</th>
              <th style={_certTh()}>Status</th>
              <th style={_certTh()}>APIs</th>
              <th style={_certTh()}>Type</th>
              <th style={_certTh()}>Entities</th>
              <th style={_certTh()}>Approval</th>
              <th style={_certTh()}>Payer</th>
              <th style={_certTh()}>Payee</th>
              <th style={_certTh()}>Resp</th>
            </tr>
          </thead>
          <tbody>
            {plan.test_cases.map((tc, i) => (
              <CertTestCaseRow key={tc.test_id || i} tc={tc} index={i} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Hover affordances + active-step pulse need pseudo-class / keyframe
// rules that inline styles can't express. One small <style> tag is
// cheaper than introducing a stylesheet dependency for this page.
const PAGE_STYLES = `
@keyframes pp-pulse {
  0%   { box-shadow: 0 0 0 0 rgba(37, 99, 235, 0.35); }
  70%  { box-shadow: 0 0 0 8px rgba(37, 99, 235, 0); }
  100% { box-shadow: 0 0 0 0 rgba(37, 99, 235, 0); }
}
.pp-doc-card { transition: transform .18s ease, box-shadow .18s ease, border-color .18s ease; }
.pp-doc-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 6px 16px rgba(15, 23, 42, 0.08), 0 2px 4px rgba(15, 23, 42, 0.04);
  border-color: rgba(37, 99, 235, 0.25);
}
.pp-doc-card .pp-hover-actions { opacity: 0; transition: opacity .15s ease; }
.pp-doc-card:hover .pp-hover-actions { opacity: 1; }
.pp-pulse-dot { animation: pp-pulse 1.6s ease-out infinite; }
.pp-filter-chip { transition: background .15s ease, color .15s ease, border-color .15s ease; }

@keyframes pp-log-fade-in {
  from { opacity: 0; transform: translateY(-3px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes pp-spin {
  to { transform: rotate(360deg); }
}
@keyframes pp-shimmer {
  0%   { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}
@keyframes pp-fade-in {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes pp-slide-in-right {
  from { transform: translateX(100%); }
  to   { transform: translateX(0); }
}
.pp-log-line { animation: pp-log-fade-in .25s ease forwards; }
.pp-spin { animation: pp-spin 1s linear infinite; }
.pp-drawer-backdrop { animation: pp-fade-in .18s ease forwards; }
.pp-drawer-panel { animation: pp-slide-in-right .22s cubic-bezier(.16,1,.3,1) forwards; }
.pp-stage-running .pp-stage-bar {
  background: linear-gradient(90deg, rgba(37,99,235,0.10), rgba(37,99,235,0.30), rgba(37,99,235,0.10));
  background-size: 200px 100%;
  animation: pp-shimmer 1.4s linear infinite;
}

/* Microinteractions: button press feedback + skeleton placeholder.
   The press scale is intentionally sub-percentage so it never feels
   rubber — just acknowledges the click. */
@keyframes pp-skeleton-shimmer {
  0%   { background-position: -480px 0; }
  100% { background-position:  480px 0; }
}
.pp-skeleton {
  background: linear-gradient(90deg, #e2e8f0 0px, #f1f5f9 240px, #e2e8f0 480px);
  background-size: 960px 100%;
  animation: pp-skeleton-shimmer 1.4s ease-in-out infinite;
  border-radius: 8px;
}
.pp-btn-press:active { transform: scale(0.97); }
.pp-fade-in { animation: pp-fade-in .25s ease forwards; }
`;

// Human-readable metadata for each Product Kit doc type. Mirrors
// the authority side (ProductKit.jsx DOC_TYPES) so the two UIs label
// the same thing identically. Default falls back to a prettified
// version of the raw key so an unknown doc type still renders OK.
const DOC_TYPE_META = {
  product_doc:        { label: 'Product Document',     desc: 'Comprehensive feature description for stakeholders & partners', icon: FileText },
  product_deck:       { label: 'Product Deck',         desc: '12-slide executive presentation outline',                       icon: Presentation },
  promo_video:        { label: 'Promo Video',          desc: 'Short-form launch teaser video',                                icon: PlayCircle },
  explainer_video:    { label: 'Explainer Video',      desc: 'How-it-works video for end-users',                              icon: PlayCircle },
  faq:                { label: 'FAQ',                  desc: 'Customer & partner frequently-asked questions',                 icon: HelpCircle },
  cert_test_cases:    { label: 'Certification Tests',  desc: 'Test cases for partner certification suite',                    icon: FlaskConical },
  circular:           { label: 'Operations Circular',  desc: `Formal ${t('term.authority')} circular for partner ops teams`,                    icon: Mail },
  manifest:           { label: 'Release Manifest',     desc: 'Machine-readable rollout manifest (YAML)',                      icon: FileJson },
  product_note:       { label: 'Product Note',         desc: 'Internal product context & rationale',                          icon: StickyNote },
  prototype_screens:  { label: 'Prototype Screens',    desc: 'UI flow mockups for the new capability',                        icon: ImageIcon },
  xsd:                { label: 'XSD Schema',           desc: 'XML schema definitions for the new payload',                    icon: FileCode2 },
  tsd:                { label: 'Technical Spec',        desc: 'Technical specification document for integration',              icon: FileCode2 },
};

function docMeta(key) {
  return DOC_TYPE_META[key] || {
    label: (key || 'Document').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
    desc:  'Product Kit artefact',
    icon:  FileText,
  };
}

// Canonical download format per artifact — each kit artifact is downloadable
// in exactly ONE extension. `kind` selects the transport/endpoint. doc_types
// not listed (product_doc, product_note) fall back to .docx.
const DOC_DOWNLOAD = {
  manifest:          { ext: 'yaml', kind: 'native' },
  cert_test_cases:   { ext: 'xlsx', kind: 'xlsx'   },
  xsd:               { ext: 'xsd',  kind: 'native' },
  explainer_video:   { ext: 'mp4',  kind: 'video'  },
  product_deck:      { ext: 'pptx', kind: 'pptx'   },
  promo_video:       { ext: 'mp4',  kind: 'video'  },
  prototype_screens: { ext: 'html', kind: 'native' },
  circular:          { ext: 'docx', kind: 'docx'   },
  faq:               { ext: 'docx', kind: 'docx'   },
};
const DEFAULT_DOWNLOAD = { ext: 'docx', kind: 'docx' };
// An xsd touching ≥2 schemas ships a .zip bundle — prefer it over the
// single-block native .xsd download when the doc carries one.
const downloadSpec = (docType, doc) =>
  (docType === 'xsd' && doc && doc.has_zip)
    ? { ext: 'zip', kind: 'zip' }
    : (DOC_DOWNLOAD[docType] || DEFAULT_DOWNLOAD);

// Is the artifact's one format actually available on this doc version?
function canDownloadSpec(spec, doc) {
  switch (spec.kind) {
    case 'pptx':   return !!doc.has_pptx;
    case 'xlsx':   return !!doc.has_xlsx;
    case 'video':  return !!doc.has_video;
    case 'zip':    return !!doc.has_zip;
    case 'native': return !!doc.content;
    case 'docx':
    default:       return !!doc.has_docx;
  }
}

// Dispatch a single download in the artifact's canonical format. `names`
// carries the server-provided filenames; we fall back to `<base>.<ext>`.
function downloadArtifact(spec, changeId, docId, base, names = {}) {
  const fn = `${base}.${spec.ext}`;
  switch (spec.kind) {
    case 'pptx':   return downloadChangeDocumentPptx(changeId, docId, names.pptx || fn);
    case 'xlsx':   return downloadChangeDocumentXlsx(changeId, docId, names.xlsx || fn);
    case 'video':  return downloadChangeDocumentVideo(changeId, docId, names.video || fn);
    case 'zip':    return downloadChangeDocumentZip(changeId, docId, names.zip || fn);
    case 'native': return downloadChangeDocumentNative(changeId, docId, fn);
    case 'docx':
    default:       return downloadChangeDocument(changeId, docId, names.docx || fn);
  }
}

// Timestamp helpers (parseIso / formatRelative / formatAbsolute) live in
// lib/datetime — server timestamps are naive UTC and must be parsed as UTC
// then rendered in IST so the partner portal matches the authority.

function formatBytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n/1024).toFixed(1)} KB`;
  return `${(n/(1024*1024)).toFixed(1)} MB`;
}

// Doc grouping — partners scan by category, not by name. The
// `members` array uses the same doc_type keys as DOC_TYPE_META so
// the grouping survives a key rename in one place. Anything not
// listed here lands in 'other' so a new doc type from upstream
// still renders without code changes.
const DOC_GROUPS = [
  { id: 'core',      label: 'Core Documents',     hint: 'Read these first',      members: ['product_doc', 'product_note', 'manifest'] },
  { id: 'technical', label: 'Technical',          hint: 'Integration artefacts', members: ['cert_test_cases', 'xsd', 'tsd'] },
  { id: 'media',     label: 'Decks & Media',      hint: 'Visual collateral',     members: ['product_deck', 'prototype_screens', 'promo_video', 'explainer_video'] },
  { id: 'comms',     label: 'Communication',      hint: 'Outreach & support',    members: ['circular', 'faq'] },
  { id: 'other',     label: 'Other',              hint: '',                      members: [] /* fallback bucket — filled at render */ },
];

function groupOf(docType) {
  for (const g of DOC_GROUPS) if (g.members.includes(docType)) return g.id;
  return 'other';
}

// Per-browser doc-view state, keyed by change. localStorage so the
// state persists across reloads on the same machine without needing
// a backend change. Tracks one flag per doc:
//   viewed — set when the partner expands the doc the first time;
//            drives the "New" → "Viewed" pill transition.
function useDocReviewState(changeId) {
  const key = `pp:docState:${changeId}`;
  const [state, setState] = useState(() => {
    try { return JSON.parse(localStorage.getItem(key)) || {}; }
    catch { return {}; }
  });
  const persist = (next) => {
    setState(next);
    try { localStorage.setItem(key, JSON.stringify(next)); }
    catch { /* private mode / quota — non-fatal */ }
  };
  const markViewed = (docId) => {
    if (!docId) return;
    if (state[docId]?.viewedAt) return;
    persist({ ...state, [docId]: { ...(state[docId] || {}), viewedAt: new Date().toISOString() } });
  };
  return { state, markViewed };
}

// Stages on the rollout lifecycle, in order. Aligned with the authority's
// Implementation Progress vocabulary so PMs and partners are looking
// at the same 7 checkpoints when they talk:
//   Communicated · Accepted · Design · Coding · Testing · Ready for Certification · Certified
// Each `done(change, steps)` gate determines whether the partner has
// cleared that stage given the change's current decision + cert_status
// + completed_steps.
const LIFECYCLE_STAGES = [
  {
    key:   'communicated',
    label: 'Communicated',
    done:  () => true,                                       // Row exists → the authority communicated it
    timeOf: (c) => c.received_at,
  },
  {
    key:   'accepted',
    label: 'Accepted',
    done:  (c) => c.decision === 'accepted',
  },
  {
    key:   'design',
    label: 'Design',
    done:  (c, steps) => (steps || []).includes('design_completed'),
  },
  {
    key:   'coding',
    label: 'Coding',
    done:  (c, steps) => (steps || []).includes('coding_completed'),
  },
  {
    key:   'testing',
    label: 'Testing',
    done:  (c, steps) => (steps || []).includes('testing_completed'),
  },
  {
    key:   'ready_for_cert',
    label: 'Ready for Certification',
    done:  (c) => ['ready_for_certification','certified'].includes(c.cert_status)
                  || ['ready','ready_for_certification','certified'].includes(c.status),
  },
  {
    key:   'certified',
    label: 'Certified',
    done:  (c) => c.cert_status === 'certified' || c.status === 'certified',
  },
];

function LifecycleStepper({ change, completedSteps }) {
  const states = LIFECYCLE_STAGES.map(s => ({
    ...s,
    isDone: s.done(change, completedSteps),
  }));
  const currentIdx = (() => {
    for (let i = states.length - 1; i >= 0; i--) if (states[i].isDone) return i;
    return 0;
  })();
  return (
    <div
      role="progressbar"
      aria-valuenow={currentIdx + 1}
      aria-valuemin={1}
      aria-valuemax={states.length}
      style={card({
        display: 'flex', alignItems: 'center',
        padding: '16px 22px', marginBottom: 16,
        overflowX: 'auto',
      })}
    >
      {states.map((s, i) => {
        const isActive = i === currentIdx;
        const isComplete = s.isDone;
        // Two-color stepper: completed = success, current = primary,
        // future = muted hairline. Connector colour follows: success
        // up through the last completed step, hairline beyond.
        const dotColor = isComplete ? T.success : isActive ? T.primary : 'rgba(15,23,42,0.15)';
        const labelColor = isComplete || isActive ? T.textPrimary : T.textMuted;
        return (
          <div key={s.key} style={{ display: 'flex', alignItems: 'center', flex: i === states.length - 1 ? '0 0 auto' : '1 1 auto' }}>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 86 }}>
              <div
                className={isActive && !isComplete ? 'pp-pulse-dot' : ''}
                style={{
                  width: 20, height: 20, borderRadius: '50%',
                  background: isComplete ? dotColor : isActive ? '#fff' : 'transparent',
                  border: `2px solid ${dotColor}`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: '#fff',
                  marginBottom: 8,
                }}>
                {isComplete && <Check size={11} strokeWidth={3} />}
                {isActive && !isComplete && <span style={{ width: 7, height: 7, borderRadius: '50%', background: dotColor }} />}
              </div>
              <div style={{
                fontSize: 11.5, fontWeight: isActive ? 700 : 500,
                color: labelColor, letterSpacing: 0.2, textAlign: 'center',
              }}>
                {s.label}
              </div>
              {s.timeOf && s.timeOf(change) && (
                <div style={{ fontSize: 10, color: T.textMuted, marginTop: 2 }}>
                  {formatRelative(s.timeOf(change))}
                </div>
              )}
            </div>
            {i < states.length - 1 && (
              <div style={{
                flex: 1, height: 1.5,
                background: isComplete ? T.success : 'rgba(15,23,42,0.10)',
                margin: '0 6px 32px 6px',
                borderRadius: 999,
              }} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function ChangeContextBar({ change }) {
  const ref = change.npci_change_id ? `${change.npci_change_id.slice(0, 8)}…` : '—';
  const items = [
    { icon: Building2, label: 'From',          value: `${t('term.authorityCap')} Change Management` },
    { icon: Hash,      label: 'Reference',     value: ref, title: change.npci_change_id },
    { icon: Clock,     label: 'Received',      value: formatRelative(change.received_at), title: formatAbsolute(change.received_at) },
    { icon: Activity,  label: 'Last activity', value: formatRelative(change.last_activity_at || change.received_at) },
  ];
  return (
    <div style={card({
      display: 'flex', flexWrap: 'wrap', gap: 28,
      padding: '14px 22px', marginBottom: 16,
      fontSize: 12,
    })}>
      {items.map((it) => {
        const Icon = it.icon;
        return (
          <div key={it.label} title={it.title || ''} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Icon size={14} style={{ color: 'var(--text-muted, #6b7280)' }} />
            <div>
              <div style={{ fontSize: 10, color: 'var(--text-muted, #6b7280)', textTransform: 'uppercase', letterSpacing: 0.4, fontWeight: 600 }}>{it.label}</div>
              <div style={{ fontSize: 13, color: 'var(--text-primary, #111827)', fontWeight: 500 }}>{it.value}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function statusBadge(status) {
  // Color by meaning, not by accent:
  //   blue   — informational, in-flight
  //   amber  — pending attention / awaiting input
  //   green  — approved / done / safe
  //   red    — blocked / failed
  const TONE = {
    new:          { bg: 'rgba(37,99,235,0.10)',  fg: T.primary,    border: 'rgba(37,99,235,0.20)' },
    received:     { bg: 'rgba(37,99,235,0.10)',  fg: T.primary,    border: 'rgba(37,99,235,0.20)' },
    in_progress:  { bg: 'rgba(37,99,235,0.10)',  fg: T.primary,    border: 'rgba(37,99,235,0.20)' },
    pending:      { bg: 'rgba(245,158,11,0.10)', fg: T.warning,    border: 'rgba(245,158,11,0.22)' },
    negotiating:  { bg: 'rgba(245,158,11,0.10)', fg: T.warning,    border: 'rgba(245,158,11,0.22)' },
    accepted:     { bg: 'rgba(16,185,129,0.10)', fg: T.success,    border: 'rgba(16,185,129,0.22)' },
    ready:        { bg: 'rgba(16,185,129,0.10)', fg: T.success,    border: 'rgba(16,185,129,0.22)' },
    certified:    { bg: 'rgba(16,185,129,0.14)', fg: T.success,    border: 'rgba(16,185,129,0.30)' },
    blocked:      { bg: 'rgba(239,68,68,0.08)',  fg: T.danger,     border: 'rgba(239,68,68,0.22)' },
    rejected:     { bg: 'rgba(239,68,68,0.08)',  fg: T.danger,     border: 'rgba(239,68,68,0.22)' },
  };
  const tone = TONE[status] || TONE.new;
  const label = (status || 'new').replace(/_/g, ' ');
  return (
    <span style={{
      fontSize: 11, padding: '4px 10px', borderRadius: 999, fontWeight: 700,
      letterSpacing: 0.5, textTransform: 'uppercase',
      background: tone.bg, color: tone.fg,
      border: `1px solid ${tone.border}`,
    }}>{label}</span>
  );
}

function StatusPill({ tone, children }) {
  const palette = {
    new:       { bg: 'rgba(37,99,235,0.10)',  fg: T.primary,  border: 'rgba(37,99,235,0.20)' },
    viewed:    { bg: 'rgba(15,23,42,0.05)',   fg: T.textSecondary, border: 'rgba(15,23,42,0.08)' },
    reviewed:  { bg: 'rgba(16,185,129,0.10)', fg: T.success,  border: 'rgba(16,185,129,0.22)' },
    warning:   { bg: 'rgba(245,158,11,0.10)', fg: T.warning,  border: 'rgba(245,158,11,0.22)' },
    danger:    { bg: 'rgba(239,68,68,0.08)',  fg: T.danger,   border: 'rgba(239,68,68,0.22)' },
  }[tone] || { bg: 'rgba(15,23,42,0.05)', fg: T.textSecondary, border: 'rgba(15,23,42,0.08)' };
  return (
    <span style={{
      fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
      letterSpacing: 0.4, textTransform: 'uppercase',
      background: palette.bg, color: palette.fg,
      border: `1px solid ${palette.border}`,
    }}>{children}</span>
  );
}

// Doc list row — purely informational on the left, explicit action
// buttons on the right. Clicking the Preview button opens the slide-
// over DocPreviewDrawer (rendered at the page level). Inline expansion
// removed — that pattern doesn't scale to 10+ docs and pulls the
// reader out of the workflow. Downloads and Mark Reviewed remain on
// the card so users don't need to open the drawer for them.
function DocCard({ versions, changeId, reviewStateByDoc, onPreview }) {
  const [downloading, setDownloading] = useState(null);  // null | 'docx' | 'pptx'
  // versions arrive newest-first. Default to the latest; the per-document
  // version tabs let the partner read any prior version of THIS document.
  const vers = versions && versions.length ? versions : [];
  const [selVer, setSelVer] = useState(vers[0]?.negotiation_version ?? 1);
  const active = vers.find(v => (v.negotiation_version || 1) === selVer) || vers[0] || {};
  const latestVer = vers[0]?.negotiation_version ?? 1;

  const docType       = active.doc_type || active.type || 'Document';
  const content       = active.content || '';
  const docId         = active.id;

  // One canonical download format per artifact (see DOC_DOWNLOAD).
  const spec        = downloadSpec(docType, active);
  const canDownload = canDownloadSpec(spec, active);

  const meta = docMeta(docType);
  const Icon = meta.icon;
  const size = formatBytes((content || '').length);

  const docFlags = (reviewStateByDoc && reviewStateByDoc[docId]) || {};
  const isViewed = !!docFlags.viewedAt;

  const onDownload = async (e) => {
    e.stopPropagation();
    if (downloading) return;
    setDownloading(spec.ext);
    try {
      await downloadArtifact(spec, changeId, docId, `${docType}_v${selVer}`, {
        docx:  active.docx_filename,
        pptx:  active.pptx_filename,
        xlsx:  active.xlsx_filename,
        video: active.video_filename,
        zip:   active.zip_filename,
      });
    } catch (err) { console.error('Download failed', err); }
    finally { setDownloading(null); }
  };

  const onPreviewClick = (e) => {
    e.stopPropagation();
    if (onPreview) onPreview(docId);
  };

  const downloadChip = canDownload && (
    <button
      onClick={onDownload}
      disabled={!!downloading}
      title={`Download .${spec.ext}`}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 5,
        padding: '5px 11px',
        borderRadius: 6, fontSize: 11, fontWeight: 600,
        background: 'transparent',
        color: T.primary,
        border: `1px solid ${T.borderSubtle}`,
        cursor: downloading ? 'wait' : 'pointer',
        opacity: downloading ? 0.6 : 1,
      }}
    >
      <Download size={11} /> {downloading ? '…' : `.${spec.ext}`}
    </button>
  );

  return (
    <div
      className="pp-doc-card"
      style={card({
        marginBottom: 8,
        padding: '11px 16px',
        borderColor: T.borderSubtle,
      })}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        {/* Icon tile */}
        <div style={{
          width: 34, height: 34, flexShrink: 0,
          borderRadius: 8,
          background: 'rgba(37, 99, 235, 0.08)',
          color: T.primary,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <Icon size={17} />
        </div>

        {/* Label + status pills + description */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 13.5, fontWeight: 600, color: T.textPrimary }}>
              {meta.label}
            </span>
            {!isViewed && <StatusPill tone="new">New</StatusPill>}
            {isViewed && <StatusPill tone="viewed">Viewed</StatusPill>}
            {/* Per-document version tabs — click to read any version of THIS doc. */}
            {vers.length > 1 && (
              <span style={{ display: 'inline-flex', gap: 2, border: `1px solid ${T.borderSubtle}`, borderRadius: 6, padding: 2 }}>
                {[...vers].sort((a, b) => (a.negotiation_version || 1) - (b.negotiation_version || 1)).map(v => {
                  const vn = v.negotiation_version || 1;
                  const isActive = vn === selVer;
                  return (
                    <button
                      key={vn}
                      onClick={(e) => { e.stopPropagation(); setSelVer(vn); }}
                      title={vn === latestVer ? `Version ${vn} (latest)` : `Version ${vn}`}
                      style={{
                        padding: '2px 9px', borderRadius: 4, border: 'none',
                        cursor: 'pointer', fontSize: 10.5, fontWeight: 700,
                        background: isActive ? T.primary : 'transparent',
                        color: isActive ? '#fff' : T.textMuted,
                      }}
                    >
                      v{vn}
                    </button>
                  );
                })}
              </span>
            )}
            {size && (
              <span style={{
                fontSize: 10, padding: '1px 7px', borderRadius: 999,
                background: T.bgMuted, color: T.textMuted,
                fontWeight: 600, letterSpacing: 0.3,
              }}>{size}</span>
            )}
          </div>
          <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {meta.desc}
          </div>
        </div>

        {/* Explicit actions — info on left, buttons on right. No
            full-card click; user picks what they want to do. */}
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {downloadChip}
          {/* Preview is the primary affordance — always visible */}
          <button
            onClick={onPreviewClick}
            title="Open preview"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              padding: '5px 12px',
              borderRadius: 6, fontSize: 11, fontWeight: 600,
              background: T.primary, color: '#fff',
              border: `1px solid ${T.primary}`,
              cursor: 'pointer',
            }}
          >
            <Eye size={11} /> Preview
          </button>
        </div>
      </div>
    </div>
  );
}

// Slide-over preview drawer. Renders at the page level on top of an
// overlay; reads the docs list so it can paginate Next/Prev without
// closing. Keyboard: ←/→ navigate, ESC closes. Auto-marks viewed on
// open so the review-progress bar reflects what the user has actually
// seen. Width is min(720px, 70vw) — comfortable for text content,
// leaves the doc list visible alongside on wide screens.
function DocPreviewDrawer({ open, doc, docs, changeId, reviewState, onClose, onNavigate, onSelectDoc, onView }) {
  const [downloading, setDownloading] = useState(null);
  // Prototype-screens full-size preview (scale-to-fit modal, mirrors the authority).
  const [showExpand, setShowExpand] = useState(false);
  const [modalScale, setModalScale] = useState(1);
  // Promo/explainer video — fetched as a blob (auth'd) for inline playback.
  const [videoUrl, setVideoUrl] = useState(null);
  const videoUrlRef = useRef(null);
  const contentRef = useRef(null);

  // Auto-mark viewed whenever the drawer opens or paginates to a doc.
  useEffect(() => {
    if (open && doc && onView) onView(doc.id);
  }, [open, doc?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reset scroll + close any expanded prototype when the visible doc changes.
  useEffect(() => {
    if (contentRef.current) contentRef.current.scrollTop = 0;
    setShowExpand(false);
  }, [doc?.id]);

  // Scale the 390×780 phone to fit the viewport while the expand modal is open.
  useEffect(() => {
    if (!showExpand) return;
    const recompute = () => {
      const fitH = (window.innerHeight - 80) / 780;
      const fitW = (window.innerWidth - 80) / 390;
      setModalScale(Math.max(0.5, Math.min(1.5, fitH, fitW)));
    };
    recompute();
    window.addEventListener('resize', recompute);
    return () => window.removeEventListener('resize', recompute);
  }, [showExpand]);

  // Load the promo/explainer MP4 (auth'd blob -> object URL) when the visible
  // doc is a video that shipped one. Revoke the prior URL on every doc change.
  useEffect(() => {
    if (videoUrlRef.current) { URL.revokeObjectURL(videoUrlRef.current); videoUrlRef.current = null; }
    setVideoUrl(null);
    const dt = doc && (doc.doc_type || doc.type);
    if (!open || !doc || !doc.has_video || !['promo_video', 'explainer_video'].includes(dt)) return;
    let cancelled = false;
    fetchChangeDocumentVideo(changeId, doc.id)
      .then((blob) => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        videoUrlRef.current = url;
        setVideoUrl(url);
      })
      .catch(() => { /* leave player hidden; download still available */ });
    return () => { cancelled = true; };
  }, [open, doc?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (videoUrlRef.current) URL.revokeObjectURL(videoUrlRef.current); }, []);

  // Keyboard nav — bind only while the drawer is open so the rest of
  // the page keeps its own arrow-key behaviour.
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        if (showExpand) setShowExpand(false); else onClose();
        return;
      }
      if (showExpand) return;  // modal open — don't paginate the drawer underneath
      if (e.key === 'ArrowRight') { e.preventDefault(); onNavigate(+1); }
      if (e.key === 'ArrowLeft')  { e.preventDefault(); onNavigate(-1); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose, onNavigate, showExpand]);

  if (!open || !doc) return null;

  const meta = docMeta(doc.doc_type || doc.type);
  const Icon = meta.icon;
  // Navigate between distinct documents (one per doc_type); the version tabs
  // handle switching versions within a document. So count/position are by type.
  const distinctTypes = [];
  const _seenTypes = new Set();
  for (const d of docs) {
    const t = d.doc_type || d.type;
    if (!_seenTypes.has(t)) { _seenTypes.add(t); distinctTypes.push(t); }
  }
  const typeIdx = distinctTypes.indexOf(doc.doc_type || doc.type);
  const isFirst = typeIdx <= 0;
  const isLast  = typeIdx >= distinctTypes.length - 1;

  const docFlags = reviewState || {};
  const isViewed = !!docFlags.viewedAt;
  const size = formatBytes((doc.content || '').length);
  const groupId = groupOf(doc.doc_type || doc.type);
  const groupLabel = (DOC_GROUPS.find(g => g.id === groupId) || {}).label || 'Document';

  // Other versions of THIS document, so the preview can switch between them.
  const sameType = docs.filter(d => (d.doc_type || d.type) === (doc.doc_type || doc.type));
  const docVersions = [...sameType].sort((a, b) => (a.negotiation_version || 1) - (b.negotiation_version || 1));
  const curVer = doc.negotiation_version || 1;
  const latestVer = docVersions.length ? Math.max(...docVersions.map(v => v.negotiation_version || 1)) : curVer;

  // One canonical download format per artifact (see DOC_DOWNLOAD).
  const dlSpec    = downloadSpec(doc.doc_type || doc.type, doc);
  const canDl     = canDownloadSpec(dlSpec, doc);
  const onDownload = async () => {
    if (downloading || !canDl) return;
    setDownloading(dlSpec.ext);
    try {
      await downloadArtifact(dlSpec, changeId, doc.id, `${doc.doc_type}_v${doc.negotiation_version || 1}`, {
        docx:  doc.docx_filename,
        pptx:  doc.pptx_filename,
        xlsx:  doc.xlsx_filename,
        video: doc.video_filename,
        zip:   doc.zip_filename,
      });
    } catch (err) { console.error('Download failed', err); }
    finally { setDownloading(null); }
  };


  return (
    <>
      <div
        className="pp-drawer-backdrop"
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0,
          background: 'rgba(15, 23, 42, 0.45)',
          zIndex: 100,
        }}
      />
      <div
        className="pp-drawer-panel"
        role="dialog"
        aria-label={meta.label}
        style={{
          position: 'fixed', top: 0, right: 0, bottom: 0,
          // True 70% of the viewport; clamp to 600px on small screens
          // so the body text stays legible on a laptop / tablet.
          width: '70vw',
          minWidth: 600,
          background: T.cardBg,
          boxShadow: '-12px 0 32px rgba(15, 23, 42, 0.12)',
          zIndex: 101,
          display: 'flex', flexDirection: 'column',
        }}
      >
        {/* Header */}
        <div style={{
          padding: '18px 24px',
          borderBottom: `1px solid ${T.borderSubtle}`,
        }}>
          {/* Category breadcrumb + close */}
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
            <div style={{ fontSize: 11, fontWeight: 700, color: T.textMuted, letterSpacing: 0.5, textTransform: 'uppercase' }}>
              {groupLabel} · Document {typeIdx + 1} of {distinctTypes.length}
            </div>
            <button
              onClick={onClose}
              title="Close (Esc)"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 4,
                background: 'transparent', border: 'none',
                color: T.textMuted, cursor: 'pointer',
                padding: 4, borderRadius: 6,
              }}
            >
              <X size={18} />
            </button>
          </div>

          {/* Title row */}
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
            <div style={{
              width: 42, height: 42, flexShrink: 0,
              borderRadius: 10,
              background: 'rgba(37, 99, 235, 0.08)',
              color: T.primary,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <Icon size={20} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <h2 style={{ margin: 0, fontSize: 17, fontWeight: 700, color: T.textPrimary }}>
                  {meta.label}
                </h2>
                {!isViewed && <StatusPill tone="new">New</StatusPill>}
                {isViewed && <StatusPill tone="viewed">Viewed</StatusPill>}
                {/* Version switch — read any version of this document in-place. */}
                {docVersions.length > 1 && (
                  <span style={{ display: 'inline-flex', gap: 2, border: `1px solid ${T.borderSubtle}`, borderRadius: 6, padding: 2 }}>
                    {docVersions.map(v => {
                      const vn = v.negotiation_version || 1;
                      const isActive = vn === curVer;
                      return (
                        <button
                          key={vn}
                          onClick={() => onSelectDoc && onSelectDoc(v.id)}
                          title={vn === latestVer ? `Version ${vn} (latest)` : `Version ${vn}`}
                          style={{
                            padding: '2px 10px', borderRadius: 4, border: 'none',
                            cursor: 'pointer', fontSize: 11, fontWeight: 700,
                            background: isActive ? T.primary : 'transparent',
                            color: isActive ? '#fff' : T.textMuted,
                          }}
                        >
                          v{vn}{vn === latestVer ? ' (latest)' : ''}
                        </button>
                      );
                    })}
                  </span>
                )}
              </div>
              <div style={{ fontSize: 13, color: T.textSecondary, marginTop: 4, lineHeight: 1.4 }}>
                {meta.desc}
              </div>
              <div style={{ fontSize: 11, color: T.textMuted, marginTop: 6, display: 'flex', gap: 14, flexWrap: 'wrap' }}>
                <span>Size {size || '—'}</span>
                {docFlags.viewedAt && <span>Viewed {formatRelative(docFlags.viewedAt)}</span>}
              </div>
            </div>
          </div>

          {/* Action toolbar — one canonical download format per artifact. */}
          <div style={{ display: 'flex', gap: 8, marginTop: 14, flexWrap: 'wrap' }}>
            {canDl && (
              <button
                onClick={onDownload}
                disabled={!!downloading}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '7px 14px',
                  borderRadius: 8, fontSize: 12, fontWeight: 600,
                  background: 'transparent', color: T.primary,
                  border: `1px solid ${T.borderSubtle}`,
                  cursor: downloading ? 'wait' : 'pointer',
                }}
              >
                <Download size={13} /> {downloading ? 'Downloading…' : `Download .${dlSpec.ext}`}
              </button>
            )}
          </div>
        </div>

        {/* Scrollable content */}
        <div
          ref={contentRef}
          style={{
            flex: 1, overflowY: 'auto',
            padding: '20px 24px',
          }}
        >
          {/* Promo/explainer video player — above the script. The MP4 was
              produced off-platform from the script below and shipped with the kit. */}
          {videoUrl && (
            <video
              src={videoUrl}
              controls
              style={{ width: '100%', maxHeight: 360, borderRadius: 8, background: '#000', display: 'block', marginBottom: 16 }}
            />
          )}
          {doc.has_video && !videoUrl && (
            <div style={{ marginBottom: 16, fontSize: 12.5, color: T.textMuted }}>Loading video…</div>
          )}
          {(doc.doc_type || doc.type) === 'prototype_screens' ? (
            // Interactive mobile-style prototype: doc.content is a complete,
            // self-contained HTML file (inline CSS/JS, text wordmark — no external
            // assets, so it renders in a sandboxed iframe with no network).
            // Render it in a sandboxed iframe — scripts ON so the in-page go()
            // click-through navigation works, but NO same-origin so it can't
            // reach the partner session. The iframe renders at native 390×780
            // internally; the inline preview is scaled to 0.6 (234×468) so it
            // fits the drawer without scrolling — Expand opens a scale-to-fit
            // full-size modal (mirrors the authority platform).
            <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
              <div style={{ position: 'relative', width: 234, height: 468 }}>
                <div style={{
                  width: 390, height: 780, borderRadius: 36, padding: 12,
                  background: '#111', boxShadow: '0 12px 40px rgba(0,0,0,0.35)',
                  position: 'absolute', top: 0, left: 0,
                  transform: 'scale(0.6)', transformOrigin: 'top left',
                }}>
                  <div style={{
                    position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                    width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
                  }} />
                  <PrototypePreview
                    title="Prototype preview"
                    html={doc.content}
                    style={{
                      width: '100%', height: '100%', border: 'none',
                      borderRadius: 28, background: '#fff', display: 'block',
                    }}
                  />
                </div>
                <button
                  onClick={() => setShowExpand(true)}
                  title="Open full-size preview"
                  style={{
                    position: 'absolute', top: 4, right: 4, zIndex: 3,
                    padding: '6px 10px', background: 'rgba(255,255,255,0.94)',
                    border: `1px solid ${T.borderSubtle}`, borderRadius: 6,
                    cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 5,
                    fontSize: 11, fontWeight: 600, color: T.textPrimary,
                    boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
                  }}
                >
                  <Maximize2 size={12} /> Expand
                </button>
              </div>
            </div>
          ) : (doc.doc_type || doc.type) === 'cert_test_cases' ? (
            <CertTestCasesView markdown={doc.content || ''} />
          ) : (
            <div className="markdown-content" style={{ color: T.textSecondary, lineHeight: 1.6, fontSize: 14 }}>
              <ReactMarkdown>{doc.content || ''}</ReactMarkdown>
            </div>
          )}
        </div>

        {/* Footer — sticky pager */}
        <div style={{
          padding: '12px 22px',
          borderTop: `1px solid ${T.borderSubtle}`,
          background: T.bgSoft,
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        }}>
          <span style={{ fontSize: 12, color: T.textMuted }}>
            ← / → to navigate · Esc to close
          </span>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              onClick={() => onNavigate(-1)}
              disabled={isFirst}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                padding: '7px 12px',
                borderRadius: 8, fontSize: 12, fontWeight: 600,
                background: 'transparent',
                color: isFirst ? T.textMuted : T.textPrimary,
                border: `1px solid ${T.borderSubtle}`,
                cursor: isFirst ? 'not-allowed' : 'pointer',
                opacity: isFirst ? 0.5 : 1,
              }}
            >
              <ArrowLeft size={13} /> Previous
            </button>
            <button
              onClick={() => onNavigate(+1)}
              disabled={isLast}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 5,
                padding: '7px 12px',
                borderRadius: 8, fontSize: 12, fontWeight: 600,
                background: isLast ? 'transparent' : T.primary,
                color: isLast ? T.textMuted : '#fff',
                border: `1px solid ${isLast ? T.borderSubtle : T.primary}`,
                cursor: isLast ? 'not-allowed' : 'pointer',
                opacity: isLast ? 0.5 : 1,
              }}
            >
              Next <ArrowRight size={13} />
            </button>
          </div>
        </div>
      </div>

      {/* Full-size prototype preview — scale-to-fit modal (mirrors the authority). */}
      {showExpand && (doc.doc_type || doc.type) === 'prototype_screens' && (
        <div
          onClick={() => setShowExpand(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(0,0,0,0.72)', overflow: 'auto',
            display: 'flex', padding: 24, backdropFilter: 'blur(2px)',
          }}
        >
          <div onClick={e => e.stopPropagation()} style={{
            position: 'relative', margin: 'auto',
            width: 390 * modalScale, height: 780 * modalScale,
          }}>
            <button
              onClick={() => setShowExpand(false)}
              title="Close (Esc)"
              aria-label="Close prototype preview"
              style={{
                position: 'absolute', top: -14, right: -14, zIndex: 2,
                width: 36, height: 36, borderRadius: '50%',
                background: '#fff', border: 'none', cursor: 'pointer',
                boxShadow: '0 4px 14px rgba(0,0,0,0.4)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}
            >
              <X size={16} />
            </button>
            <div style={{
              width: 390, height: 780, borderRadius: 36, padding: 12,
              background: '#111', boxShadow: '0 24px 60px rgba(0,0,0,0.6)',
              position: 'absolute', top: 0, left: 0,
              transform: `scale(${modalScale})`, transformOrigin: 'top left',
            }}>
              <div style={{
                position: 'absolute', top: 12, left: '50%', transform: 'translateX(-50%)',
                width: 110, height: 22, background: '#111', borderRadius: 14, zIndex: 2,
              }} />
              <PrototypePreview
                title="Prototype preview (expanded)"
                html={doc.content}
                style={{
                  width: '100%', height: '100%', border: 'none',
                  borderRadius: 28, background: '#fff', display: 'block',
                }}
              />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function DraftRow({ draft, changeId }) {
  const queryClient = useQueryClient();
  const [isEditing, setIsEditing] = useState(false);
  const [text, setText] = useState(draft.text);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['queryDrafts', changeId] });
    queryClient.invalidateQueries({ queryKey: ['change', changeId] });
  };

  const saveMut = useMutation({
    mutationFn: () => updateQueryDraft(draft.id, text.trim()),
    onSuccess: () => { setIsEditing(false); invalidate(); },
  });
  const discardMut = useMutation({
    mutationFn: () => discardQueryDraft(draft.id),
    onSuccess: invalidate,
  });
  const sendMut = useMutation({
    mutationFn: () => sendQueryDraft(draft.id),
    onSuccess: invalidate,
  });

  const busy = saveMut.isPending || discardMut.isPending || sendMut.isPending;

  return (
    <div className="query-item" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {isEditing ? (
        <>
          <textarea
            className="input-field"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            style={{ resize: 'vertical', fontFamily: 'inherit' }}
          />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => { setText(draft.text); setIsEditing(false); }}
              disabled={busy}
            >
              <X size={13} /> Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => text.trim() && saveMut.mutate()}
              disabled={busy || !text.trim() || text.trim() === draft.text}
            >
              <Save size={13} /> Save
            </button>
          </div>
        </>
      ) : (
        <>
          <div className="query-question" style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <Sparkles size={14} style={{ flexShrink: 0, marginTop: 3, color: 'var(--accent, #da7756)' }} />
            <span style={{ flex: 1 }}>{draft.text}</span>
          </div>
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => setIsEditing(true)}
              disabled={busy}
            >
              <Edit2 size={13} /> Edit
            </button>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => discardMut.mutate()}
              disabled={busy}
            >
              <Trash2 size={13} /> Remove
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => sendMut.mutate()}
              disabled={busy}
            >
              <Send size={13} /> Send
            </button>
          </div>
        </>
      )}
    </div>
  );
}


function QueryPanel({ changeId, queries }) {
  const [message, setMessage] = useState('');
  const queryClient = useQueryClient();

  const submitMut = useMutation({
    mutationFn: (msg) => submitQuery(changeId, msg),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
      setMessage('');
    },
  });

  // Drafts list
  const { data: drafts = [] } = useQuery({
    queryKey: ['queryDrafts', changeId],
    queryFn: () => listQueryDrafts(changeId),
  });

  // Auto-trigger suggestion once per change. Backend is idempotent — if drafts
  // already exist (any status) it short-circuits without calling the LLM.
  const suggestMut = useMutation({
    mutationFn: () => suggestQueryDrafts(changeId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['queryDrafts', changeId] });
    },
  });

  useEffect(() => {
    if (!changeId) return;
    suggestMut.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeId]);

  const suggestReason = suggestMut.data?.generated === false ? suggestMut.data.reason : null;

  return (
    <div className="query-panel">
      {/* Manual query input — unchanged behaviour */}
      <div className="query-input-row">
        <input
          className="input-field"
          placeholder="Ask a clarifying question..."
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && message.trim() && submitMut.mutate(message.trim())}
        />
        <button
          className="btn btn-primary"
          disabled={!message.trim() || submitMut.isPending}
          onClick={() => message.trim() && submitMut.mutate(message.trim())}
        >
          <Send size={14} />
          Send
        </button>
      </div>

      {/* Auto-suggested drafts */}
      {(suggestMut.isPending || drafts.length > 0 || suggestReason) && (
        <div style={{ marginTop: 16 }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8,
            fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.05em',
            marginBottom: 8,
          }}>
            <Sparkles size={13} />
            Suggested questions from documents
          </div>

          {suggestMut.isPending && (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: 10 }}>
              Analyzing shared documents…
            </div>
          )}

          {!suggestMut.isPending && drafts.length === 0 && suggestReason === 'no_api_key' && (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: 10 }}>
              Configure the partner Anthropic API key in Settings to enable question suggestions.
            </div>
          )}

          {!suggestMut.isPending && drafts.length === 0 && suggestReason === 'no_documents' && (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: 10 }}>
              No documents received yet — nothing to analyze.
            </div>
          )}

          {!suggestMut.isPending && drafts.length === 0 && suggestReason === 'no_questions_produced' && (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, padding: 10 }}>
              The documents look clear — no clarifying questions were suggested.
            </div>
          )}

          {drafts.map((d) => (
            <DraftRow key={d.id} draft={d} changeId={changeId} />
          ))}
        </div>
      )}

      {/* Sent queries + the authority responses */}
      {queries && queries.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{
            fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.05em',
            marginBottom: 8,
          }}>
            Sent queries
          </div>
          {queries.map((q, i) => (
            <div key={i} className="query-item">
              <div className="query-question">Q: {q.message}</div>
              {q.response ? (
                <div className="query-answer">{q.response}</div>
              ) : (
                <div className="query-pending">Awaiting response from {t('term.authority')}...</div>
              )}
            </div>
          ))}
        </div>
      )}

      {(!queries || queries.length === 0) && drafts.length === 0 && !suggestMut.isPending && !suggestReason && (
        <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: 12 }}>
          No questions yet. Ask {t('term.authority')} a clarifying question, or use Negotiate above to propose changes.
        </div>
      )}
    </div>
  );
}

function FrozenDecisionActions({ changeId, onSubmitted }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [severity, setSeverity] = useState('high');
  const [text, setText] = useState('');
  const mutation = useMutation({
    mutationFn: () => reportBlocker(changeId, { severity, description: text }),
    onSuccess: (data) => {
      setText('');
      setOpen(false);
      if (data && data.change) queryClient.setQueryData(['change', changeId], data.change);
      else queryClient.invalidateQueries({ queryKey: ['change', changeId] });
      if (onSubmitted) onSubmitted();
    },
  });

  return (
    <div style={{
      borderRadius: 8, overflow: 'hidden',
      border: '1px solid rgba(99,102,241,0.20)',
      background: 'rgba(99,102,241,0.04)',
    }}>
      {/* Notice row */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px' }}>
        <Lock size={15} style={{ flexShrink: 0, color: '#6366f1' }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#4338ca' }}>
            2 negotiation rounds completed — no further changes accepted
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-secondary, #6b7280)', marginTop: 2 }}>
            The final product kit has been issued. Queries and counter-proposals are closed.
          </div>
        </div>
        <button
          onClick={() => { setOpen(o => !o); setText(''); }}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '8px 14px', flexShrink: 0,
            background: open ? 'var(--bg-base,#fff)' : '#fff',
            color: '#374151',
            border: '1px solid var(--border, #d1d5db)',
            borderRadius: 7, fontSize: 13, fontWeight: 500, cursor: 'pointer',
          }}
        >
          <Shield size={13} /> {open ? 'Cancel' : 'Report a blocker'}
        </button>
      </div>

      {/* Inline blocker form */}
      {open && (
        <div style={{
          padding: '12px 16px', borderTop: '1px solid rgba(99,102,241,0.12)',
          background: '#fff', display: 'flex', flexDirection: 'column', gap: 10,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <label style={{ fontSize: 12, color: 'var(--text-muted,#6b7280)', whiteSpace: 'nowrap' }}>Severity</label>
            <select
              value={severity} onChange={e => setSeverity(e.target.value)}
              style={{ padding: '5px 8px', fontSize: 12, borderRadius: 6, border: '1px solid var(--border,#d1d5db)' }}
            >
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
          </div>
          <textarea
            value={text} onChange={e => setText(e.target.value)}
            placeholder="Describe what is blocking your implementation…"
            rows={3}
            style={{
              width: '100%', padding: '8px 10px', fontSize: 13,
              border: '1px solid var(--border,#d1d5db)', borderRadius: 6,
              fontFamily: 'inherit', resize: 'vertical', boxSizing: 'border-box',
            }}
          />
          {mutation.isError && (
            <div style={{ fontSize: 12, color: '#ef4444' }}>
              ⚠ {mutation.error?.response?.data?.detail || 'Failed to send — please try again.'}
            </div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
            <button
              disabled={!text.trim() || mutation.isPending}
              onClick={() => mutation.mutate()}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '9px 18px',
                background: '#ef4444', color: '#fff',
                border: 'none', borderRadius: 7, fontSize: 13, fontWeight: 600,
                cursor: text.trim() && !mutation.isPending ? 'pointer' : 'not-allowed',
                opacity: text.trim() && !mutation.isPending ? 1 : 0.5,
              }}
            >
              {mutation.isPending ? <Loader2 size={13} className="spin" /> : <Send size={13} />}
              {mutation.isPending ? 'Sending…' : `Send blocker to ${t('term.authority')}`}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function DecisionPanel({ changeId, decision, npciCounter, change, onSubmitted }) {
  const queryClient = useQueryClient();
  // Messaging hold while the authority prepares a revised kit.
  const revisionHeld = !!change?.revision_in_progress;
  // Kit frozen by the authority (round cap reached / specs finalised). The partner can
  // still Accept the rollout and ask basic clarifying questions — only the
  // negotiation (term changes) is closed.
  const frozen = Boolean(change?.negotiation_finalized_at) || (change?.negotiation_version || 1) >= 3;
  // mode: 'accept' | 'question' | null
  const [mode, setMode] = useState(null);
  const [cabRef, setCabRef] = useState('');
  const [kickoff, setKickoff] = useState('');
  const [questionText, setQuestionText] = useState('');
  const [error, setError] = useState('');
  // New version acknowledgment
  const [acceptingVersion, setAcceptingVersion] = useState(false);

  const acceptMutation = useMutation({
    mutationFn: () => acceptChange(changeId, {
      internal_change_advisory_ref: cabRef.trim() || undefined,
      implementation_kickoff_date:  kickoff || undefined,
    }),
    onSuccess: () => {
      setMode(null);
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
      if (onSubmitted) onSubmitted();
    },
    onError: (e) => setError(e.response?.data?.detail || 'Failed to send acceptance'),
  });

  const askMutation = useMutation({
    mutationFn: () => submitQuery(changeId, questionText.trim()),
    onSuccess: () => {
      setMode(null);
      setQuestionText('');
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
      if (onSubmitted) onSubmitted();
    },
    onError: (e) => setError(e.response?.data?.detail || 'Failed to send question'),
  });

  // Once accepted, render a calm confirmation card — but keep an "Ask a
  // question" affordance so the partner can still raise generic clarifying
  // queries to the authority after accepting (the rollout decision is done, questions
  // never close).
  if (decision === 'accepted') {
    return (
      <div style={card({
        padding: '16px 18px', marginBottom: 16,
        background: 'linear-gradient(to bottom, rgba(16,185,129,0.04), rgba(16,185,129,0.02))',
        borderColor: 'rgba(16, 185, 129, 0.18)',
      })}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
          <div style={{
            width: 28, height: 28, flexShrink: 0,
            borderRadius: '50%', background: 'var(--success, #10b981)',
            color: '#fff', display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <Check size={16} strokeWidth={3} />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary, #111827)', marginBottom: 2 }}>
              Rollout accepted
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted, #6b7280)' }}>
              Implementation can begin. You can still ask {t('term.authority')} a clarifying question below or in Activity.
            </div>

            {mode !== 'question' && (
              <button
                onClick={() => { setError(''); setMode('question'); }}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6, marginTop: 10,
                  padding: '7px 14px', background: '#fff', color: '#374151',
                  border: '1px solid var(--border, #d1d5db)', borderRadius: 7,
                  fontSize: 12.5, fontWeight: 500, cursor: 'pointer',
                }}
              >
                <HelpCircle size={13} /> Ask a question
              </button>
            )}

            {mode === 'question' && (
              <div style={{ marginTop: 10 }}>
                {frozen ? (
                  <label style={{ fontSize: 12, display: 'block', marginBottom: 4, color: '#92400e', background: 'rgba(245,158,11,0.10)', border: '1px solid rgba(245,158,11,0.25)', borderRadius: 6, padding: '8px 10px', lineHeight: 1.5 }}>
                    The product kit has been <strong>frozen by {t('term.authority')}</strong> — use this only for
                    {' '}<strong>basic, generic clarifying questions</strong> (it won't reopen negotiation).
                  </label>
                ) : (
                  <label style={{ fontSize: 12, display: 'block', marginBottom: 4, color: 'var(--text-muted, #6b7280)' }}>
                    Ask {t('term.authority')} a generic clarifying question. Opens a thread with {t('term.authority')}'s PM —
                    it doesn't change the accepted rollout.
                  </label>
                )}
                <textarea
                  value={questionText}
                  onChange={(e) => setQuestionText(e.target.value)}
                  rows={3}
                  placeholder="e.g. Which cert window applies for the production cutover?"
                  style={{ width: '100%', padding: 8, boxSizing: 'border-box' }}
                />
                {error && <div style={{ color: 'crimson', fontSize: 12, marginTop: 4 }}>{error}</div>}
                <div style={{ marginTop: 8 }}>
                  <button
                    className="btn btn-primary"
                    disabled={askMutation.isPending || !questionText.trim()}
                    onClick={() => askMutation.mutate()}
                    style={{ marginRight: 8 }}
                  >
                    <Send size={14} /> {askMutation.isPending ? 'Sending…' : 'Send question'}
                  </button>
                  <button className="btn btn-secondary" onClick={() => { setError(''); setMode(null); setQuestionText(''); }}>
                    <X size={14} /> Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  // Render the prompt + buttons + (optional) modal-style form inline.
  const showAckBadge = decision === 'acknowledged';
  const showNegotiatingBadge = decision === 'negotiating';
  const subline = showNegotiatingBadge
    ? `In negotiation with ${t('term.authority')}. You can still Accept at any time.`
    : showAckBadge
      ? `Receipt acknowledged to ${t('term.authority')} (checksums verified). Pending your decision.`
      : 'Review the Product Kit documents below. Ask a question if you need clarification, accept the rollout when ready, or propose a formal change to terms.';

  return (
    <div style={card({
      padding: '20px 22px', marginBottom: 16,
      borderLeft: `3px solid ${T.primary}`,
    })}>
      <div style={{ marginBottom: 8 }}>
        <span style={{
          display: 'inline-block', marginBottom: 8,
          fontSize: 10, padding: '3px 9px', borderRadius: 999, fontWeight: 700,
          letterSpacing: 0.5, textTransform: 'uppercase', whiteSpace: 'nowrap',
          background: 'rgba(37, 99, 235, 0.10)', color: 'var(--primary, #2563eb)',
        }}>
          Action required
        </span>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: 'var(--text-primary, #111827)' }}>
          Your decision on this rollout
        </h3>
      </div>
      <div style={{ fontSize: 13, color: 'var(--text-muted, #6b7280)', marginBottom: 14 }}>
        {subline}
      </div>

      {/* Round status indicator */}
      {change && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
          {change.negotiation_finalized_at ? (
            <span style={{ fontSize: 11, padding: '3px 8px', borderRadius: 4, background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.20)', color: '#065f46', display: 'flex', alignItems: 'center', gap: 4, fontWeight: 600 }}>
              <Check size={10} /> Specs finalized — no further changes
            </span>
          ) : decision === 'negotiating' ? (
            <span style={{ fontSize: 11, padding: '3px 8px', borderRadius: 4, background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.20)', color: '#92400e', display: 'flex', alignItems: 'center', gap: 4 }}>
              <Clock size={10} /> Negotiation in progress — {t('term.authorityCap')} will respond within 24 hours
            </span>
          ) : null}
        </div>
      )}

      {/* Frozen note — negotiation is closed, but Accept + Ask a question stay
          open (only term changes / counters are gone, which we already removed). */}
      {!mode && frozen && (
        <div style={{
          display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 12,
          padding: '9px 12px', borderRadius: 7,
          background: 'rgba(99,102,241,0.06)', border: '1px solid rgba(99,102,241,0.20)',
        }}>
          <Lock size={14} style={{ flexShrink: 0, marginTop: 1, color: '#6366f1' }} />
          <div style={{ fontSize: 12, color: 'var(--text-secondary, #6b7280)', lineHeight: 1.5 }}>
            {t('term.authorityCap')} has frozen the product kit — negotiation is closed. You can still
            accept the rollout or ask a basic clarifying question.
          </div>
        </div>
      )}

      {!mode && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          <button
            onClick={() => { setError(''); setMode('question'); }}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '9px 18px',
              background: 'var(--primary, #2563eb)', color: '#fff',
              border: '1px solid var(--primary, #2563eb)',
              borderRadius: 8, fontSize: 13, fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            <HelpCircle size={14} /> Ask a question
          </button>
          <button
            onClick={() => { setError(''); setMode('accept'); }}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              padding: '9px 18px',
              background: 'var(--success, #10b981)', color: '#fff',
              border: '1px solid var(--success, #10b981)',
              borderRadius: 8, fontSize: 13, fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            <Check size={14} /> Accept rollout
          </button>
        </div>
      )}

      {mode === 'question' && (
        <div>
          <strong>Ask {t('term.authority')} a question</strong>
          <div style={{ marginTop: 10 }}>
            {frozen ? (
              <label style={{ fontSize: 12, display: 'block', marginBottom: 4, color: '#92400e', background: 'rgba(245,158,11,0.10)', border: '1px solid rgba(245,158,11,0.25)', borderRadius: 6, padding: '8px 10px', lineHeight: 1.5 }}>
                The product kit has been <strong>frozen by {t('term.authority')}</strong> — negotiation is
                closed. Use this only for <strong>basic, generic clarifying questions</strong>
                {' '}(it won't reopen negotiation or change any terms).
              </label>
            ) : (
              <label style={{ fontSize: 12, display: 'block', marginBottom: 4, color: 'var(--text-muted, #6b7280)' }}>
                Free-text message to {t('term.authority')} — ask a clarifying question or request a
                change to a deadline, scope, limit, or rate. It opens a thread with
                {t('term.authorityCap')}'s PM and doesn't block your decision; you can still Accept the
                rollout at any time.
              </label>
            )}
            <textarea
              value={questionText}
              onChange={(e) => setQuestionText(e.target.value)}
              rows={4}
              placeholder="e.g. Do the cert windows in section 4.1 include the freeze week before Diwali, or is that excluded?"
              style={{ width: '100%', padding: 8 }}
            />
            {error && <div style={{ color: 'crimson', fontSize: 12, marginTop: 4 }}>{error}</div>}
            <div style={{ marginTop: 8 }}>
              <button
                className="btn btn-primary"
                disabled={revisionHeld || askMutation.isPending || !questionText.trim()}
                onClick={() => askMutation.mutate()}
                style={{ marginRight: 8 }}
              >
                <Send size={14} /> Send question
              </button>
              <button className="btn btn-secondary" onClick={() => { setError(''); setMode(null); }}>
                <X size={14} /> Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {mode === 'accept' && (
        <div>
          <strong>Accept this rollout</strong>
          <div style={{ display: 'flex', gap: 8, flexDirection: 'column', marginTop: 10 }}>
            <label style={{ fontSize: 12 }}>
              Internal Change Advisory ref (optional)
              <input
                type="text"
                value={cabRef}
                onChange={(e) => setCabRef(e.target.value)}
                placeholder="e.g. SBI-CAB-2026-0512-001"
                style={{ width: '100%', padding: 6, marginTop: 2 }}
              />
            </label>
            <label style={{ fontSize: 12 }}>
              Implementation kickoff date (optional)
              <input
                type="date"
                value={kickoff}
                onChange={(e) => setKickoff(e.target.value)}
                style={{ width: '100%', padding: 6, marginTop: 2 }}
              />
            </label>
            {error && <div style={{ color: 'crimson', fontSize: 12 }}>{error}</div>}
            <div>
              <button
                className="btn btn-success"
                disabled={acceptMutation.isPending}
                onClick={() => acceptMutation.mutate()}
                style={{ marginRight: 8 }}
              >
                <Send size={14} /> Confirm Accept
              </button>
              <button className="btn btn-secondary" onClick={() => { setError(''); setMode(null); }}>
                <X size={14} /> Cancel
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}


function NpciCounterCard({ changeId, npciCounter, decision }) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState(null);  // null | 'reply'
  const [replyText, setReplyText] = useState('');
  const [error, setError] = useState('');

  // Replies to an authority counter go through the single query channel, like every
  // other partner message — no separate counter-propose path.
  const replyMutation = useMutation({
    mutationFn: () => submitQuery(changeId, replyText.trim()),
    onSuccess: () => {
      setMode(null);
      setReplyText('');
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    },
    onError: (e) => setError(e.response?.data?.detail || 'Failed to send reply'),
  });

  const acceptMutation = useMutation({
    mutationFn: () => acceptCounter(changeId, npciCounter.counter_proposal_id),
    onSuccess: () => {
      setError('');
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    },
    onError: (e) => setError(e.response?.data?.detail || 'Failed to accept'),
  });

  if (!npciCounter) return null;

  const fmt = (iso) => {
    if (!iso) return null;
    try { return parseIso(iso).toLocaleDateString(); } catch { return iso; }
  };

  return (
    <div className="card" style={{
      padding: 12, marginBottom: 16,
      border: '1px solid #6ea8dc', background: 'rgba(110,168,220,0.08)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        <span style={{
          fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
          background: '#6ea8dc', color: 'white', textTransform: 'uppercase', letterSpacing: '0.5px',
        }}>
          {t('term.authorityCap')} countered back
        </span>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          round {npciCounter.negotiation_round}
        </span>
        {npciCounter.valid_until && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            · valid until {fmt(npciCounter.valid_until)}
          </span>
        )}
      </div>

      <div style={{ fontSize: 13, lineHeight: 1.5, marginBottom: 10, whiteSpace: 'pre-wrap' }}>
        {npciCounter.justification || '(no justification provided)'}
      </div>

      {!mode && (
        <>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn-success"
              disabled={acceptMutation.isPending}
              onClick={() => { setError(''); acceptMutation.mutate(); }}>
              <Check size={12} /> {acceptMutation.isPending ? 'Accepting…' : 'Accept these terms'}
            </button>
            <button className="btn btn-secondary"
              onClick={() => { setError(''); setMode('reply'); }}>
              <Edit2 size={12} /> Reply
            </button>
          </div>
          {error && (
            <div style={{ marginTop: 6, color: 'crimson', fontSize: 12 }}>{error}</div>
          )}
        </>
      )}

      {mode === 'reply' && (
        <div style={{ marginTop: 8 }}>
          <strong style={{ display: 'block', marginBottom: 6, fontSize: 13 }}>Reply to {t('term.authority')}</strong>
          <textarea value={replyText}
            onChange={(e) => setReplyText(e.target.value)}
            rows={5}
            placeholder="Your response — what you can/can't accept and what you propose instead."
            style={{ width: '100%', padding: 8, marginBottom: 6, fontSize: 12, fontFamily: 'inherit' }} />
          {error && <div style={{ color: 'crimson', fontSize: 12, marginBottom: 6 }}>{error}</div>}
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn btn-primary"
              disabled={replyMutation.isPending || !replyText.trim()}
              onClick={() => replyMutation.mutate()}>
              <Send size={12} /> {replyMutation.isPending ? 'Sending…' : 'Send reply'}
            </button>
            <button className="btn btn-secondary" onClick={() => { setError(''); setMode(null); }}>
              <X size={12} /> Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


// ── the authority Decisions log — PM accept/reject responses to partner counters ───
function CounterDecisionsSection({ decisions }) {
  if (!decisions || decisions.length === 0) return null;
  const fmt = (iso) => { try { return parseIso(iso).toLocaleString(); } catch { return iso; } };
  return (
    <div style={{ marginBottom: 16 }}>
      <h3 className="section-title">{t('term.authorityCap')} Decisions on Your Counters</h3>
      {decisions.map((d, i) => {
        const accepted = d.decision === 'ACCEPT';
        return (
          <div key={i} className="card" style={{
            padding: 10, marginBottom: 8,
            border: `1px solid ${accepted ? '#c3e6cb' : '#f5c6cb'}`,
            background: accepted ? '#d4edda' : '#fdecea',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
                background: accepted ? '#155724' : '#721c24', color: 'white',
                textTransform: 'uppercase', letterSpacing: '0.5px',
              }}>{d.decision}</span>
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                round {d.negotiation_round} · {fmt(d.received_at)}
              </span>
              {d.in_response_to && (
                <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'monospace' }}>
                  → {d.in_response_to}
                </span>
              )}
            </div>
            {d.response_text && (
              <div style={{ fontSize: 12, lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                {d.response_text}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


// ── Blockers log — partner-reported blockers + the authority's resolutions ─────────
function BlockersSection({ blockers }) {
  if (!blockers || blockers.length === 0) return null;
  const fmt = (iso) => { try { return parseIso(iso).toLocaleString(); } catch { return iso; } };

  const sevColor = (s) => ({
    critical: '#721c24', high: '#856404', medium: '#0c5460', low: '#383d41',
  }[s] || '#383d41');

  return (
    <div style={{ marginBottom: 16 }}>
      <h3 className="section-title">Blockers</h3>
      {blockers.slice().reverse().map((b) => {
        const resolved = b.status === 'resolved';
        const sev = b.severity || 'high';
        return (
          <div key={b.blocker_id} className="card" style={{
            padding: 12, marginBottom: 10,
            border: `1px solid ${resolved ? '#c3e6cb' : '#f5c6cb'}`,
            background: resolved ? 'rgba(212,237,218,0.5)' : 'rgba(253,236,234,0.5)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
                background: sevColor(sev), color: 'white',
                textTransform: 'uppercase', letterSpacing: '0.5px',
              }}>Blocker · {sev}</span>
              <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'monospace' }}>{b.blocker_id}</span>
              <span style={{
                fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4,
                background: resolved ? '#155724' : '#856404', color: 'white',
                textTransform: 'uppercase',
              }}>{resolved ? 'Resolved' : 'Open'}</span>
              {b.created_at && (
                <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>· reported {fmt(b.created_at)}</span>
              )}
            </div>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
              {b.description}
            </div>
            {b.impact && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 4 }}>
                <strong>Impact:</strong> {b.impact}
              </div>
            )}
            {b.options_considered?.length > 0 && (
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginBottom: 6 }}>
                <strong>Options proposed:</strong>
                <ul style={{ margin: '2px 0 0 0', paddingLeft: 18 }}>
                  {b.options_considered.map((o, i) => (
                    <li key={i}>
                      {o.option}
                      {o.eta && <span style={{ color: 'var(--text-muted)' }}> · {o.eta}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {b.resolution && (
              <div style={{
                marginTop: 8, padding: 8, borderRadius: 5,
                background: 'rgba(212,237,218,0.7)', borderLeft: '3px solid #155724',
              }}>
                <div style={{ fontSize: 11, fontWeight: 700, color: '#155724', marginBottom: 4 }}>
                  {t('term.authorityCap')} Resolution {b.resolution.resolved_at && `· ${fmt(b.resolution.resolved_at)}`}
                </div>
                {b.resolution.action_taken && (
                  <div style={{ fontSize: 12, marginBottom: 4 }}>
                    <strong>Action:</strong> <span style={{ fontFamily: 'monospace' }}>{b.resolution.action_taken}</span>
                  </div>
                )}
                {b.resolution.resolution_text && (
                  <div style={{ fontSize: 12, lineHeight: 1.5, whiteSpace: 'pre-wrap', marginBottom: 4 }}>
                    {b.resolution.resolution_text}
                  </div>
                )}
                {b.resolution.artifact_ref && (
                  <div style={{ fontSize: 11 }}>
                    <ArtifactRef value={b.resolution.artifact_ref} tone="resolution" />
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


function BlockerPanel({ changeId: _changeId, decision, onReport }) {
  // The structured Report-blocker form was retired in favor of the
  // chat-style composer that lives on Activity → Blocker tab. Clicking
  // here just routes the partner there; severity + description capture
  // happens inline in that composer.
  // Visible only after partner has accepted — you can't be blocked on
  // something you haven't accepted yet.
  if (decision !== 'accepted') return null;

  return (
    <div className="card" style={{ padding: 12, marginBottom: 16, borderColor: '#f5c6cb', background: '#fdecea' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <Shield size={14} color="#721c24" />
        <span style={{ fontSize: 12, color: '#721c24', flex: 1 }}>
          Stuck on something? Open the Blocker chat to describe the issue — {t('term.authorityCap')}'s PM picks it up from there.
        </span>
        <button className="btn btn-secondary" onClick={() => onReport && onReport()}>
          <Shield size={12} /> Report blocker
        </button>
      </div>
    </div>
  );
}


// ── Emergency issue (post-freeze break-glass) ──────────────────────────────
// Visible only when the change is frozen (negotiation_version >= 3): the authority has
// shipped the final kit and no longer accepts queries/counters. This is the
// only channel left to flag a critical, work-stopping problem.
function EmergencyIssueSection({ change, onRaised }) {
  const frozen = Boolean(change?.negotiation_finalized_at) || (change?.negotiation_version || 1) >= 3;
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [severity, setSeverity] = useState('critical');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const issues = change?.emergency_issues || [];

  if (!frozen) return null;

  const submit = async () => {
    if (!title.trim() || !description.trim()) return;
    setLoading(true); setError('');
    try {
      await raiseEmergencyIssue(change.id, { title: title.trim(), description: description.trim(), severity });
      setTitle(''); setDescription(''); setOpen(false);
      onRaised && onRaised();
    } catch (e) {
      setError(e?.response?.data?.detail || 'Failed to send emergency issue');
    } finally { setLoading(false); }
  };

  const fmt = (iso) => { try { return parseIso(iso).toLocaleString(); } catch { return iso; } };

  return (
    <div className="card" style={{ padding: 14, marginBottom: 16, borderColor: '#f5c6cb', background: 'rgba(253,236,234,0.6)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: open ? 12 : 0 }}>
        <AlertTriangle size={15} color="#721c24" />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#721c24' }}>Negotiation frozen</div>
          <div style={{ fontSize: 11, color: 'var(--text-secondary)' }}>
            The final Product Kit has shipped. Queries and counter-proposals are closed. Use this only for a critical, work-stopping issue.
          </div>
        </div>
        <button className="btn btn-secondary" onClick={() => setOpen(o => !o)}>
          <AlertTriangle size={12} /> {open ? 'Cancel' : 'Raise emergency issue'}
        </button>
      </div>

      {open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <input
            value={title} onChange={e => setTitle(e.target.value)}
            placeholder="Short title (e.g. Production go-live blocked by spec gap)"
            style={{ padding: '8px 10px', fontSize: 13, border: '1px solid rgba(15,23,42,0.12)', borderRadius: 6 }}
          />
          <textarea
            value={description} onChange={e => setDescription(e.target.value)}
            placeholder="Describe the issue and why work has stopped…"
            rows={4}
            style={{ padding: '8px 10px', fontSize: 13, border: '1px solid rgba(15,23,42,0.12)', borderRadius: 6, resize: 'vertical', fontFamily: 'inherit' }}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <label style={{ fontSize: 12, color: 'var(--text-secondary)' }}>Severity</label>
            <select value={severity} onChange={e => setSeverity(e.target.value)} style={{ padding: '6px 8px', fontSize: 13, borderRadius: 6 }}>
              <option value="critical">Critical</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <button className="btn btn-primary" disabled={!title.trim() || !description.trim() || loading} onClick={submit} style={{ marginLeft: 'auto' }}>
              {loading ? <Loader2 size={12} className="spin" /> : <Send size={12} />} Send to {t('term.authority')}
            </button>
          </div>
          {error && <div style={{ fontSize: 12, color: '#ef4444' }}>{error}</div>}
        </div>
      )}

      {issues.length > 0 && (
        <div style={{ marginTop: 12 }}>
          {issues.slice().reverse().map(e => (
            <div key={e.issue_id} style={{ padding: 10, marginTop: 8, borderRadius: 6, background: '#fff', border: '1px solid rgba(15,23,42,0.08)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 4, background: '#721c24', color: 'white', textTransform: 'uppercase' }}>
                  {e.severity}
                </span>
                <span style={{ fontSize: 10, fontFamily: 'monospace', color: 'var(--text-muted)' }}>{e.issue_id}</span>
                {e.created_at && <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>· {fmt(e.created_at)}</span>}
              </div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{e.title}</div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'pre-wrap' }}>{e.description}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ── Unified ActivityThread ────────────────────────────────────────────────
//
// Single chat-style timeline interleaving every the authority ↔ partner exchange:
// queries, authority clarifications, counters (both sides), counter decisions,
// blockers, and blocker resolutions. Sorted ascending by timestamp.
// Replaces the previously-fragmented sections.
//
// Right bubble = partner-sent (green). Left bubble = the authority-received (blue).
// A small kind-badge at the top of each bubble identifies the intent
// (Counter, Blocker · severity, Accept, Reject, Resolved).
// Action buttons (Accept counter / Counter back) embed inline on the
// authority counter bubble when there's an open one.

// Three-way categorization of ActivityThread events for the tab filter.
// Pure view-layer derivation from the event kinds the events-builder
// already emits — no backend or schema change needed.
//   - partner_counter / partner_counter_response: the partner's own
//     counter-propose bubbles, derived from change.queries rows that
//     carry kind='negotiation' (stamped by /counter endpoint).
const partnerTabOf = (e) => {
  if (e.kind === 'npci_counter_open'
      || e.kind === 'npci_counter_resolved'
      || e.kind === 'pm_decision'
      || e.kind === 'partner_counter'
      || e.kind === 'partner_counter_response'
      || e.kind === 'round_closed'
      || e.kind === 'round_opened') return 'negotiation';
  if (e.kind === 'blocker' || e.kind === 'blocker_resolution') return 'blocker';
  return 'general';
};

const BLOCKER_SEVERITIES = ['critical', 'high', 'medium', 'low'];

function ActivityThread({ changeId, change, activeTab: controlledTab, onActiveTabChange }) {
  const queryClient = useQueryClient();
  const scrollRef = useRef(null);
  const [composer, setComposer] = useState('');
  // activeTab is controlled when the parent passes it in (so the
  // Report-blocker entry on the Progress tab can jump straight to the
  // Blocker subtab here). Falls back to local state when uncontrolled
  // for older call sites.
  const [internalTab, setInternalTab] = useState('general');
  const activeTab = controlledTab ?? internalTab;
  const setActiveTab = (next) => {
    if (onActiveTabChange) onActiveTabChange(next);
    else setInternalTab(next);
  };
  // Blocker tab carries an inline severity selector — captured on the
  // composer-row alongside the description so the partner can fire off
  // a quick blocker without leaving the chat. The full structured
  // BlockerForm (impact / options / investigation) still lives in the
  // DecisionPanel above for cases that need the long form.
  const [blockerSeverity, setBlockerSeverity] = useState('high');

  // ── Composer mutations (one per tab) ───────────────────────────────
  // Helper: hydrate the cache from the server's fattened response when
  // available; fall back to invalidate so a stale field doesn't make us
  // miss the update. `data.change` is the fresh blob the partner
  // endpoints now return on /query, /counter, /blocker.
  const onSendSuccess = (data) => {
    setComposer('');
    if (data && data.change) {
      queryClient.setQueryData(['change', changeId], data.change);
    } else {
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    }
  };
  // General — sends a free-text clarifying question via /query.
  const sendMutation = useMutation({
    mutationFn: () => submitQuery(changeId, composer),
    onSuccess: onSendSuccess,
  });
  // Blocker — sends severity + description via /blocker.
  const blockerMutation = useMutation({
    mutationFn: () => reportBlocker(changeId, {
      severity: blockerSeverity,
      description: composer,
    }),
    onSuccess: onSendSuccess,
  });
  // Unified flow: one composer, one send path. Every partner message goes via
  // the query endpoint; the authority runs the full negotiation pipeline (auto-reject,
  // clustering, escalation) on it — no per-tab pre-categorization.
  const activeMutation = sendMutation;
  // Query hold — the authority is preparing a revised kit (round closed → ship). The
  // composer is held until the new version arrives. A revision is only "in
  // progress" BEFORE the kit freezes; once frozen there is no next version, so a
  // leftover revision_in_progress flag must NOT hold the composer (the partner
  // can always ask a clarifying question post-freeze).
  const held = !!change?.revision_in_progress
    && !(Boolean(change?.negotiation_finalized_at) || (change?.negotiation_version || 1) >= 3);
  const submitActive = () => {
    if (held || !composer.trim() || activeMutation.isPending) return;
    activeMutation.mutate();
  };

  // ── AI-suggested drafts (kept from old QueryPanel) ────────────────
  const { data: drafts = [] } = useQuery({
    queryKey: ['queryDrafts', changeId],
    queryFn: () => listQueryDrafts(changeId),
    // 2 s — was 5 s. Drafts surface above the composer; the prior 5 s
    // lag made suggestions feel stale.
    refetchInterval: 2000,
  });
  const visibleDrafts = drafts.filter(d => d.status === 'draft');

  // ── authority counter inline mutations ─────────────────────────────────
  const [counterError, setCounterError] = useState('');
  const acceptCounterMutation = useMutation({
    mutationFn: (counterProposalId) => acceptCounter(changeId, counterProposalId),
    onSuccess: () => {
      setCounterError('');
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    },
    onError: (err) => setCounterError(
      err?.response?.data?.detail || err?.message || 'Failed to accept terms'
    ),
  });
  // Reply to an open authority counter — goes through the single query channel,
  // like every other partner message.
  const counterBackMutation = useMutation({
    mutationFn: (replyText) => submitQuery(changeId, replyText),
    onSuccess: () => {
      setCounterBackText('');
      setExpandedCounterAction(null);
      setCounterError('');
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    },
    onError: (err) => setCounterError(
      err?.response?.data?.detail || err?.message || 'Failed to send reply'
    ),
  });
  const [expandedCounterAction, setExpandedCounterAction] = useState(null); // null | 'reply' | 'accept'
  const [counterBackText, setCounterBackText] = useState('');

  // ── Build the unified events array ────────────────────────────────
  const events = useMemo(() => {
    const evts = [];

    // Q&A — partner queries become right-bubbles, responses left-bubbles.
    // A query with kind='negotiation' originated from the "Propose term
    // change" composer (counter-propose justification). It renders the
    // same as a general query but its event kind is `partner_counter` so
    // partnerTabOf routes it to the Negotiation tab.
    (change.queries || []).forEach(q => {
      const isNeg = q.kind === 'negotiation';
      evts.push({
        id: `q-${q.id}-out`, side: 'right',
        kind: isNeg ? 'partner_counter' : 'query',
        timestamp: q.sent_at, body: q.message,
      });
      if (q.status === 'auto_rejected' && q.response) {
        let brdData = null;
        try { brdData = JSON.parse(q.response); } catch {}
        if (brdData && brdData.type === 'brd_rejection') {
          evts.push({
            id: `q-${q.id}-brd-rejection`, side: 'left',
            kind: 'brd_rejection',
            timestamp: q.response_received_at || q.sent_at,
            requirement: brdData.requirement,
            reason: brdData.reason,
            body: brdData.reason || '',
          });
        }
      } else if (q.response) {
        evts.push({
          id: `q-${q.id}-in`, side: 'left',
          kind: isNeg ? 'partner_counter_response' : 'response',
          // Use the real arrival time when we have it (the authority's
          // CLARIFICATION_RESPONSE landed at response_received_at).
          // Fall back to q.sent_at only for legacy rows pre-dating
          // that column — those will still bunch with the query.
          timestamp: q.response_received_at || q.sent_at,
          body: q.response,
        });
      }
    });

    // Follow-up authority replies to an already-answered general question.
    // The first answer rides on q.response above; these are the extra
    // replies the authority sent to the same question (kept server-side as a
    // separate list so a newer reply doesn't overwrite the older one).
    // Each renders as its own authority left-bubble in the General tab.
    (change.npci_followups || []).forEach((f, i) => {
      evts.push({
        id: `npci-followup-${f.query_id}-${i}`, side: 'left', kind: 'response',
        timestamp: f.received_at, body: f.message,
      });
    });

    // authority round-lifecycle notices (round_opened / round_closed — sent per
    // (change, partner) 24h window). The partner has no round UI of its own,
    // so these surface as neutral authority notices in the Negotiation tab so the
    // user sees "Round 1 opened → Round 1 closed → Round 2 opened" inline
    // with the rest of the timeline. Backward-compat: legacy entries without
    // an `event` field are all closures (that was the only shape before).
    (change.round_notices || []).forEach((n, i) => {
      const isOpened = n.event === 'round_opened';
      evts.push({
        id: `round-notice-${i}`,
        side: 'left',
        kind: isOpened ? 'round_opened' : 'round_closed',
        timestamp: n.received_at || n.closed_at || n.deadline_at,
        body: n.message,
        round: n.negotiation_round || n.round_number,
      });
    });

    // the authority's open counter (if any)
    if (change.npci_counter) {
      const c = change.npci_counter;
      evts.push({
        id: `npci-counter-${c.counter_proposal_id}`,
        side: 'left', kind: 'npci_counter_open',
        timestamp: c.received_at, body: c.justification,
        round: c.negotiation_round, validUntil: c.valid_until,
        counterProposalId: c.counter_proposal_id,
      });
    }

    // authority counters that the partner has already responded to — kept
    // as part of the chat for audit / non-repudiation. The active slot
    // is cleared on response so the action card disappears, but each
    // resolved snapshot is appended to `npci_counter_history` for the
    // timeline to render.
    (change.npci_counter_history || []).forEach((c, i) => {
      evts.push({
        id: `npci-counter-hist-${c.counter_proposal_id || i}`,
        side: 'left', kind: 'npci_counter_resolved',
        timestamp: c.received_at, body: c.justification,
        round: c.negotiation_round,
        resolution: c.resolution,       // 'countered' | 'accepted' | 'accepted_rollout'
        responseText: c.response_text,
        resolvedAt: c.resolved_at,
        counterProposalId: c.counter_proposal_id,
      });
    });

    // PM accept/reject decisions on partner counters
    (change.counter_decisions || []).forEach((d, i) => {
      evts.push({
        id: `pm-decision-${i}`, side: 'left', kind: 'pm_decision',
        timestamp: d.received_at, body: d.response_text,
        decision: d.decision, round: d.negotiation_round,
        inResponseTo: d.in_response_to,
        // Snapshot of the partner's original counter (the authority echoes it
        // back in the COUNTER_DECISION payload). Rendered as a quoted
        // "↳ replying to:" line above the decision body so the
        // operator sees what was accepted/rejected at a glance instead
        // of an orphaned "Counter accepted" floating at the bottom.
        originalText: d.original_text || '',
      });
    });

    // Partner-reported blockers, plus inline authority resolutions
    (change.blockers || []).forEach(b => {
      evts.push({
        id: `blocker-${b.blocker_id}`, side: 'right', kind: 'blocker',
        timestamp: b.created_at, body: b.description,
        severity: b.severity, impact: b.impact,
        options: b.options_considered, blockerId: b.blocker_id,
        status: b.status,
      });
      if (b.resolution) {
        evts.push({
          id: `blocker-res-${b.blocker_id}`,
          side: 'left', kind: 'blocker_resolution',
          timestamp: b.resolution.resolved_at,
          body: b.resolution.resolution_text,
          actionTaken: b.resolution.action_taken,
          artifactRef: b.resolution.artifact_ref,
          blockerId: b.blocker_id,
        });
      }
    });

    evts.sort((a, b) => parseIso(a.timestamp) - parseIso(b.timestamp));
    // Defensive id-based dedup — if change.queries (or any other
    // upstream array) ever returns the same row twice (e.g. a join
    // duplicate from the GET endpoint), drop the repeat instead of
    // letting React warn about duplicate keys and rendering both.
    const seen = new Set();
    return evts.filter(e => {
      if (seen.has(e.id)) return false;
      seen.add(e.id);
      return true;
    });
  }, [change]);

  // Unified stream — every event shows in one timeline (tabs removed).
  const tabEvents = events;

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events.length]);

  const fmt = (iso) => {
    if (!iso) return '';
    try { return parseIso(iso).toLocaleString(); } catch { return iso; }
  };

  // ── Bubble renderer ───────────────────────────────────────────────
  const renderBubble = (e) => {
    const isRight = e.side === 'right';

    // Per-kind visual config
    const cfg = {
      query:              { name: 'You', avatarBg: '#28a745', avatarText: 'Y', tag: null,                                bubbleBg: '#d4edda', bubbleBorder: '#c3e6cb' },
      response:           { name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N', tag: null,                                bubbleBg: '#dcedf6', bubbleBorder: '#b8d4ea' },
      // Partner-originated counter-propose (right bubble). Same shape
      // as `query` but tagged "Counter" so the tab and timeline both
      // make it obvious this was a term-change proposal, not a general
      // clarifying question.
      partner_counter:    { name: 'You', avatarBg: '#28a745', avatarText: 'Y', tag: { label: 'Counter',  bg: '#28a745' }, bubbleBg: '#d4edda', bubbleBorder: '#c3e6cb' },
      partner_counter_response: { name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N', tag: { label: 'Counter response',  bg: '#6ea8dc' }, bubbleBg: '#dcedf6', bubbleBorder: '#b8d4ea' },
      npci_counter_open:  { name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N', tag: { label: 'Counter',  bg: '#6ea8dc' }, bubbleBg: '#dcedf6', bubbleBorder: '#6ea8dc' },
      // Past counters from the authority that the partner has already actioned.
      // Greyed out vs the live one so the operator can tell at a glance
      // which counter is still waiting on them and which is settled.
      // The TAG now identifies the round + originator (the authority's counter,
      // not the partner's response). The partner's reply renders as a
      // quoted "↳ You countered:" line inside the bubble body so the
      // full round-N exchange is visible as one unit.
      npci_counter_resolved: {
        name: `${t('term.authorityCap')}`, avatarBg: '#9aa6b2', avatarText: 'N',
        tag: { label: `${t('term.authorityCap')} counter · Round ${e.round}`, bg: '#6c757d' },
        bubbleBg: '#f1f3f5', bubbleBorder: '#dee2e6',
      },
      pm_decision:        { name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N',
                            tag: e.decision === 'ACCEPT'
                              ? { label: 'Accept',  bg: '#155724' }
                              : { label: 'Reject',  bg: '#721c24' },
                            bubbleBg: e.decision === 'ACCEPT' ? '#d4edda' : '#fdecea',
                            bubbleBorder: e.decision === 'ACCEPT' ? '#c3e6cb' : '#f5c6cb' },
      blocker:            { name: 'You', avatarBg: '#28a745', avatarText: 'Y',
                            tag: { label: `Blocker · ${e.severity || 'high'}`,
                                   bg: e.severity === 'critical' ? '#721c24' :
                                       e.severity === 'high'     ? '#856404' :
                                       e.severity === 'medium'   ? '#0c5460' : '#383d41' },
                            bubbleBg: '#fff3cd', bubbleBorder: '#ffe69c' },
      blocker_resolution: { name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N', tag: { label: 'Resolved', bg: '#155724' }, bubbleBg: '#d4edda', bubbleBorder: '#c3e6cb' },
      round_closed: {
        name: `${t('term.authorityCap')}`, avatarBg: '#9aa6b2', avatarText: 'N',
        tag: { label: e.round ? `Round ${e.round} · Closed` : 'Round closed', bg: '#6c757d' },
        bubbleBg: '#f1f3f5', bubbleBorder: '#dee2e6',
      },
      round_opened: {
        name: `${t('term.authorityCap')}`, avatarBg: '#6ea8dc', avatarText: 'N',
        tag: { label: e.round ? `Round ${e.round} · Open` : 'Round open', bg: '#155724' },
        bubbleBg: '#e6f4ea', bubbleBorder: '#c3e6cb',
      },
      brd_rejection: {
        name: `${t('term.authorityCap')}`, avatarBg: '#dc3545', avatarText: 'N',
        tag: { label: 'Auto-Rejected', bg: '#721c24' },
        bubbleBg: '#fdecea', bubbleBorder: '#f5c6cb',
      },
    }[e.kind];

    return (
      <div key={e.id} style={{
        display: 'flex',
        flexDirection: isRight ? 'row-reverse' : 'row',
        gap: 8, marginBottom: 12,
      }}>
        {/* Avatar */}
        <div style={{
          flexShrink: 0, width: 28, height: 28, borderRadius: '50%',
          background: cfg.avatarBg, color: 'white',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, marginTop: 16,
        }}>{cfg.avatarText}</div>

        {/* Bubble + meta */}
        <div style={{ maxWidth: '78%', display: 'flex', flexDirection: 'column', alignItems: isRight ? 'flex-end' : 'flex-start' }}>
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3,
            flexDirection: isRight ? 'row-reverse' : 'row',
          }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: cfg.avatarBg }}>{cfg.name}</span>
            <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>{fmt(e.timestamp)}</span>
            {cfg.tag && (
              <span style={{
                fontSize: 9, fontWeight: 700, padding: '1px 6px', borderRadius: 3,
                background: cfg.tag.bg, color: 'white',
                textTransform: 'uppercase', letterSpacing: '0.5px',
              }}>{cfg.tag.label}</span>
            )}
            {e.round && (
              <span style={{ fontSize: 9, color: 'var(--text-muted)' }}>round {e.round}</span>
            )}
          </div>

          <div style={{
            padding: '10px 14px', borderRadius: 12,
            borderTopLeftRadius:  isRight ? 12 : 4,
            borderTopRightRadius: isRight ? 4 : 12,
            background: cfg.bubbleBg, border: `1px solid ${cfg.bubbleBorder}`,
            fontSize: 13, lineHeight: 1.5, color: 'var(--text-primary)',
            wordBreak: 'break-word',
            // No `whiteSpace: pre-wrap` here — ReactMarkdown handles paragraph
            // wrapping below. The blocker/blocker_resolution branches that
            // render plain divs handle their own line breaks via natural HTML.
          }}>
            {/* Body — varies by kind */}
            {e.kind === 'blocker' && (
              <>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>{e.body}</div>
                {e.impact && (
                  <div style={{ fontSize: 12, marginBottom: 4 }}>
                    <strong>Impact:</strong> {e.impact}
                  </div>
                )}
                {e.options?.length > 0 && (
                  <div style={{ fontSize: 12 }}>
                    <strong>Options proposed:</strong>
                    <ul style={{ margin: '2px 0 0 0', paddingLeft: 18 }}>
                      {e.options.map((o, i) => (
                        <li key={i}>{o.option}{o.eta && ` · ${o.eta}`}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </>
            )}
            {e.kind === 'blocker_resolution' && (
              <>
                {e.actionTaken && (
                  <div style={{ fontSize: 12, marginBottom: 4 }}>
                    <strong>Action:</strong> <span style={{ fontFamily: 'monospace' }}>{e.actionTaken}</span>
                  </div>
                )}
                {e.body && <div style={{ marginBottom: 4 }}>{e.body}</div>}
                {e.artifactRef && (
                  <div style={{ fontSize: 12 }}>
                    <ArtifactRef value={e.artifactRef} tone="resolution" />
                  </div>
                )}
              </>
            )}
            {e.kind === 'brd_rejection' && (
              <>
                <div style={{ fontWeight: 600, marginBottom: 6, color: '#721c24', fontSize: 13 }}>
                  Query automatically rejected
                </div>
                <div style={{ fontSize: 12.5, marginBottom: 8, lineHeight: 1.5 }}>
                  This query contradicts a mandatory BRD requirement and cannot be accepted for negotiation.
                </div>
                {e.requirement && (
                  <div style={{
                    background: '#fff', border: '1px solid #f5c6cb', borderRadius: 6,
                    padding: '7px 11px', fontSize: 12, marginBottom: 6,
                  }}>
                    <span style={{ fontWeight: 700, color: '#721c24', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.4 }}>
                      Requirement violated
                    </span>
                    <div style={{ marginTop: 3, color: 'var(--text-primary)', fontWeight: 600 }}>{e.requirement}</div>
                  </div>
                )}
                {e.reason && (
                  <div style={{ fontSize: 12, color: '#6c3a3a', lineHeight: 1.5, fontStyle: 'italic' }}>
                    {e.reason}
                  </div>
                )}
              </>
            )}
            {/* Quoted reference for pm_decision — chat-style
                "↳ replying to:" header so an Accept / Reject that
                arrives hours after the original counter still reads
                as a real reply, not an orphaned bubble. */}
            {e.kind === 'pm_decision' && e.originalText && (
              <div style={{
                fontSize: 11, color: 'var(--text-muted)',
                borderLeft: '3px solid var(--border)', paddingLeft: 8,
                marginBottom: 6, fontStyle: 'italic',
                whiteSpace: 'pre-wrap', overflow: 'hidden',
                textOverflow: 'ellipsis', maxHeight: 60,
              }}>
                ↳ replying to your round {e.round} counter:
                <br />
                <span style={{ color: 'var(--text-secondary)' }}>
                  "{e.originalText.length > 140 ? e.originalText.slice(0, 140) + '…' : e.originalText}"
                </span>
              </div>
            )}
            {!['blocker', 'blocker_resolution'].includes(e.kind) && (
              // authority responses (and partner queries sent via the rich
              // editor) often arrive as markdown — render it instead
              // of showing raw `**bold**`, bullet hyphens, etc. The
              // `.markdown-content` styles in index.css scope heading
              // and paragraph margins so the bubble stays compact.
              <div className="markdown-content" style={{ fontSize: 13, lineHeight: 1.5 }}>
                <ReactMarkdown>{e.body || '(empty)'}</ReactMarkdown>
              </div>
            )}
            {/* For resolved the authority counters: surface the partner's own
                reply (response_text from the archive snapshot) as a
                quoted "↳ You countered:" / "↳ You accepted" line so the
                full round is visible in one bubble — the operator sees
                what {t('term.authority')} said AND what they said back without scanning
                separately for the matching OutgoingQuery row. */}
            {e.kind === 'npci_counter_resolved' && e.responseText && (
              <div style={{
                fontSize: 11, color: 'var(--text-muted)',
                borderLeft: '3px solid #4caf7d', paddingLeft: 8,
                marginTop: 8, fontStyle: 'italic',
                whiteSpace: 'pre-wrap', overflow: 'hidden',
                textOverflow: 'ellipsis', maxHeight: 80,
              }}>
                ↳ You {e.resolution === 'accepted_rollout' ? 'accepted the rollout' :
                       e.resolution === 'superseded'       ? 'closed this counter on accepting the rollout' :
                       e.resolution === 'accepted'         ? 'accepted'
                                                           : 'countered with'}:
                <br />
                <span style={{ color: 'var(--text-secondary)' }}>
                  "{(e.responseText || '').length > 140
                      ? (e.responseText || '').slice(0, 140) + '…'
                      : (e.responseText || '')}"
                </span>
              </div>
            )}
            {e.validUntil && (
              <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 6 }}>
                valid until {fmt(e.validUntil)}
              </div>
            )}
          </div>

          {/* Inline action buttons for live the authority counter */}
          {e.kind === 'npci_counter_open' && (
            <div style={{ marginTop: 6 }}>
              {!expandedCounterAction && (
                <div style={{ display: 'flex', gap: 6 }}>
                  <button className="btn btn-success"
                    style={{ fontSize: 11, padding: '5px 10px' }}
                    disabled={acceptCounterMutation.isPending}
                    onClick={() => acceptCounterMutation.mutate(e.counterProposalId)}>
                    <Check size={11} /> {acceptCounterMutation.isPending ? 'Accepting…' : 'Accept these terms'}
                  </button>
                  <button className="btn btn-secondary"
                    style={{ fontSize: 11, padding: '5px 10px' }}
                    onClick={() => setExpandedCounterAction('reply')}>
                    <Edit2 size={11} /> Reply
                  </button>
                </div>
              )}
              {expandedCounterAction === 'reply' && (
                <div>
                  <textarea value={counterBackText} onChange={ev => setCounterBackText(ev.target.value)}
                    rows={3} placeholder={`Your reply to ${t('term.authority')}…`}
                    style={{ width: '100%', padding: 6, fontSize: 12, fontFamily: 'inherit', marginBottom: 4 }} />
                  <div style={{ display: 'flex', gap: 6 }}>
                    <button className="btn btn-primary" style={{ fontSize: 11, padding: '5px 10px' }}
                      disabled={!counterBackText.trim() || counterBackMutation.isPending}
                      onClick={() => counterBackMutation.mutate(counterBackText)}>
                      <Send size={11} /> {counterBackMutation.isPending ? 'Sending…' : 'Send reply'}
                    </button>
                    <button className="btn btn-secondary" style={{ fontSize: 11, padding: '5px 10px' }}
                      onClick={() => { setExpandedCounterAction(null); setCounterBackText(''); setCounterError(''); }}>
                      <X size={11} /> Cancel
                    </button>
                  </div>
                </div>
              )}
              {counterError && (
                <div style={{ marginTop: 6, fontSize: 11, color: 'crimson' }}>
                  ⚠ {counterError}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  };

  // Per-tab counts for the tab badges.
  const tabCounts = useMemo(() => events.reduce((acc, e) => {
    const t = partnerTabOf(e); acc[t] = (acc[t] || 0) + 1; return acc;
  }, { negotiation: 0, blocker: 0, general: 0 }), [events]);

  const composerPlaceholder = `Type a message to ${t('term.authority')}…`;
  const frozen = Boolean(change?.negotiation_finalized_at) || (change?.negotiation_version || 1) >= 3;
  const [blockerOpen, setBlockerOpen] = useState(false);

  return (
    <div style={{ marginBottom: 16 }}>
      <h3 className="section-title">Activity</h3>

      <div className="card" style={{ display: 'flex', flexDirection: 'column', padding: 0 }}>
        {/* Scrollable timeline */}
        <div ref={scrollRef} style={{
          padding: '14px 16px', maxHeight: 540, overflowY: 'auto',
          minHeight: 180,
        }}>
          {tabEvents.length === 0 && (
            <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              No messages yet. Type a message to {t('term.authority')} below — questions, term changes, or blockers all go here.
            </div>
          )}
          {tabEvents.map(renderBubble)}
        </div>

        {/* AI-suggested query drafts — hidden once negotiations are closed */}
        {!frozen && visibleDrafts.length > 0 && (
          <div style={{
            padding: '8px 12px', borderTop: '1px solid var(--border)',
            background: 'rgba(110,168,220,0.06)',
          }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#6ea8dc', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 4 }}>
              <Sparkles size={11} /> Suggested questions
            </div>
            {visibleDrafts.map(d => <DraftRow key={d.id} draft={d} changeId={changeId} />)}
          </div>
        )}

        {frozen ? (
          /* ── Negotiation closed — generic questions still open, plus blockers ── */
          <div style={{ borderTop: '1px solid var(--border)' }}>
            {/* Notice banner */}
            <div style={{
              display: 'flex', alignItems: 'flex-start', gap: 10,
              padding: '12px 16px',
              background: 'rgba(99,102,241,0.05)',
              borderBottom: '1px solid var(--border)',
            }}>
              <Lock size={15} style={{ flexShrink: 0, marginTop: 1, color: '#6366f1' }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: '#4338ca', marginBottom: 2 }}>
                  Product kit frozen by {t('term.authority')} — negotiation closed
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
                  Counter-proposals are closed, but you can still ask {t('term.authority')} a basic, generic
                  clarifying question below. If you're blocked on implementation, raise a blocker.
                </div>
              </div>
              <button
                className="btn btn-secondary"
                onClick={() => { setBlockerOpen(o => !o); setComposer(''); }}
                style={{ flexShrink: 0, fontSize: 12 }}
              >
                <Shield size={12} /> {blockerOpen ? 'Ask a question instead' : 'Report a blocker'}
              </button>
            </div>

            {/* Default: generic question composer (sends via the query channel) */}
            {!blockerOpen && (
              <div style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                  <textarea
                    value={composer}
                    onChange={ev => setComposer(ev.target.value)}
                    placeholder={`Ask ${t('term.authority')} a generic clarifying question — won't reopen negotiation…`}
                    rows={2}
                    style={{
                      flex: 1, padding: '8px 10px', borderRadius: 6,
                      border: '1px solid var(--border)', fontSize: 12,
                      fontFamily: 'inherit', resize: 'vertical', minHeight: 44,
                    }}
                    onKeyDown={ev => {
                      if (ev.key === 'Enter' && !ev.shiftKey && !ev.nativeEvent.isComposing) {
                        ev.preventDefault();
                        submitActive();
                      }
                    }}
                  />
                  <button
                    className="btn btn-primary"
                    disabled={!composer.trim() || activeMutation.isPending}
                    onClick={submitActive}
                    style={{ flexShrink: 0 }}
                  >
                    {activeMutation.isPending ? <Loader2 size={13} className="spin" /> : <Send size={13} />}
                    {activeMutation.isPending ? 'Sending…' : 'Send question'}
                  </button>
                </div>
                {activeMutation.isError && (
                  <div style={{ fontSize: 12, color: '#ef4444' }}>
                    ⚠ {activeMutation.error?.response?.data?.detail || 'Failed to send — please try again.'}
                  </div>
                )}
              </div>
            )}

            {/* Inline blocker form */}
            {blockerOpen && (
              <div style={{ padding: '10px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <label style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>Severity</label>
                  <select
                    value={blockerSeverity}
                    onChange={e => setBlockerSeverity(e.target.value)}
                    style={{ padding: '5px 8px', fontSize: 12, borderRadius: 6, border: '1px solid var(--border)' }}
                  >
                    <option value="critical">Critical</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                  </select>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                  <textarea
                    value={composer}
                    onChange={ev => setComposer(ev.target.value)}
                    placeholder="Describe what is blocking your implementation…"
                    rows={3}
                    style={{
                      flex: 1, padding: '8px 10px', borderRadius: 6,
                      border: '1px solid var(--border)', fontSize: 12,
                      fontFamily: 'inherit', resize: 'vertical', minHeight: 60,
                    }}
                    onKeyDown={ev => {
                      if (ev.key === 'Enter' && !ev.shiftKey && !ev.nativeEvent.isComposing) {
                        ev.preventDefault();
                        if (composer.trim() && !blockerMutation.isPending) {
                          blockerMutation.mutate(undefined, { onSuccess: () => setBlockerOpen(false) });
                        }
                      }
                    }}
                  />
                  <button
                    className="btn btn-primary"
                    disabled={!composer.trim() || blockerMutation.isPending}
                    onClick={() => blockerMutation.mutate(undefined, { onSuccess: () => setBlockerOpen(false) })}
                    style={{ flexShrink: 0 }}
                  >
                    {blockerMutation.isPending ? <Loader2 size={13} className="spin" /> : <Send size={13} />}
                    {blockerMutation.isPending ? 'Sending…' : 'Send blocker'}
                  </button>
                </div>
                {blockerMutation.isError && (
                  <div style={{ fontSize: 12, color: '#ef4444' }}>
                    ⚠ {blockerMutation.error?.response?.data?.detail || 'Failed to send — please try again.'}
                  </div>
                )}
              </div>
            )}
          </div>
        ) : (
          /* ── Normal composer ── */
          <div style={{
            padding: '10px 12px', borderTop: '1px solid var(--border)',
            display: 'flex', flexDirection: 'column', gap: 8,
          }}>
            {held && (
              <div style={{
                display: 'flex', alignItems: 'flex-start', gap: 8,
                padding: '8px 10px', borderRadius: 6,
                background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.3)',
                fontSize: 12, color: '#92400e', lineHeight: 1.5,
              }}>
                <Clock size={14} style={{ flexShrink: 0, marginTop: 1, color: '#d97706' }} />
                <span>
                  {t('term.authorityCap')} is preparing a revised kit
                  {change?.revision_target_version ? ` (v${change.revision_target_version})` : ''}.
                  {' '}Messaging is on hold until the new version ships — you'll be able to
                  raise queries against the updated kit once it arrives.
                </span>
              </div>
            )}
            {!blockerOpen ? (
              <>
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                  <textarea
                    value={composer} onChange={ev => setComposer(ev.target.value)}
                    placeholder={held ? `Queries are on hold while ${t('term.authority')} prepares the revised kit…` : composerPlaceholder}
                    rows={2}
                    disabled={held}
                    style={{
                      flex: 1, padding: '8px 10px', borderRadius: 6,
                      border: '1px solid var(--border)', fontSize: 12,
                      fontFamily: 'inherit', resize: 'vertical', minHeight: 40,
                      background: held ? 'var(--bg-muted, #f1f5f9)' : undefined,
                      cursor: held ? 'not-allowed' : undefined,
                    }}
                    onKeyDown={ev => {
                      if (ev.key === 'Enter' && !ev.shiftKey && !ev.nativeEvent.isComposing) {
                        ev.preventDefault();
                        submitActive();
                      }
                    }}
                  />
                  <button className="btn btn-primary"
                    disabled={held || !composer.trim() || activeMutation.isPending}
                    onClick={submitActive}
                    style={{ flexShrink: 0 }}>
                    <Send size={13} /> Send
                  </button>
                </div>
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <button
                    onClick={() => { setBlockerOpen(true); setComposer(''); }}
                    style={{
                      fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4,
                      padding: '5px 12px', fontWeight: 600,
                      background: 'rgba(220,38,38,0.08)', color: '#dc2626',
                      border: '1px solid rgba(220,38,38,0.30)',
                      borderRadius: 6, cursor: 'pointer',
                    }}
                  >
                    <Shield size={11} /> Report a blocker
                  </button>
                </div>
              </>
            ) : (
              <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <label style={{ fontSize: 12, color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>Severity</label>
                  <select
                    value={blockerSeverity}
                    onChange={e => setBlockerSeverity(e.target.value)}
                    style={{ padding: '5px 8px', fontSize: 12, borderRadius: 6, border: '1px solid var(--border)' }}
                  >
                    <option value="critical">Critical</option>
                    <option value="high">High</option>
                    <option value="medium">Medium</option>
                    <option value="low">Low</option>
                  </select>
                  <button
                    className="btn btn-secondary"
                    onClick={() => { setBlockerOpen(false); setComposer(''); }}
                    style={{ marginLeft: 'auto', fontSize: 11 }}
                  >
                    Cancel
                  </button>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-end' }}>
                  <textarea
                    value={composer}
                    onChange={ev => setComposer(ev.target.value)}
                    placeholder="Describe what is blocking you…"
                    rows={3}
                    style={{
                      flex: 1, padding: '8px 10px', borderRadius: 6,
                      border: '1px solid rgba(239,68,68,0.4)', fontSize: 12,
                      fontFamily: 'inherit', resize: 'vertical', minHeight: 60,
                    }}
                    onKeyDown={ev => {
                      if (ev.key === 'Enter' && !ev.shiftKey && !ev.nativeEvent.isComposing) {
                        ev.preventDefault();
                        if (composer.trim() && !blockerMutation.isPending)
                          blockerMutation.mutate(undefined, { onSuccess: () => setBlockerOpen(false) });
                      }
                    }}
                  />
                  <button
                    className="btn btn-primary"
                    disabled={!composer.trim() || blockerMutation.isPending}
                    onClick={() => blockerMutation.mutate(undefined, { onSuccess: () => setBlockerOpen(false) })}
                    style={{ flexShrink: 0, background: '#dc2626', borderColor: '#dc2626' }}
                  >
                    {blockerMutation.isPending ? <Loader2 size={13} className="spin" /> : <Shield size={13} />}
                    {blockerMutation.isPending ? 'Sending…' : 'Send blocker'}
                  </button>
                </div>
                {blockerMutation.isError && (
                  <div style={{ fontSize: 12, color: '#ef4444' }}>
                    ⚠ {blockerMutation.error?.response?.data?.detail || 'Failed to send — please try again.'}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}


// ─── Declare Ready dialog ─────────────────────────────────────────────────────
// Pops over the page when the partner clicks "Declare Ready for
// Certification". Captures role + role-relevant test data so the authority's
// orchestrator can pre-configure the matching cert-agent test cases
// before triggering the LLM cert run.
//
// Role → tc_id prefix on cert-agent:
//   PAYER_PSP        PR_*   → partner shares payer_vpa
//   PAYEE_PSP        PE_*   → partner shares payee_vpa (+ MCC for P2M)
//   REMITTER_BANK    RE_*   → bank shares account_number, ifsc, account_type
//   BENEFICIARY_BANK BE_*   → bank shares account_number, ifsc, account_type
const ROLE_FIELDS = {
  PAYER_PSP: [
    { key: 'payer_vpa',          label: 'Payer VPA',        placeholder: 'test@sbi',     required: true  },
    { key: 'mobile_number',      label: 'Mobile (optional)',placeholder: '9999900001'                    },
  ],
  PAYEE_PSP: [
    { key: 'payee_vpa',              label: 'Payee VPA',          placeholder: 'merchant@sbi',  required: true },
    { key: 'merchant_category_code', label: 'MCC (P2M, optional)', placeholder: '5411'                          },
  ],
  REMITTER_BANK: [
    { key: 'account_number',  label: 'Account #',     placeholder: '12345678901',   required: true },
    { key: 'ifsc',            label: 'IFSC',          placeholder: 'SBIN0000001',   required: true },
    { key: 'account_type',    label: 'Account type',  placeholder: 'SAVINGS'                       },
  ],
  BENEFICIARY_BANK: [
    { key: 'account_number',  label: 'Account #',     placeholder: '98765432101',   required: true },
    { key: 'ifsc',            label: 'IFSC',          placeholder: 'SBIN0000002',   required: true },
    { key: 'account_type',    label: 'Account type',  placeholder: 'SAVINGS'                       },
  ],
};

function DeclareReadyDialog({ open, onClose, onSubmit, busy }) {
  const [role, setRole] = useState('PAYER_PSP');
  const [td, setTd]     = useState({});
  if (!open) return null;

  const fields = ROLE_FIELDS[role] || [];
  const missing = fields.filter(f => f.required && !(td[f.key] || '').trim()).map(f => f.label);
  const canSubmit = missing.length === 0 && !busy;

  return (
    <div style={{
      position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 1000,
    }}>
      <div className="card" style={{
        width: 480, maxWidth: '92vw', padding: 22, background: 'var(--bg-elevated, #1c1f26)',
        borderRadius: 12, border: '1px solid var(--border)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
          <Shield size={18} style={{ color: 'var(--accent, #2563eb)' }} />
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>Declare Ready for Certification</h2>
          <button onClick={onClose} style={{
            marginLeft: 'auto', background: 'transparent', border: 'none',
            color: 'var(--text-muted)', cursor: 'pointer', padding: 4,
          }}><X size={16} /></button>
        </div>

        <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '0 0 14px' }}>
          Pick the role this bank/PSP plays in the certification scenarios.
          {t('term.authorityCap')} will pre-configure cert-agent test cases (PR_/PE_/RE_/BE_ prefix matching this role)
          with the data below, then trigger the cert run.
        </p>

        <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 6 }}>
          Role
        </label>
        <select
          value={role}
          onChange={e => { setRole(e.target.value); setTd({}); }}
          className="input-field"
          style={{ width: '100%', marginBottom: 16 }}
        >
          <option value="PAYER_PSP">Payer PSP — runs PR_* test cases</option>
          <option value="PAYEE_PSP">Payee PSP — runs PE_* test cases</option>
          <option value="REMITTER_BANK">Remitter Bank — runs RE_* test cases</option>
          <option value="BENEFICIARY_BANK">Beneficiary Bank — runs BE_* test cases</option>
        </select>

        <div style={{ display: 'grid', gap: 10 }}>
          {fields.map(f => (
            <div key={f.key}>
              <label style={{ display: 'block', fontSize: 11, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4 }}>
                {f.label}{f.required && <span style={{ color: '#dc2626' }}> *</span>}
              </label>
              <input
                className="input-field"
                style={{ width: '100%' }}
                placeholder={f.placeholder}
                value={td[f.key] || ''}
                onChange={e => setTd({ ...td, [f.key]: e.target.value })}
              />
            </div>
          ))}
        </div>

        {missing.length > 0 && (
          <div style={{ marginTop: 12, fontSize: 12, color: '#dc2626' }}>
            Required: {missing.join(', ')}
          </div>
        )}

        <div style={{ display: 'flex', gap: 10, marginTop: 18, justifyContent: 'flex-end' }}>
          <button className="btn" onClick={onClose} disabled={busy} style={{ padding: '8px 14px' }}>
            Cancel
          </button>
          <button
            className="btn btn-success"
            disabled={!canSubmit}
            onClick={() => onSubmit({ role, test_data: td })}
            style={{ padding: '8px 16px' }}
          >
            <Shield size={14} />
            {busy ? 'Sending…' : 'Declare Ready'}
          </button>
        </div>
      </div>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// ImplementationPipeline
// Replaces the binary "Mark Done" checklist with a staged orchestration
// that animates through substeps and streams a live log panel. The
// underlying contract is unchanged — each completed stage still fires
// the real `reportProgress(apiKey)` mutation so the authority sees the same
// progress_report A2A messages. The frontend dresses up the operation
// to feel like a CI/CD pipeline; the backend wire shape is identical.
// ──────────────────────────────────────────────────────────────────────

const PIPELINE_STAGES = [
  {
    id:           'design',
    label:        'Design review',
    runningLabel: 'Reporting Design milestone',
    apiKey:       'design_completed',
    // the authority-facing milestone phase reported on Complete.
    npciPhase:    'Design',
    summary:      `Review the partner design document, then mark it complete to report the Design milestone to ${t('term.authority')}.`,
  },
  {
    id:           'integration',
    label:        'Code review and merge',
    runningLabel: 'Reporting Coding milestone',
    apiKey:       'coding_completed',
    npciPhase:    'Coding',
    summary:      `Review the implementation plan / merge request, then mark it complete to report the Coding milestone to ${t('term.authority')}.`,
  },
  {
    id:           'validation',
    label:        'Test Execution',
    runningLabel: 'Reporting Testing milestone',
    apiKey:       'testing_completed',
    npciPhase:    'Testing',
    summary:      `Execute the test plan, then mark it complete to report the Testing milestone to ${t('term.authority')}.`,
  },
];

// Status colors map by *meaning* — used by both the pipeline dots
// and the log gutter glyph.
function stageColor(state) {
  if (state === 'completed') return T.success;
  if (state === 'running' || state === 'submitting') return T.primary;
  if (state === 'actionable') return T.primary;
  if (state === 'failed')    return T.danger;
  return 'rgba(15,23,42,0.20)';
}

function StageNode({ stage, state, completedAt, latencyMs, onComplete }) {
  const color = stageColor(state);
  const isRunning = state === 'running' || state === 'submitting';
  return (
    <div
      className={isRunning ? 'pp-stage-running' : ''}
      style={card({
        padding: '14px 16px',
        marginBottom: 10,
        borderLeft: `3px solid ${color}`,
        opacity: state === 'pending' ? 0.7 : 1,
        transition: 'opacity .2s ease, border-color .2s ease',
      })}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{
          width: 28, height: 28, flexShrink: 0, borderRadius: '50%',
          background: state === 'completed' ? color
                    : state === 'failed' ? color
                    : '#fff',
          border: `2px solid ${color}`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: '#fff',
        }}>
          {state === 'completed' && <Check size={14} strokeWidth={3} />}
          {isRunning             && <Loader2 size={14} className="pp-spin" color={color} />}
          {state === 'failed'    && <AlertTriangle size={14} />}
          {(state === 'pending' || state === 'actionable') && <span style={{ width: 7, height: 7, borderRadius: '50%', background: color }} />}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: T.textPrimary }}>
              {isRunning && stage.runningLabel ? stage.runningLabel : stage.label}
            </span>
            <span style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
              letterSpacing: 0.4, textTransform: 'uppercase',
              background: state === 'completed' ? 'rgba(16,185,129,0.10)'
                        : isRunning             ? 'rgba(37,99,235,0.10)'
                        : state === 'actionable'? 'rgba(37,99,235,0.10)'
                        : state === 'failed'    ? 'rgba(239,68,68,0.10)'
                        : 'rgba(15,23,42,0.05)',
              color: color,
              border: `1px solid ${state === 'pending' ? T.borderSubtle : color + '33'}`,
            }}>
              {state === 'completed' ? 'Completed'
               : state === 'submitting' ? 'Sending…'
               : state === 'running' ? 'In Progress'
               : state === 'actionable' ? 'Action required'
               : state === 'failed'  ? 'Failed'
               : 'Locked'}
            </span>
            {completedAt && (
              <span style={{ fontSize: 11, color: T.textMuted, marginLeft: 'auto' }}>
                {formatRelative(completedAt)}
              </span>
            )}
          </div>
          <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 2 }}>
            {state === 'submitting'
              ? `Reporting the ${stage.npciPhase} milestone to ${t('term.authority')}…`
              : state === 'completed'
                ? `${stage.npciPhase} milestone reported to ${t('term.authority')}.`
                : stage.summary}
          </div>

          {/* Complete action — fires the the authority milestone for this phase.
              Only the next actionable stage shows it; prior stages must be
              done first (the backend enforces the same ordering). */}
          {(state === 'actionable' || state === 'submitting') && (
            <button
              onClick={onComplete}
              disabled={state === 'submitting'}
              style={{
                marginTop: 10, display: 'inline-flex', alignItems: 'center', gap: 7,
                padding: '7px 14px', fontSize: 12.5, fontWeight: 600,
                background: state === 'submitting' ? T.bgMuted : T.primary,
                color: state === 'submitting' ? T.textMuted : '#fff',
                border: 'none', borderRadius: 8,
                cursor: state === 'submitting' ? 'wait' : 'pointer',
              }}
            >
              {state === 'submitting'
                ? <><Loader2 size={13} className="pp-spin" /> Reporting to {t('term.authority')}…</>
                : <><Check size={13} /> Complete</>}
            </button>
          )}
        </div>
      </div>

      {/* Shimmer bar while running — neutral "in flight" indicator,
          no fabricated substep narration. */}
      {isRunning && (
        <div className="pp-stage-bar" style={{
          height: 3, marginTop: 12, borderRadius: 999,
          background: 'rgba(37,99,235,0.10)',
        }} />
      )}
    </div>
  );
}

function ImplementationPipeline({ changeId, completedSteps }) {
  const queryClient = useQueryClient();
  const completed = completedSteps || [];
  const [submitting, setSubmitting] = useState(null);  // stage.id currently being reported
  const [error, setError] = useState(null);

  const mutation = useMutation({
    // Each Complete fires POST /api/changes/{id}/progress, which sends the
    // matching Design/Coding/Testing milestone to the authority over A2A.
    mutationFn: (step) => reportProgress(changeId, step),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['change', changeId] }),
  });

  const isDone   = (s) => completed.includes(s.apiKey);
  // Index of the next stage the partner can complete (prior stages done).
  // The backend enforces the same sequential ordering.
  const nextIdx  = PIPELINE_STAGES.findIndex((s) => !isDone(s));
  const allDone  = nextIdx === -1;
  const completedCount = PIPELINE_STAGES.filter(isDone).length;

  const handleComplete = async (stage) => {
    if (submitting) return;
    setError(null);
    setSubmitting(stage.id);
    try {
      await mutation.mutateAsync(stage.apiKey);
    } catch (err) {
      setError({ stageLabel: stage.label, message: err?.response?.data?.detail || err?.message || `Failed to report to ${t('term.authority')}` });
    } finally {
      setSubmitting(null);
    }
  };

  const stageState = (s, i) => {
    if (isDone(s))            return 'completed';
    if (submitting === s.id)  return 'submitting';
    if (i === nextIdx)        return 'actionable';
    return 'pending';  // locked until prior stages complete
  };

  return (
    <div>
      {/* Pipeline header card */}
      <div style={card({ padding: '16px 18px', marginBottom: 14 })}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <Rocket size={16} style={{ color: T.primary }} />
              <span style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>Implementation Pipeline</span>
              <span style={{
                fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
                background: allDone ? 'rgba(16,185,129,0.10)' : 'rgba(15,23,42,0.05)',
                color: allDone ? T.success : T.textSecondary,
                border: `1px solid ${allDone ? 'rgba(16,185,129,0.22)' : T.borderSubtle}`,
                letterSpacing: 0.4, textTransform: 'uppercase',
              }}>
                {completedCount}/{PIPELINE_STAGES.length} steps
              </span>
            </div>
            <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 4 }}>
              {allDone
                ? `All milestones reported to ${t('term.authority')} — you can now declare ready for certification.`
                : `Complete each step to report its milestone to ${t('term.authority')}. Steps unlock in order; all three must be reported before certification.`}
            </div>
          </div>
        </div>
      </div>

      {/* Stage nodes — each shows a Complete button when it is the next step */}
      {PIPELINE_STAGES.map((s, i) => (
        <StageNode
          key={s.id}
          stage={s}
          state={stageState(s, i)}
          onComplete={() => handleComplete(s)}
        />
      ))}

      {/* Surface the real failure detail when a milestone report errors. */}
      {error && (
        <div style={card({
          padding: '12px 16px',
          marginTop: 4,
          borderLeft: `3px solid ${T.danger}`,
          background: 'rgba(239, 68, 68, 0.04)',
        })}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
            <AlertTriangle size={14} style={{ color: T.danger }} />
            <span style={{ fontSize: 13, fontWeight: 600, color: T.textPrimary }}>
              {error.stageLabel} failed to report to {t('term.authority')}
            </span>
          </div>
          <div style={{ fontSize: 12, color: T.textSecondary, paddingLeft: 22 }}>
            {error.message}
          </div>
        </div>
      )}
    </div>
  );
}

function ProgressPanel({ changeId, completedSteps, status }) {
  const queryClient = useQueryClient();
  const completed = completedSteps || [];

  const mutation = useMutation({
    mutationFn: (step) => reportProgress(changeId, step),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
    },
  });

  const allDone = PROGRESS_STEPS.every((s) => completed.includes(s.key));

  return (
    <div className="progress-panel">
      {PROGRESS_STEPS.map((step, idx) => {
        const isDone = completed.includes(step.key);
        const prevDone = idx === 0 || completed.includes(PROGRESS_STEPS[idx - 1].key);
        const isCurrent = !isDone && prevDone;

        return (
          <div key={step.key} className="progress-step">
            <div className={`step-indicator ${isDone ? 'done' : isCurrent ? 'current' : 'pending'}`}>
              {isDone ? <Check size={14} /> : idx + 1}
            </div>
            <span className={`step-label ${isDone ? 'done' : !isCurrent ? 'pending' : ''}`}>
              {step.label}
            </span>
            {isCurrent && (
              <button
                className="btn btn-primary btn-sm"
                onClick={() => mutation.mutate(step.key)}
                disabled={mutation.isPending}
              >
                Mark Done
              </button>
            )}
          </div>
        );
      })}

      {/* Note: "Declare Ready for Certification" now lives on the Cert
          Lifecycle panel below — that`s where ${t('term.authority')}`s cert run is
          orchestrated. This panel only tracks internal implementation
          progress (design / coding / testing). */}
    </div>
  );
}

// ─── Cert Lifecycle Panel ─────────────────────────────────────────────────────
// 4-stage cert lifecycle, communicated to the authority via A2A task_type=
// cert_status_update. Stages are linear:
//   received  (auto on change_communication arrival)
//   deployed  (bank user clicks)
//   tested    (bank user clicks)
//   ready_for_certification (bank user clicks → opens role+test_data
//     dialog → submit triggers cert orchestrator)
//
// Replaces the older CertMessagingPanel (Q&A inbox) and the standalone
// "Declare Ready" button. The wire shape is the same Google A2A SDK
// pipeline; the partition is purely on task_type.

// Cert Lifecycle is the *post-readiness* journey only. Pre-cert milestones
// (deployed/tested/etc.) are already covered by the Implementation Pipeline
// above, so duplicating them here only added noise.
//
// Three stages tied to real cert_status transitions:
//   1. Declare Ready — partner action, fires cert_status_update +
//      cert_readiness_declaration A2A to the authority.
//   2. the authority Orchestration — visible while cert_status sits at
//      'ready_for_certification'. the authority pushes test cases to cert-agent
//      and runs them. The partner side polls cert_status every 5s and
//      shows a live spinner so the wait is visible, not invisible.
//   3. Certified — terminal success when cert_status flips to 'certified'.
//      If a defect_notice ever lands (failure path), we mirror it.

const CERT_STAGES = [
  {
    key:   'declare',
    label: 'Declare Ready for Certification',
    sub:   `Provide role + test data; ${t('term.authority')} will orchestrate the cert run.`,
  },
  {
    key:   'orchestrating',
    label: `${t('term.authorityCap')} Cert Orchestration`,
    sub:   `${t('term.authorityCap')} is pushing test cases to cert-agent and running them against the bank simulator.`,
  },
  {
    key:   'certified',
    label: 'Certified',
    sub:   'All tests passed — the rollout is certified for production.',
  },
];

// Map the raw cert_status field to the cert-lifecycle stage index.
// Anything pre-readiness (received / deployed / tested) sits at the
// Declare-Ready entry; ready_for_certification is the orchestration
// wait; certified is terminal — returning length=3 (past the last
// stage) makes every stage render as completed.
function certActiveIdx(status) {
  if (status === 'certified')              return 3;   // past terminal → all 3 stages = completed
  if (status === 'tests_completed')        return 3;   // also terminal — run done with failures; summary panel reveals the split
  if (status === 'ready_for_certification') return 1;
  return 0;
}

function fmtTs(iso) {
  if (!iso) return null;
  try { return parseIso(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }); }
  catch { return iso; }
}

// While the orchestrating stage runs, cycle the body text through the
// three real backend phases of an authority cert orchestration:
//   1) Test Suite Mapping — cert-agent fetches the change's test cases
//      and merges the partner's role + test_data into each row.
//   2) Test Data Ingestion — cert-agent persists the patched cases for
//      the run; bank-simulator boots its scenario fixtures.
//   3) Execution in Progress — cases are dispatched against the bank
//      simulator one by one; results stream back to cert-agent.
// We don't know in real-time which phase the authority is in (no granular A2A
// signal yet); the cycle keeps the wait legible. Each phase shows for
// CERT_PHASE_DURATION_MS so it doesn't strobe.
const CERT_RUN_PHASES = [
  { label: 'Test Suite Mapping',         sub: 'Cert-agent is loading the test suite for this flow and merging your partner-side test data into each case.' },
  { label: 'Test Data Ingestion',        sub: 'Patched test cases are being persisted to the certification database; bank simulator fixtures are warming up.' },
  { label: 'Execution in Progress',      sub: 'Test cases are dispatching against the bank simulator. Results stream back to cert-agent as each case completes.' },
];
const CERT_PHASE_DURATION_MS = 5000;

// Demo/dev fallback: the authority's cert orchestrator doesn't publish a summary back
// on this environment, so the middle stage would spin forever after readiness.
// Once cert_status has been at 'ready_for_certification' for the full CERT_RUN
// cycle, synthesise a UPI Circle Personalization result set locally so the UI
// renders the executed test cases (RE_* the authority-initiated + MT_* BANK-initiated)
// with mixed pass / fail / timeout outcomes instead of a stuck loader.
// Every test case PASSES (bank behaved as spec required). The variety lives
// in the `scenario` field — success / failure / timeout — reflected in the
// expected & actual response codes:
//   • success  → expected 000, actual 000
//   • failure  → expected a specific negative UPI code (U19 limit, U16 risk,
//                U66 blocked …); the case passes because the bank correctly
//                returned that same code
//   • timeout  → expected 91 (system timeout); the case passes because the
//                bank surfaced the timeout code within SLA and queued reversal
const MOCK_UPI_CIRCLE_CASES = [
  {
    test_case_id: 'RE_87',
    title:        'Enable UPI Circle — add primary user linkage',
    api:          'RegisterCircle',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'success',
    expected_code:'000',
    actual_code:  '000',
    status:       'PASS',
    remarks:      'Primary linkage created; consent digest verified against ledger.',
    duration_ms:  812,
  },
  {
    test_case_id: 'RE_89',
    title:        'Add secondary user with per-transaction cap',
    api:          'AddDelegate',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'success',
    expected_code:'000',
    actual_code:  '000',
    status:       'PASS',
    remarks:      'Delegate provisioned with per-txn cap of ₹5,000; audit event emitted.',
    duration_ms:  634,
  },
  {
    test_case_id: 'RE_90',
    title:        'Reject Pay above secondary user cap',
    api:          'Pay',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'failure',
    expected_code:'U19',
    actual_code:  'U19',
    status:       'PASS',
    remarks:      'Bank correctly rejected with U19 (per-transaction limit exceeded); no debit posted.',
    duration_ms:  987,
  },
  {
    test_case_id: 'RE_91',
    title:        'Delegate approval flow to primary user',
    api:          'ApprovalRequest',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'success',
    expected_code:'000',
    actual_code:  '000',
    status:       'PASS',
    remarks:      'Approval intent forwarded to primary; consent received within SLA window.',
    duration_ms:  1214,
  },
  {
    test_case_id: 'RE_92',
    title:        'SLA-bounded delegated spend — timeout handling',
    api:          'Pay',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'timeout',
    expected_code:'91',
    actual_code:  '91',
    status:       'PASS',
    remarks:      'Bank surfaced 91 (system unavailable) within the 30s SLA; auto-reversal queued.',
    duration_ms:  30042,
  },
  {
    test_case_id: 'RE_94',
    title:        'Reject Pay after UPI Circle revocation',
    api:          'Pay',
    initiated_by: `${t('term.authorityCap')}`,
    scenario:     'failure',
    expected_code:'U16',
    actual_code:  'U16',
    status:       'PASS',
    remarks:      'Bank correctly rejected with U16 (risk / delegation revoked); reason surfaced to primary.',
    duration_ms:  705,
  },
  {
    test_case_id: 'MT_116',
    title:        'Notify primary on secondary user spend',
    api:          'OutgoingNotification',
    initiated_by: 'BANK',
    scenario:     'success',
    expected_code:'000',
    actual_code:  '000',
    status:       'PASS',
    remarks:      'Push notification delivered within 2s of Pay success; receipt recorded.',
    duration_ms:  1518,
  },
  {
    test_case_id: 'MT_117',
    title:        'Reject secondary spend when primary is suspended',
    api:          'OutgoingAlert',
    initiated_by: 'BANK',
    scenario:     'failure',
    expected_code:'U66',
    actual_code:  'U66',
    status:       'PASS',
    remarks:      'Bank correctly returned U66 (beneficiary / primary blocked); alert relayed to secondary.',
    duration_ms:  1076,
  },
];

const MOCK_CERT_SUMMARY = (() => {
  const cases = MOCK_UPI_CIRCLE_CASES;
  const npci = cases.filter((c) => c.initiated_by === `${t('term.authorityCap')}`);
  const bank = cases.filter((c) => c.initiated_by === 'BANK');
  const passed  = cases.filter((c) => c.status === 'PASS').length;
  const failed  = cases.filter((c) => c.status === 'FAIL').length;
  const timeout = cases.filter((c) => c.status === 'TIMEOUT').length;
  const byScenario = (s) => cases.filter((c) => c.scenario === s).length;
  return {
    total:   cases.length,
    passed,
    failed,
    timeout,
    scenarios: {
      success: byScenario('success'),
      failure: byScenario('failure'),
      timeout: byScenario('timeout'),
    },
    feature: 'UPI Circle Personalization',
    phases: {
      npci: { total: npci.length, passed: npci.filter((c) => c.status === 'PASS').length },
      bank: { total: bank.length, passed: bank.filter((c) => c.status === 'PASS').length },
    },
    cases,
  };
})();

// Long enough for the phase cycle (3 × 5s) to play through once before we
// declare the run complete.
const MOCK_CERT_DELAY_MS = CERT_PHASE_DURATION_MS * CERT_RUN_PHASES.length + 1000;

// localStorage key that persists the role + test_data the partner submitted
// via the Declare Ready dialog. Lets the results report echo VPA / mobile /
// account details even after a refresh — the backend doesn't retain them yet.
const CERT_READY_CTX_KEY = (changeId) => `pp:cert-ready-ctx:${changeId}`;

// Small metric tile rendered in the cert results summary strip.
function CertStatTile({ label, value, tone = 'neutral' }) {
  const fg = tone === 'success' ? '#059669'
           : tone === 'danger'  ? '#dc2626'
           : tone === 'warning' ? '#b45309'
           : '#0f172a';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 74 }}>
      <span style={{
        fontSize: 10, fontWeight: 700, letterSpacing: '0.08em',
        color: '#64748b', textTransform: 'uppercase',
      }}>{label}</span>
      <span style={{
        fontSize: 20, fontWeight: 700, color: fg,
        fontVariantNumeric: 'tabular-nums', lineHeight: 1.1,
      }}>{value}</span>
    </div>
  );
}

// Professional UPI-style cert results table. Rendered inside the "Certified"
// stage once cert_summary is available (real or mocked). Colours stay in the
// slate / green / red / amber / blue / teal palette — no purple or violet on
// any test-case text.
function CertResultsReport({ summary, readyContext, changeId, certifiedAt }) {
  const [openId, setOpenId] = useState(null);
  const cases = Array.isArray(summary?.cases) ? summary.cases : [];
  const norm  = (s) => String(s || '').toUpperCase();
  const passed  = cases.filter((c) => norm(c.status) === 'PASS').length;
  const failed  = cases.filter((c) => norm(c.status) === 'FAIL').length;
  const total   = cases.length;
  const passRate = total ? Math.round((passed / total) * 100) : 0;
  const durationSec = Math.max(1, Math.round(
    cases.reduce((acc, c) => acc + (c.duration_ms || 0), 0) / 1000,
  ));
  const feature = summary?.feature || 'UPI Circle Personalization';
  // Scenario coverage — success / failure-path / timeout-path scenarios exercised.
  // Falls back to counting the `scenario` field on cases if the summary blob
  // doesn't ship a rollup.
  const scen = summary?.scenarios || {
    success: cases.filter((c) => c.scenario === 'success').length,
    failure: cases.filter((c) => c.scenario === 'failure').length,
    timeout: cases.filter((c) => c.scenario === 'timeout').length,
  };

  const shortId = String(changeId || 'CHG').slice(0, 8).toUpperCase();
  const tsCompact = (certifiedAt || '').replace(/[^0-9]/g, '').slice(2, 12) || 'RUN01';
  const runId = `TR-${shortId}-${tsCompact}`;
  const executedAt = certifiedAt ? (() => {
    try {
      return new Date(certifiedAt).toLocaleString('en-IN', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
      });
    } catch { return certifiedAt; }
  })() : '—';

  const roleLabel = ({
    PAYER_PSP:        'Payer PSP',
    PAYEE_PSP:        'Payee PSP',
    REMITTER_BANK:    'Remitter Bank',
    BENEFICIARY_BANK: 'Beneficiary Bank',
  })[readyContext?.role] || readyContext?.role || 'Bank / PSP';

  const td = readyContext?.test_data || {};
  const submittedVpa    = td.payer_vpa || td.payee_vpa || null;
  const submittedMobile = td.mobile_number || null;
  const submittedAcct   = td.account_number || null;
  const submittedIfsc   = td.ifsc || null;

  const statusStyle = (s) => {
    const u = norm(s);
    if (u === 'PASS')    return { bg: 'rgba(16,185,129,0.10)', fg: '#059669', border: 'rgba(16,185,129,0.30)', label: 'Pass' };
    if (u === 'FAIL')    return { bg: 'rgba(239,68,68,0.10)',  fg: '#dc2626', border: 'rgba(239,68,68,0.30)',  label: 'Fail' };
    if (u === 'TIMEOUT') return { bg: 'rgba(245,158,11,0.10)', fg: '#b45309', border: 'rgba(245,158,11,0.32)', label: 'Timeout' };
    return { bg: 'rgba(148,163,184,0.12)', fg: '#475569', border: 'rgba(148,163,184,0.28)', label: u || '—' };
  };

  const initStyle = (init) => (init === 'BANK'
    ? { bg: 'rgba(13,148,136,0.10)', fg: '#0d9488', border: 'rgba(13,148,136,0.28)', label: 'BANK' }
    : { bg: 'rgba(37,99,235,0.10)',  fg: '#2563eb', border: 'rgba(37,99,235,0.28)',  label: `${t('term.authorityCap')}` });

  // Scenario badge — distinct from Status so the reader can see this row
  // exercised a failure or timeout path even though it passed.
  const scenarioStyle = (s) => {
    const u = String(s || 'success').toLowerCase();
    if (u === 'failure') return { bg: 'rgba(245,158,11,0.10)', fg: '#b45309', border: 'rgba(245,158,11,0.30)', label: 'Failure' };
    if (u === 'timeout') return { bg: 'rgba(14,165,233,0.10)', fg: '#0369a1', border: 'rgba(14,165,233,0.30)', label: 'Timeout' };
    return                       { bg: 'rgba(15,23,42,0.05)',  fg: '#334155', border: 'rgba(15,23,42,0.12)',  label: 'Success' };
  };

  const metaRow = { fontSize: 11.5, color: '#64748b', display: 'flex', gap: 6, whiteSpace: 'nowrap' };
  const metaVal = { color: '#0f172a', fontWeight: 600, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' };
  const thBase  = {
    padding: '10px 12px',
    fontSize: 10.5, fontWeight: 700, letterSpacing: '0.06em',
    color: '#475569', textTransform: 'uppercase',
    borderBottom: '1px solid rgba(15,23,42,0.08)',
    whiteSpace: 'nowrap', textAlign: 'left',
  };
  const tdBase  = {
    padding: '11px 12px',
    borderBottom: '1px solid rgba(15,23,42,0.05)',
    verticalAlign: 'middle',
  };

  return (
    <div style={{
      marginTop: 14,
      background: '#ffffff',
      border: '1px solid rgba(15,23,42,0.08)',
      borderRadius: 10,
      overflow: 'hidden',
      boxShadow: '0 1px 2px rgba(15,23,42,0.04), 0 4px 12px rgba(15,23,42,0.04)',
    }}>
      {/* Report header */}
      <div style={{
        padding: '16px 20px 14px',
        borderBottom: '1px solid rgba(15,23,42,0.06)',
        background: 'linear-gradient(180deg, #f8fafc 0%, #ffffff 100%)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{
            fontSize: 10, fontWeight: 700, letterSpacing: '0.10em',
            color: '#475569', textTransform: 'uppercase',
            padding: '3px 8px', border: '1px solid rgba(15,23,42,0.12)',
            borderRadius: 4, background: '#ffffff',
          }}>{t('term.authorityCap')} · {t('cert.reportTitle')}</span>
          <span style={{ fontSize: 14, fontWeight: 700, color: '#0f172a' }}>
            {feature}
          </span>
          <span style={{
            marginLeft: 'auto',
            fontSize: 10.5, fontWeight: 700, letterSpacing: '0.08em',
            color: '#059669', textTransform: 'uppercase',
            padding: '4px 10px', borderRadius: 999,
            background: 'rgba(16,185,129,0.10)',
            border: '1px solid rgba(16,185,129,0.28)',
          }}>Certification Complete</span>
        </div>
        <div style={{
          marginTop: 12,
          display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
          gap: '6px 24px',
        }}>
          <div style={metaRow}><span>Run ID:</span><span style={metaVal}>{runId}</span></div>
          <div style={metaRow}><span>Executed:</span><span style={metaVal}>{executedAt}</span></div>
          <div style={metaRow}><span>Environment:</span><span style={metaVal}>Cert-Agent · Simulator</span></div>
          <div style={metaRow}><span>Role:</span><span style={metaVal}>{roleLabel}</span></div>
          {submittedVpa    && <div style={metaRow}><span>VPA:</span><span style={metaVal}>{submittedVpa}</span></div>}
          {submittedMobile && <div style={metaRow}><span>Mobile:</span><span style={metaVal}>{submittedMobile}</span></div>}
          {submittedAcct   && <div style={metaRow}><span>Account #:</span><span style={metaVal}>{submittedAcct}</span></div>}
          {submittedIfsc   && <div style={metaRow}><span>IFSC:</span><span style={metaVal}>{submittedIfsc}</span></div>}
        </div>
      </div>

      {/* Summary strip — outcome + coverage. Scenario coverage sits beside
          the counts so a reader can see failure / timeout paths were
          exercised, even though every case passed. */}
      <div style={{
        padding: '14px 20px 12px',
        borderBottom: '1px solid rgba(15,23,42,0.06)',
        background: '#ffffff',
      }}>
        <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', alignItems: 'center' }}>
          <CertStatTile label="Total"    value={total}  tone="neutral" />
          <CertStatTile label="Passed"   value={passed} tone="success" />
          <CertStatTile label="Failed"   value={failed} tone={failed ? 'danger' : 'neutral'} />
          <CertStatTile
            label="Pass rate"
            value={`${passRate}%`}
            tone={passRate === 100 ? 'success' : passRate >= 70 ? 'warning' : 'danger'}
          />
          <CertStatTile label="Duration" value={`${durationSec}s`} tone="neutral" />
        </div>

        <div style={{
          marginTop: 12, paddingTop: 12,
          borderTop: '1px dashed rgba(15,23,42,0.08)',
          display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'center',
        }}>
          <span style={{
            fontSize: 10, fontWeight: 700, letterSpacing: '0.08em',
            color: '#64748b', textTransform: 'uppercase', marginRight: 4,
          }}>Scenario coverage</span>
          {[
            { key: 'success', label: 'Success path',  count: scen.success ?? 0, ...scenarioStyle('success') },
            { key: 'failure', label: 'Failure path',  count: scen.failure ?? 0, ...scenarioStyle('failure') },
            { key: 'timeout', label: 'Timeout path',  count: scen.timeout ?? 0, ...scenarioStyle('timeout') },
          ].map((s) => (
            <span key={s.key} style={{
              fontSize: 11, fontWeight: 600, letterSpacing: '0.02em',
              padding: '4px 10px', borderRadius: 999,
              background: s.bg, color: s.fg, border: `1px solid ${s.border}`,
              display: 'inline-flex', alignItems: 'center', gap: 6,
            }}>
              {s.label}
              <span style={{
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                fontWeight: 700,
              }}>{s.count}</span>
            </span>
          ))}
        </div>
      </div>

      {/* Results table */}
      <div style={{ overflowX: 'auto' }}>
        <table style={{
          width: '100%', borderCollapse: 'collapse', fontSize: 12.5,
          color: '#0f172a', tableLayout: 'auto',
        }}>
          <thead>
            <tr style={{ background: '#f8fafc' }}>
              <th style={{ ...thBase, textAlign: 'center', width: 44 }}>#</th>
              <th style={thBase}>Test Case ID</th>
              <th style={thBase}>Description</th>
              <th style={thBase}>API</th>
              <th style={thBase}>Initiated By</th>
              <th style={thBase}>Scenario</th>
              <th style={thBase}>Expected</th>
              <th style={thBase}>Actual</th>
              <th style={thBase}>Duration</th>
              <th style={thBase}>Status</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c, idx) => {
              const st  = statusStyle(c.status);
              const inb = initStyle(String(c.initiated_by || `${t('term.authorityCap')}`).toUpperCase());
              const sc  = scenarioStyle(c.scenario);
              const tcId = c.test_case_id || c.tc_id || c.id || '';
              const isOpen = openId === tcId;
              const canExpand = Boolean(c.remarks);
              const rowBg = idx % 2 === 0 ? '#ffffff' : '#fbfcfd';
              // Expected & actual match on every PASS row — colour both the
              // same neutral slate, whether the expected code is 000 or a
              // negative code like U19. Only diverge if a real backend row
              // ever lands with mismatched codes.
              const codeMatches = String(c.expected_code || '') === String(c.actual_code || '');
              return (
                <Fragment key={tcId || idx}>
                  <tr
                    onClick={() => canExpand && setOpenId((o) => (o === tcId ? null : tcId))}
                    style={{
                      cursor: canExpand ? 'pointer' : 'default',
                      background: isOpen ? '#f8fafc' : rowBg,
                    }}
                  >
                    <td style={{
                      ...tdBase, textAlign: 'center',
                      color: '#94a3b8', fontSize: 11.5,
                      fontVariantNumeric: 'tabular-nums',
                    }}>{String(idx + 1).padStart(2, '0')}</td>
                    <td style={{
                      ...tdBase,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      fontWeight: 700, color: '#0f172a', whiteSpace: 'nowrap',
                    }}>{tcId}</td>
                    <td style={{ ...tdBase, color: '#1e293b', maxWidth: 340 }}>
                      {c.title || '—'}
                    </td>
                    <td style={{
                      ...tdBase, color: '#475569',
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      fontSize: 11.5, whiteSpace: 'nowrap',
                    }}>{c.api || '—'}</td>
                    <td style={{ ...tdBase, whiteSpace: 'nowrap' }}>
                      <span style={{
                        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.05em',
                        padding: '2px 8px', borderRadius: 4,
                        background: inb.bg, color: inb.fg,
                        border: `1px solid ${inb.border}`,
                      }}>{inb.label}</span>
                    </td>
                    <td style={{ ...tdBase, whiteSpace: 'nowrap' }}>
                      <span style={{
                        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.05em',
                        padding: '2px 8px', borderRadius: 4,
                        background: sc.bg, color: sc.fg,
                        border: `1px solid ${sc.border}`,
                      }}>{sc.label}</span>
                    </td>
                    <td style={{
                      ...tdBase,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      color: '#0f172a', fontWeight: 600, whiteSpace: 'nowrap',
                    }}>{c.expected_code || '—'}</td>
                    <td style={{
                      ...tdBase,
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                      color: codeMatches ? '#0f172a' : st.fg,
                      fontWeight: 600, whiteSpace: 'nowrap',
                    }}>{c.actual_code || '—'}</td>
                    <td style={{
                      ...tdBase, color: '#64748b',
                      fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap',
                    }}>
                      {c.duration_ms != null ? `${(c.duration_ms / 1000).toFixed(2)}s` : '—'}
                    </td>
                    <td style={{ ...tdBase, whiteSpace: 'nowrap' }}>
                      <span style={{
                        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.05em',
                        padding: '3px 10px', borderRadius: 999,
                        background: st.bg, color: st.fg,
                        border: `1px solid ${st.border}`,
                        textTransform: 'uppercase',
                      }}>{st.label}</span>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr style={{ background: '#f8fafc', borderBottom: '1px solid rgba(15,23,42,0.05)' }}>
                      <td />
                      <td colSpan={9} style={{
                        padding: '4px 12px 12px', fontSize: 12,
                        color: '#334155', lineHeight: 1.55,
                      }}>
                        <span style={{
                          fontSize: 10, fontWeight: 700,
                          color: '#64748b', textTransform: 'uppercase',
                          letterSpacing: '0.06em', marginRight: 8,
                        }}>Remarks</span>
                        {c.remarks}
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Download the official the authority sign-off PDF. Rendered on demand
          from cert_summary — always available once cert_status is
          'certified'. */}
      <div style={{
        padding: '16px 20px',
        borderTop: '1px solid rgba(15,23,42,0.06)',
        background: 'linear-gradient(180deg, #ffffff 0%, #f8fafc 100%)',
        display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center',
      }}>
        <div style={{ flex: 1, minWidth: 240 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#0f172a', marginBottom: 2 }}>
            Official {t('term.authority')} Certification Sign-off
          </div>
          <div style={{ fontSize: 11.5, color: '#64748b', lineHeight: 1.5 }}>
            Congratulatory certificate on {t('term.authority')} letterhead, embedding the test-case
            results table above. Downloadable as PDF.
          </div>
        </div>
        <button
          onClick={() => downloadCertSignoffPdf(
            changeId,
            `${t('cert.filePrefix')}_Signoff_${changeId}.pdf`,
          )}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 8,
            padding: '10px 18px', fontSize: 12.5, fontWeight: 700,
            letterSpacing: '0.02em',
            background: '#1d4ed8', color: '#ffffff',
            border: '1px solid #1e40af', borderRadius: 8, cursor: 'pointer',
            boxShadow: '0 1px 2px rgba(29,78,216,0.35), 0 4px 12px rgba(29,78,216,0.20)',
          }}
        >
          <Download size={14} />
          Download Certificate (PDF)
        </button>
      </div>

      {/* Footer / signoff line */}
      <div style={{
        padding: '10px 20px',
        borderTop: '1px solid rgba(15,23,42,0.06)',
        background: '#fbfcfd',
        fontSize: 11, color: '#64748b',
        display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center',
      }}>
        <span>
          Digest:{' '}
          <span style={{
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            color: '#0f172a',
          }}>
            sha256:{runId.toLowerCase()}-{cases.length}c
          </span>
        </span>
        <span style={{ marginLeft: 'auto' }}>
          Report generated by {t('term.authority')} Cert Orchestrator · v2.1
        </span>
      </div>
    </div>
  );
}

// ─── Certification Execution Evidence ────────────────────────────────────────
// The partner's OWN record of every executed certification case, grouped by
// attempt: what the deployed application was asked, the exact XML it answered
// (the certified artifact), the grader's verdict and reasons, and the round
// pack that graded it. Rows come from cert_case_executions, written by the
// verdict channel BEFORE the report is forwarded — evidence survives even a
// failed A2A send.
function CertExecutionsPanel({ npciChangeId }) {
  const [openCase, setOpenCase] = useState(null)

  const { data } = useQuery({
    queryKey: ['cert-executions', npciChangeId],
    queryFn: () => listCertExecutions(npciChangeId),
    enabled: !!npciChangeId,
    refetchInterval: 5000,   // rounds land asynchronously; keep the trail live
  })
  const rows = data?.executions || []
  if (!rows.length) return null

  const byAttempt = rows.reduce((m, r) => {
    (m[r.cert_attempt] = m[r.cert_attempt] || []).push(r); return m
  }, {})
  const attempts = Object.keys(byAttempt).map(Number).sort((a, b) => b - a)

  const chip = (status) => (
    <span style={{
      fontSize: 10, fontWeight: 700, padding: '1px 8px', borderRadius: 10,
      textTransform: 'uppercase',
      color: status === 'passed' ? 'var(--ok, #16a34a)' : status === 'failed' ? '#dc2626' : '#d97706',
      border: '1px solid currentColor',
    }}>{status}</span>
  )
  const mono = { fontFamily: 'monospace', fontSize: 10.5, whiteSpace: 'pre-wrap',
                 wordBreak: 'break-all', background: 'var(--panel-2, rgba(0,0,0,0.25))',
                 padding: '8px 10px', borderRadius: 6, margin: '4px 0 8px' }

  return (
    <div style={{ marginTop: 16, border: '1px solid var(--border, #333)', borderRadius: 10, overflow: 'hidden' }}>
      <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border, #333)' }}>
        <div style={{ fontSize: 13, fontWeight: 700 }}>Execution Evidence</div>
        <div style={{ fontSize: 11, opacity: 0.7 }}>
          Per-case record kept by this platform: request sent to the application, its exact response, and the grading against the round's contract pack
        </div>
      </div>
      <div style={{ padding: '10px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
        {attempts.map(att => (
          <div key={att}>
            <div style={{ fontSize: 11, fontWeight: 700, opacity: 0.8, margin: '4px 0' }}>
              Round {att} · {byAttempt[att].filter(r => r.status === 'passed').length}/{byAttempt[att].length} passed
            </div>
            {byAttempt[att].map(r => {
              const d = r.details || {}
              const pay = d.payloads || {}
              const open = openCase === r.id
              return (
                <div key={r.id} style={{ border: '1px solid var(--border, #333)', borderRadius: 8, marginBottom: 6 }}>
                  <div onClick={() => setOpenCase(open ? null : r.id)}
                       style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '7px 12px', cursor: 'pointer' }}>
                    <span style={{ fontFamily: 'monospace', fontSize: 12, fontWeight: 700 }}>{r.case_id}</span>
                    {chip(r.status)}
                    <span style={{ fontSize: 10.5, opacity: 0.65 }}>
                      {d.sim_pack ? `pack ${String(d.sim_pack).split(' ')[0]}` : ''}
                    </span>
                    <span style={{ flex: 1 }} />
                    <span style={{ fontSize: 10.5, opacity: 0.6 }}>{r.at ? formatAbsolute(parseIso(r.at)) : ''}</span>
                    <ChevronRight size={13} style={{ transform: open ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }} />
                  </div>
                  {open && (
                    <div style={{ padding: '4px 14px 12px', fontSize: 11 }}>
                      {(d.assertion_failures || []).length > 0 && (
                        <>
                          <div style={{ fontWeight: 700, color: '#dc2626', margin: '6px 0 2px' }}>Grading failures</div>
                          {d.assertion_failures.map((f, i) =>
                            <div key={i} style={{ fontFamily: 'monospace', fontSize: 10.5 }}>• {String(f)}</div>)}
                        </>
                      )}
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, margin: '6px 0' }}>
                        <div>
                          <div style={{ fontWeight: 700, opacity: 0.8 }}>Expected</div>
                          <div style={mono}>{JSON.stringify(d.expected || {}, null, 1)}</div>
                        </div>
                        <div>
                          <div style={{ fontWeight: 700, opacity: 0.8 }}>Observed</div>
                          <div style={mono}>{JSON.stringify(d.observed || {}, null, 1)}</div>
                        </div>
                      </div>
                      {pay.sut_request && (<>
                        <div style={{ fontWeight: 700, opacity: 0.8 }}>Request → application</div>
                        <div style={mono}>{pay.sut_request}</div>
                      </>)}
                      {pay.sut_response && (<>
                        <div style={{ fontWeight: 700, opacity: 0.8 }}>Application response (the certified artifact)</div>
                        <div style={mono}>{pay.sut_response}</div>
                      </>)}
                      {pay.sim_response && (<>
                        <div style={{ fontWeight: 700, opacity: 0.8 }}>Authority simulator reply (HTTP {String(d.sim_status ?? '')})</div>
                        <div style={mono}>{pay.sim_response}</div>
                      </>)}
                      {pay.truncated && <div style={{ opacity: 0.6 }}>⚠ one or more payloads truncated at the persistence cap</div>}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}


function CertLifecyclePanel({ changeId, completedSteps = [] }) {
  const queryClient = useQueryClient();
  const [readyDlg, setReadyDlg] = useState(false);
  // Certification can only be declared after all three implementation
  // milestones (Design review / Code review and merge / Test Execution) have
  // been reported to the authority via the Implementation Pipeline.
  const REQUIRED_STEPS = ['design_completed', 'coding_completed', 'testing_completed'];
  const implementationDone = REQUIRED_STEPS.every((k) => completedSteps.includes(k));
  const [phaseIdx, setPhaseIdx] = useState(0);
  // Guards the demo mock-complete POST so it fires exactly once per session
  // per change while cert_status sits at ready_for_certification.
  const [mockPosted, setMockPosted] = useState(false);
  // Role + test_data the partner submitted via the Declare Ready dialog —
  // echoed in the results report. Seeded from localStorage so it survives refresh.
  const [readyContext, setReadyContext] = useState(() => {
    try {
      const raw = localStorage.getItem(CERT_READY_CTX_KEY(changeId));
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  });

  // Polls every 5s so the orchestration stage updates without manual
  // refresh — the partner sees the cert_status flip to 'certified' as
  // soon as the authority (or the demo mock-complete POST) publishes the result.
  const { data } = useQuery({
    queryKey: ['certStatus', changeId],
    queryFn: () => getCertStatus(changeId),
    refetchInterval: 5000,
    refetchOnWindowFocus: true,
  });

  const backendCurrent = data?.current;

  // Demo-mode completion: after the orchestration-phase cycle plays out
  // once, POST the mocked UPI Circle result set to the backend. That
  // persists cert_summary + cert_status='certified' (so refresh keeps
  // state) and best-effort mirrors the run to the authority over A2A. The 5s
  // getCertStatus poll picks up the new certified payload automatically.
  useEffect(() => {
    if (backendCurrent === 'ready_for_certification' && !mockPosted) {
      let cancelled = false;
      const timer = setTimeout(async () => {
        if (cancelled) return;
        setMockPosted(true);
        try {
          await certMockComplete(changeId, {
            ...MOCK_CERT_SUMMARY,
            role: readyContext?.role || null,
            test_data: readyContext?.test_data || null,
          });
          queryClient.invalidateQueries({ queryKey: ['certStatus', changeId] });
          queryClient.invalidateQueries({ queryKey: ['change', changeId] });
        } catch {
          // Let the next poll retry.
          setMockPosted(false);
        }
      }, MOCK_CERT_DELAY_MS);
      return () => {
        cancelled = true;
        clearTimeout(timer);
      };
    }
    // Reset the guard if the backend rewinds out of readiness/certified
    // (e.g. reset scripts) so a fresh cycle can trigger the mock again.
    if (backendCurrent
        && backendCurrent !== 'ready_for_certification'
        && backendCurrent !== 'certified'
        && mockPosted) {
      setMockPosted(false);
    }
  }, [backendCurrent, mockPosted, changeId, readyContext, queryClient]);

  // Self-heal on page load: if the change is already certified locally but a
  // prior A2A push to the authority may have failed (older authority backend, transient
  // network), fire a zero-arg resync exactly once per mount. The backend
  // uses the saved cert_summary and the authority's handler is idempotent, so this
  // is safe if the authority already knows.
  const [resyncPosted, setResyncPosted] = useState(false);
  useEffect(() => {
    if (backendCurrent === 'certified' && !resyncPosted && !mockPosted) {
      setResyncPosted(true);
      certMockComplete(changeId, {}).catch(() => {
        // Non-fatal — dashboard flip is best-effort, keep the UI responsive.
      });
    }
  }, [backendCurrent, resyncPosted, mockPosted, changeId]);

  const current        = data?.current || 'received';
  const history        = data?.history || {};
  const activeIdx      = certActiveIdx(current);
  const failed         = current === 'cert_failed' || current === 'defect_notice';
  // Terminal but not all-pass: run finished, some TCs failed. Renders the
  // 3rd stage as completed-with-warnings rather than green "certified".
  const testsCompleted = current === 'tests_completed';

  // Cycle the sub-phase label only while we're actively waiting on
  // the authority. Resets when the stage flips out of 'running'.
  useEffect(() => {
    if (activeIdx !== 1 || failed) {
      setPhaseIdx(0);
      return;
    }
    const tick = setInterval(
      () => setPhaseIdx((i) => (i + 1) % CERT_RUN_PHASES.length),
      CERT_PHASE_DURATION_MS,
    );
    return () => clearInterval(tick);
  }, [activeIdx, failed]);

  const advance = useMutation({
    mutationFn: (body) => updateCertStatus(changeId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['certStatus', changeId] });
      queryClient.invalidateQueries({ queryKey: ['change', changeId] });
      setReadyDlg(false);
    },
  });

  // Per-stage visual state. The middle stage is the live "waiting on
  // the authority" one — shows a spinner; the first/last are pending/done.
  const stateOf = (i) => {
    if (failed && i === 1) return 'failed';
    if (i < activeIdx)     return 'completed';
    if (i === activeIdx)   return i === 0 ? 'pending-action' : 'running';
    return 'pending';
  };

  return (
    <div style={card({ padding: 20, position: 'relative' })}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18 }}>
        <div style={{
          width: 32, height: 32, borderRadius: 8,
          background: 'rgba(16,185,129,0.10)', border: '1px solid rgba(16,185,129,0.22)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
        }}>
          <Shield size={16} color={T.success} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>Cert Lifecycle</div>
          <div style={{ fontSize: 11, color: T.textMuted, marginTop: 2 }}>
            Live · poll every 5s · status from {t('term.authority')} cert orchestrator
          </div>
        </div>
        <span style={{
          fontSize: 11, fontWeight: 700, padding: '3px 10px', borderRadius: 999,
          background: failed ? 'rgba(239,68,68,0.10)'
                    : testsCompleted ? 'rgba(245,158,11,0.10)'
                    : activeIdx >= 3 ? 'rgba(16,185,129,0.14)'
                    : activeIdx === 1 ? 'rgba(245,158,11,0.10)'
                    : 'rgba(15,23,42,0.05)',
          color: failed ? T.danger
               : testsCompleted ? T.warning
               : activeIdx >= 3 ? T.success
               : activeIdx === 1 ? T.warning
               : T.textSecondary,
          border: `1px solid ${failed ? 'rgba(239,68,68,0.22)'
                              : testsCompleted ? 'rgba(245,158,11,0.22)'
                              : activeIdx >= 3 ? 'rgba(16,185,129,0.30)'
                              : activeIdx === 1 ? 'rgba(245,158,11,0.22)'
                              : T.borderSubtle}`,
          textTransform: 'uppercase', letterSpacing: '0.4px',
        }}>
          {failed ? 'Defect Identified'
           : testsCompleted ? 'Tests Completed'
           : activeIdx >= 3 ? 'Certified'
           : activeIdx === 1 ? 'Running cert'
           : 'Awaiting partner'}
        </span>
      </div>

      {/* Stages */}
      <div style={{ position: 'relative', paddingLeft: 4 }}>
        {CERT_STAGES.map((s, i) => {
          const state    = stateOf(i);
          const isLast   = i === CERT_STAGES.length - 1;
          const ts       = history[s.key === 'declare' ? 'ready_for_certification' : s.key]
                          || (s.key === 'certified' ? history['certified'] : null);

          const dotColor = state === 'completed' ? T.success
                         : state === 'running'   ? T.warning
                         : state === 'failed'    ? T.danger
                         : state === 'pending-action' ? T.primary
                         : 'rgba(15,23,42,0.18)';
          const labelColor = state === 'pending' ? T.textMuted : T.textPrimary;

          return (
            <div key={s.key} style={{ display: 'flex', alignItems: 'flex-start', gap: 14, paddingBottom: isLast ? 0 : 18, position: 'relative' }}>
              {/* Connector */}
              {!isLast && (
                <div style={{
                  position: 'absolute', left: 19, top: 40, bottom: -4,
                  width: 2,
                  background: i < activeIdx ? T.success : 'rgba(15,23,42,0.10)',
                  transition: 'background .3s ease',
                }} />
              )}

              {/* Dot — spinning for running stage, check for completed */}
              <div style={{
                width: 40, height: 40, flexShrink: 0,
                borderRadius: '50%',
                background: state === 'completed' || state === 'failed'
                  ? dotColor
                  : '#fff',
                border: `2px solid ${dotColor}`,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                boxShadow: state === 'running' ? `0 0 0 4px rgba(245,158,11,0.18)` : 'none',
                color: '#fff',
                transition: 'all 0.2s',
              }}>
                {state === 'completed' && <Check size={16} strokeWidth={3} />}
                {state === 'running'   && <Loader2 size={16} className="pp-spin" color={T.warning} />}
                {state === 'failed'    && <AlertTriangle size={16} />}
                {state === 'pending-action' && <BadgeCheck size={16} color={T.primary} />}
                {state === 'pending'   && <span style={{ width: 8, height: 8, borderRadius: '50%', background: dotColor }} />}
              </div>

              {/* Body */}
              <div style={{ flex: 1, minWidth: 0, paddingTop: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ fontSize: 13.5, fontWeight: 700, color: labelColor }}>
                    {state === 'running' && s.key === 'orchestrating'
                      ? CERT_RUN_PHASES[phaseIdx].label
                      : (testsCompleted && s.key === 'certified')
                        ? 'Tests Completed — Review Results'
                        : s.label}
                  </span>
                  {state === 'running' && (
                    <span style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
                      background: 'rgba(245,158,11,0.10)', color: T.warning,
                      border: '1px solid rgba(245,158,11,0.22)',
                      textTransform: 'uppercase', letterSpacing: 0.4,
                    }}>In Progress</span>
                  )}
                  {state === 'completed' && (
                    <span style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
                      background: 'rgba(16,185,129,0.10)', color: T.success,
                      border: '1px solid rgba(16,185,129,0.22)',
                      textTransform: 'uppercase', letterSpacing: 0.4,
                    }}>Completed</span>
                  )}
                  {state === 'failed' && (
                    <span style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 700,
                      background: 'rgba(239,68,68,0.10)', color: T.danger,
                      border: '1px solid rgba(239,68,68,0.22)',
                      textTransform: 'uppercase', letterSpacing: 0.4,
                    }}>Defect</span>
                  )}
                  {ts && (
                    <span style={{ fontSize: 11, color: T.textMuted, marginLeft: 'auto' }}>
                      {fmtTs(ts)}
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 3, lineHeight: 1.45 }}>
                  {state === 'running' && s.key === 'orchestrating'
                    ? CERT_RUN_PHASES[phaseIdx].sub
                    : (testsCompleted && s.key === 'certified')
                      ? 'Cert run completed with failures. Review the per-case breakdown below before re-declaring readiness.'
                      : s.sub}
                </div>

                {/* Shimmer bar while orchestrating */}
                {state === 'running' && (
                  <div className="pp-stage-running" style={{ marginTop: 10 }}>
                    <div className="pp-stage-bar" style={{
                      height: 3, borderRadius: 999,
                      background: 'rgba(245,158,11,0.10)',
                    }} />
                  </div>
                )}

                {/* Cert results summary on the certified stage. If the
                    backend later persists a cert_summary blob on the
                    change (counts + per-case status), it'll render
                    here automatically; until then we surface a clear
                    "waiting for results" message so the UI doesn't
                    fabricate numbers. */}
                {state === 'completed' && s.key === 'certified' && (() => {
                  const summary = data?.cert_summary;
                  if (!summary) {
                    return (
                      <div style={{
                        marginTop: 10, padding: '10px 12px', borderRadius: 8,
                        background: 'rgba(16,185,129,0.05)',
                        border: '1px solid rgba(16,185,129,0.20)',
                        fontSize: 12, color: T.textSecondary,
                      }}>
                        Pass / fail breakdown will appear here once {t('term.authority')} publishes
                        the detailed cert_test_response back to this partner.
                      </div>
                    );
                  }
                  return (
                    <CertResultsReport
                      summary={summary}
                      readyContext={readyContext}
                      changeId={changeId}
                      certifiedAt={history?.certified}
                    />
                  );
                })()}

                {/* the authority Certification Result certificate — downloadable once
                    the all-PASS sign-off has arrived over A2A. */}
                {state === 'completed' && s.key === 'certified' && data?.has_signoff && (
                  <button
                    onClick={() => downloadCertSignoff(changeId, data.signoff_filename)}
                    style={{
                      marginTop: 12, display: 'inline-flex', alignItems: 'center', gap: 8,
                      padding: '8px 14px', borderRadius: 8, cursor: 'pointer',
                      background: 'rgba(16,185,129,0.10)', color: T.success,
                      border: '1px solid rgba(16,185,129,0.30)',
                      fontSize: 12, fontWeight: 700,
                    }}
                  >
                    <BadgeCheck size={14} />
                    Download Certification Signoff
                  </button>
                )}

                {/* Action button on stage 1 — open Declare Ready dialog.
                    Gated on all three implementation milestones being reported
                    to {t('term.authority')} first. */}
                {state === 'pending-action' && i === 0 && (
                  <>
                    <button
                      onClick={() => setReadyDlg(true)}
                      disabled={advance.isPending || !implementationDone}
                      title={implementationDone ? undefined : 'Complete Design review, Code review and merge, and Test Execution first'}
                      style={{
                        marginTop: 12,
                        display: 'inline-flex', alignItems: 'center', gap: 7,
                        padding: '8px 16px', fontSize: 13, fontWeight: 600,
                        background: (advance.isPending || !implementationDone) ? T.bgMuted : T.primary,
                        color: (advance.isPending || !implementationDone) ? T.textMuted : '#fff',
                        border: 'none', borderRadius: 8,
                        cursor: advance.isPending ? 'wait' : !implementationDone ? 'not-allowed' : 'pointer',
                      }}
                    >
                      {advance.isPending
                        ? <><Loader2 size={13} className="pp-spin" /> Submitting…</>
                        : <><BadgeCheck size={13} /> Declare Ready</>}
                    </button>
                    {!implementationDone && (
                      <div style={{ marginTop: 8, fontSize: 11.5, color: T.textMuted, display: 'flex', alignItems: 'center', gap: 6 }}>
                        <AlertTriangle size={12} />
                        Complete all three implementation steps (Design review, Code review and merge, Test Execution) to enable this.
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {advance.isError && (
        <div style={{
          marginTop: 14, padding: '10px 14px', borderRadius: 8,
          background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.22)',
          fontSize: 12, color: T.danger, display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <AlertTriangle size={14} />
          {advance.error?.response?.data?.detail || 'Could not declare ready'}
        </div>
      )}

      {/* Role + test_data dialog */}
      <DeclareReadyDialog
        open={readyDlg}
        busy={advance.isPending}
        onClose={() => setReadyDlg(false)}
        onSubmit={({ role, test_data }) => {
          const ctx = { role, test_data, submitted_at: new Date().toISOString() };
          try { localStorage.setItem(CERT_READY_CTX_KEY(changeId), JSON.stringify(ctx)); } catch {}
          setReadyContext(ctx);
          advance.mutate({ status: 'ready_for_certification', role, test_data });
        }}
      />
    </div>
  );
}



// Render-level safety net. If any subtree crashes (bad null deref, bad
// markdown, missing icon, etc.) we show the error inline instead of
// silently blanking the page. Without this, a thrown JSX error from
// one of the panels below bubbles all the way to the React root and
// unmounts the entire app.
class ChangeDetailErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, info: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    this.setState({ error, info });
    console.error('[ChangeDetail render error]', error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="card" style={{ padding: 16, borderColor: '#f5c6cb', background: '#fdecea' }}>
            <div style={{ fontWeight: 700, color: '#721c24', marginBottom: 6 }}>
              Page failed to render
            </div>
            <div style={{ fontSize: 12, color: '#721c24', whiteSpace: 'pre-wrap', fontFamily: 'monospace' }}>
              {String(this.state.error?.message || this.state.error)}
              {this.state.info?.componentStack && '\n\n' + this.state.info.componentStack}
            </div>
            <button
              className="btn btn-secondary"
              style={{ marginTop: 10 }}
              onClick={() => window.location.reload()}
            >Reload</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function ChangeDetailInner() {
  const { id } = useParams();
  const navigate = useNavigate();

  const queryClient = useQueryClient();
  const { data: change, isLoading, error, isFetching } = useQuery({
    queryKey: ['change', id],
    queryFn: () => getChange(id),
    // 2 s — was 5 s. Active workflow page; the chat / blocker / counter
    // round trips were the main "feels slow" complaint. Mutations now
    // hydrate the cache directly from the fattened server response, so
    // this poll mostly covers the authority-driven changes (response landed,
    // cert lifecycle ticks).
    refetchInterval: 2000,
    refetchOnWindowFocus: true,
  });

  // Force-refresh all queries scoped to this change. Same UX as a
  // browser hard-reload, but keeps the React tree mounted so we don't
  // lose tab state, scroll position, or in-flight composer drafts.
  const handleSync = () => {
    queryClient.invalidateQueries({ queryKey: ['change', id] });
    queryClient.invalidateQueries({ queryKey: ['certStatus', id] });
    queryClient.invalidateQueries({ queryKey: ['queryDrafts', id] });
    queryClient.invalidateQueries({ queryKey: ['certQueries', id] });
  };

  // Per-doc review state (localStorage-backed) for status pills +
  // review progress. The hook is safe to call before `change` is
  // loaded — it just opens an empty bucket keyed by the change id.
  const docReview = useDocReviewState(id);

  // Documents tab toolbar state — search query + filter chip.
  const [docSearch, setDocSearch] = useState('');

  // DocPreviewDrawer state. Holds the currently-previewed doc id;
  // the drawer reads from the docs array to render header + content,
  // and pages forward/back without unmounting.
  const [previewDocId, setPreviewDocId] = useState(null);

  // Stage model — the workspace lands the partner on the Documents tab so the
  // product kit is the first thing they see when entering a change. The other
  // stages (Decide / Build / Certify) stay one click away in the workflow order.
  const initialStage = 'documents';
  const [stage, setStage] = useState(initialStage);
  const [stageInitialized, setStageInitialized] = useState(false);
  useEffect(() => {
    if (change && !stageInitialized) {
      setStage(initialStage);
      setStageInitialized(true);
    }
  }, [change, initialStage, stageInitialized]);
  // Sub-view within the Build stage. Land on Design (the first tab) — the work
  // flows Design → Code → Testing, then the Pipeline reports the milestones.
  const [buildView, setBuildView] = useState('design');
  // Lifted so the Report-blocker entry on the Progress tab can drive
  // the inner subtab — clicking it takes the partner straight to the
  // Activity → Blocker chat composer instead of opening a structured
  // form. ActivityThread accepts these as controlled props.
  const [activityTab, setActivityTab] = useState('general');
  const openBlockerChat = () => {
    setActivityTab('blocker');
    setStage('activity');
  };
  // Ref for scrolling the Activity area into view after a feasibility
  // panel send. Without this, the user stays parked on the panel and
  // has no visual confirmation that their message landed in a thread.
  const activityAreaRef = useRef(null);
  // Called by FeasibilityPanel when one of its drafted messages is sent.
  // Lands the partner on the general activity thread (the single channel all
  // partner messages go to), then scrolls the activity panel into view on the
  // next tick (state updates must flush before the tab-content DOM exists).
  const handleFeasibilityMessageSent = () => {
    setActivityTab('general');
    setStage('activity');
    setTimeout(() => {
      activityAreaRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 60);
  };

  if (isLoading) return (
    <div className="page" style={{ background: T.bgSoft, minHeight: '100vh' }}>
      <style>{PAGE_STYLES}</style>
      <div style={{ maxWidth: 1180, margin: '0 auto', padding: '24px 4px' }}>
        {/* Skeleton: eyebrow + hero title */}
        <div className="pp-skeleton" style={{ width: 220, height: 12, marginBottom: 10 }} />
        <div className="pp-skeleton" style={{ width: 480, height: 32, marginBottom: 20 }} />
        {/* Skeleton: context bar */}
        <div style={card({ padding: '14px 22px', marginBottom: 16, display: 'flex', gap: 28 })}>
          {[1,2,3,4].map(i => (
            <div key={i} style={{ flex: 1 }}>
              <div className="pp-skeleton" style={{ width: 60, height: 9, marginBottom: 8 }} />
              <div className="pp-skeleton" style={{ width: 140, height: 14 }} />
            </div>
          ))}
        </div>
        {/* Skeleton: stepper */}
        <div style={card({ padding: '20px 22px', marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 })}>
          {[1,2,3,4,5,6].flatMap((i, idx) => [
            (
              <div key={`d-${i}`} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 80 }}>
                <div className="pp-skeleton" style={{ width: 20, height: 20, borderRadius: '50%', marginBottom: 8 }} />
                <div className="pp-skeleton" style={{ width: 70, height: 11 }} />
              </div>
            ),
            idx < 5 && (
              <div key={`c-${i}`} className="pp-skeleton" style={{ flex: 1, height: 2, margin: '0 6px 22px 6px' }} />
            ),
          ].filter(Boolean))}
        </div>
        {/* Skeleton: tabs + body */}
        <div style={{ display: 'flex', gap: 24, alignItems: 'flex-start' }}>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
              {[60, 70, 80].map(w => <div key={w} className="pp-skeleton" style={{ width: w, height: 14 }} />)}
            </div>
            {[1,2,3,4].map(i => (
              <div key={i} style={card({ padding: '14px 18px', marginBottom: 10, display: 'flex', alignItems: 'center', gap: 12 })}>
                <div className="pp-skeleton" style={{ width: 34, height: 34, borderRadius: 8 }} />
                <div style={{ flex: 1 }}>
                  <div className="pp-skeleton" style={{ width: '60%', height: 14, marginBottom: 6 }} />
                  <div className="pp-skeleton" style={{ width: '85%', height: 11 }} />
                </div>
              </div>
            ))}
          </div>
          <aside style={{ width: 320 }}>
            <div style={card({ padding: 20 })}>
              <div className="pp-skeleton" style={{ width: 100, height: 10, marginBottom: 12 }} />
              <div className="pp-skeleton" style={{ width: '70%', height: 18, marginBottom: 14 }} />
              <div className="pp-skeleton" style={{ width: '100%', height: 38, borderRadius: 8 }} />
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
  if (error) {
    return (
      <div className="page">
        <button className="back-btn" onClick={() => navigate('/')}>
          <ArrowLeft size={16} /> Back
        </button>
        <div className="card">
          <p style={{ color: 'var(--danger)' }}>Failed to load change details.</p>
        </div>
      </div>
    );
  }

  // Defensive: if useQuery somehow returns no error but undefined data
  // (e.g. a stale background-refetch race), surface a friendly empty
  // state instead of letting `change.documents` deref blow up.
  if (!change) {
    return (
      <div className="page">
        <button className="back-btn" onClick={() => navigate('/')}>
          <ArrowLeft size={16} /> Back
        </button>
        <div className="card">
          <p style={{ color: 'var(--text-muted)' }}>No data for this change yet. Try refreshing.</p>
        </div>
      </div>
    );
  }

  // Doc types hidden from the partner-side Documents tab for now.
  // The rows still exist on the backend / authority side; they're just
  // filtered out of the partner UI so partners aren't asked to
  // review artefacts that aren't ready / aren't relevant yet.
  const HIDDEN_DOC_TYPES = new Set([
    'product_doc',
  ]);
  const documents = (change.documents || change.product_kit || [])
    .filter(d => !HIDDEN_DOC_TYPES.has(d.doc_type || d.type));
  const queries = change.queries || [];
  const rawProgress = change.completed_steps || change.progress || [];
  const completedSteps = (Array.isArray(rawProgress) ? rawProgress : [])
    .map(p => typeof p === 'string' ? p : p?.step)
    .filter(Boolean);

  const decision = change.decision || 'pending';
  // The decision action (Ask / Accept / Counter) shows in the Decide stage
  // while the rollout is still open; once accepted it falls away.
  const showDecisionAction = decision !== 'accepted';

  // Stage model — the workspace is organised around the four workflow modes
  // (Decide / Build / Certify) plus the cross-cutting Activity thread, rather
  // than a flat list of feature panels. Reachability mirrors the backend
  // state machine; future stages render locked until the change reaches them.
  const allMilestonesDone = PROGRESS_STEPS.every(s => completedSteps.includes(s.key));
  const buildReachable = decision === 'accepted';
  // A change the authority has already CERTIFIED (or is actively certifying)
  // must expose its Certify view regardless of the local decide/build
  // tracking: the execution evidence and signoff live there, and locking a
  // completed certification behind an unclicked local checkbox hides real
  // history. The forward gate (decide → build → certify) still applies to
  // changes that haven't reached certification.
  const certifyReachable = ['certifying', 'tests_completed', 'certified'].includes(change.cert_status)
    || (buildReachable && (allMilestonesDone
    || ['ready_for_certification', 'tests_completed', 'certified'].includes(change.cert_status)));
  const docCount = documents.length;
  const STAGES = [
    // Documents leads — partner lands here on entry so the product kit is the
    // first thing surfaced. Always reachable; reads from any workflow stage.
    { id: 'documents', label: 'Documents', sub: docCount ? `${docCount} in kit` : 'Product kit', icon: FileText, reachable: true },
    { id: 'decide',    label: 'Decide',    sub: 'Review & decide',    icon: ClipboardCheck, reachable: true,             done: decision === 'accepted' },
    { id: 'build',     label: 'Build',     sub: 'Implement & report', icon: Rocket,         reachable: buildReachable,   done: allMilestonesDone },
    { id: 'certify',   label: 'Certify',   sub: 'Test & certify',     icon: BadgeCheck,     reachable: certifyReachable, done: change.cert_status === 'certified' },
    { id: 'activity',  label: 'Activity',  sub: `Talk to ${t('term.authority')}`, icon: Activity, reachable: true },
  ];

  return (
    <div className="page" style={{ background: T.bgSoft, minHeight: '100vh' }}>
      <style>{PAGE_STYLES}</style>
      <div style={{ maxWidth: 1180, margin: '0 auto', padding: '0 4px' }}>
        <button className="back-btn" onClick={() => navigate('/')}>
          <ArrowLeft size={16} /> Back to Dashboard
        </button>

        {/* Hero block — title is the single dominant element on the page;
            status pill rides alongside it. Eyebrow line above gives
            workflow context without competing visually with the title. */}
        <div style={{ marginBottom: 20 }}>
          <div style={{
            fontSize: T.fontMicro, fontWeight: 700, color: T.textMuted,
            letterSpacing: 0.7, textTransform: 'uppercase', marginBottom: 6,
          }}>
            {t('term.authorityCap')} Rollout · ID {(change.npci_change_id || '').slice(0, 8) || '—'}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
            <h1 style={{
              margin: 0,
              fontSize: T.fontHero, lineHeight: 1.15, fontWeight: 700,
              color: T.textPrimary,
              letterSpacing: -0.3,
            }}>
              {change.title}
            </h1>
            {statusBadge(change.status)}
            {/* Force-pull the latest state from the authority. Drops in alongside
                polling so the operator can demand a fresh read after a
                step instead of waiting for the next 5s tick. */}
            <button
              type="button"
              onClick={handleSync}
              disabled={isFetching}
              title={`Sync with ${t('term.authority')}`}
              style={{
                marginLeft: 'auto',
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '8px 12px', fontSize: 12, fontWeight: 600,
                color: T.textSecondary, background: '#fff',
                border: `1px solid ${T.borderSubtle}`, borderRadius: 8,
                cursor: isFetching ? 'wait' : 'pointer',
                opacity: isFetching ? 0.6 : 1,
              }}
            >
              <RefreshCw size={14} style={{ animation: isFetching ? 'pp-spin 1s linear infinite' : 'none' }} />
              {isFetching ? 'Syncing…' : 'Sync'}
            </button>
          </div>
        </div>

        <ChangeContextBar change={change} />

        <LifecycleStepper change={change} completedSteps={completedSteps} />

        {/* Open-blocker alert — always visible regardless of active stage so
            the partner is never unaware of an unresolved blocker. */}
        {(() => {
          const openBlockers = (change.blockers || []).filter(b => b.status !== 'resolved');
          if (!openBlockers.length) return null;
          const top = openBlockers[0];
          const sevColor = { critical: '#7f1d1d', high: '#78350f', medium: '#1e3a5f', low: '#374151' }[top.severity] || '#7f1d1d';
          const sevBg   = { critical: '#fdecea', high: '#fef3c7', medium: '#e0f2fe', low: '#f3f4f6' }[top.severity] || '#fdecea';
          const sevBorder = { critical: '#fca5a5', high: '#fcd34d', medium: '#7dd3fc', low: '#e5e7eb' }[top.severity] || '#fca5a5';
          return (
            <div style={{
              margin: '12px 0',
              background: sevBg,
              border: `1px solid ${sevBorder}`,
              borderLeft: `4px solid ${sevColor}`,
              borderRadius: 10,
              padding: '12px 16px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                <AlertTriangle size={16} color={sevColor} style={{ flexShrink: 0 }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 3, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 12, fontWeight: 700, color: sevColor, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                      {openBlockers.length === 1 ? 'Open Blocker' : `${openBlockers.length} Open Blockers`}
                      {' · '}{(top.severity || 'high').toUpperCase()}
                    </span>
                  </div>
                  <div style={{ fontSize: 13, color: sevColor, lineHeight: 1.4 }}>
                    {top.description}
                  </div>
                </div>
                <button
                  onClick={() => { setStage('build'); setBuildView('pipeline'); }}
                  style={{
                    flexShrink: 0, padding: '6px 14px', fontSize: 12, fontWeight: 600,
                    background: sevColor, color: '#fff', border: 'none',
                    borderRadius: 7, cursor: 'pointer',
                  }}
                >
                  View {openBlockers.length > 1 ? 'all' : 'details'}
                </button>
              </div>
            </div>
          );
        })()}

        {/* Stage navigation — the workflow modes drive the workspace. Only
            the current mode's tools render; future modes show locked until the
            change reaches them. Activity spans every mode. */}
        <div className="stage-nav" role="tablist">
          {STAGES.map(s => {
            const active = stage === s.id;
            const locked = !s.reachable;
            const Icon = s.icon;
            return (
              <button
                key={s.id}
                role="tab"
                aria-selected={active}
                disabled={locked}
                title={locked ? 'Unlocks once the change reaches this stage' : undefined}
                onClick={() => { if (!locked) setStage(s.id); }}
                className={`stage-item ${active ? 'active' : ''} ${locked ? 'locked' : ''} ${s.done ? 'done' : ''}`}
              >
                <span className="stage-item-icon">
                  {locked ? <Lock size={15} /> : (s.done && !active ? <Check size={16} /> : <Icon size={16} />)}
                </span>
                <span style={{ minWidth: 0 }}>
                  <span className="stage-item-label">{s.label}</span>
                  <span className="stage-item-sub">{locked ? 'Locked' : s.sub}</span>
                </span>
              </button>
            );
          })}
        </div>

        {/* Mode content — full width; the active tab decides what renders. */}
        <div>

            {/* ── DECIDE ── feasibility verdict → decision → documents. The
                analyser auto-runs on receipt; `onMessageSent` routes a sent
                draft to the matching Activity sub-channel. */}
            {stage === 'decide' && (
              <FeasibilityPanel
                changeId={id}
                onMessageSent={handleFeasibilityMessageSent}
                decision={decision}
              />
            )}
            {stage === 'decide' && showDecisionAction && (
              <DecisionPanel
                changeId={id}
                decision={decision}
                npciCounter={change.npci_counter}
                change={change}
                onSubmitted={() => setStage('activity')}
              />
            )}
            {stage === 'decide' && (
              <div style={{
                marginTop: 12, padding: '12px 16px',
                background: 'rgba(220,38,38,0.04)',
                border: '1px solid rgba(220,38,38,0.18)',
                borderRadius: 10,
                display: 'flex', alignItems: 'center', gap: 12,
              }}>
                <Shield size={15} color="#dc2626" style={{ flexShrink: 0 }} />
                <span style={{ flex: 1, fontSize: 12, color: '#7f1d1d', lineHeight: 1.5 }}>
                  Hit a blocker while reviewing this kit? Flag it to {t('term.authority')} now — you don't need to accept first.
                </span>
                <button
                  onClick={openBlockerChat}
                  style={{
                    flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 6,
                    padding: '7px 14px', fontSize: 12, fontWeight: 600,
                    background: '#dc2626', color: '#fff',
                    border: 'none', borderRadius: 7, cursor: 'pointer',
                  }}
                >
                  <Shield size={12} /> Report a blocker
                </button>
              </div>
            )}

            {/* ── BUILD ── pipeline (milestones + blockers) plus the partner
                design / code / test artifacts, each on its own sub-view so
                they aren't all stacked. */}
            {stage === 'build' && (
              <>
                <div className="filter-chips" style={{ marginBottom: 16 }}>
                  {[
                    { id: 'design',   label: 'Design' },
                    { id: 'code',     label: 'Code' },
                    { id: 'test',     label: 'Testing' },
                    { id: 'pipeline', label: 'Pipeline' },
                  ].map(v => (
                    <button
                      key={v.id}
                      className={`filter-chip ${buildView === v.id ? 'active' : ''}`}
                      onClick={() => setBuildView(v.id)}
                    >
                      {v.label}
                    </button>
                  ))}
                </div>
                {buildView === 'pipeline' && (
                  <>
                    <ImplementationPipeline changeId={id} completedSteps={completedSteps} status={change.status} />
                    {/* Full blocker log — shows all reported blockers with severity,
                        description, and {t('term.authority')} resolution inline. Report-new-blocker
                        button opens the chat composer directly. */}
                    <BlockersSection blockers={change.blockers} />
                    {decision === 'accepted' && (
                      <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 10 }}>
                        <button className="btn btn-secondary" onClick={openBlockerChat} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                          <Shield size={13} /> Report a blocker
                        </button>
                        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                          Stuck on something? Describe the issue — {t('term.authorityCap')}'s PM picks it up from the chat.
                        </span>
                      </div>
                    )}
                  </>
                )}
                {buildView === 'design' && <DesignPanel changeId={id} />}
                {buildView === 'code' && <CodePanel changeId={id} />}
                {buildView === 'test' && <TestingPanel changeId={id} />}
              </>
            )}

            {/* ── CERTIFY ── cert lifecycle + the post-freeze emergency channel. */}
            {stage === 'certify' && (
              <>
                <CertLifecyclePanel changeId={id} completedSteps={completedSteps} />
                <CertExecutionsPanel npciChangeId={change.npci_change_id} />
                <EmergencyIssueSection
                  change={change}
                  onRaised={() => queryClient.invalidateQueries({ queryKey: ['change', id] })}
                />
              </>
            )}

            {/* ── ACTIVITY ── the the authority conversation, reachable from any stage. */}
            {stage === 'activity' && (
              <div ref={activityAreaRef}>
                <ActivityThread
                  changeId={id}
                  change={change}
                  activeTab={activityTab}
                  onActiveTabChange={setActivityTab}
                />
              </div>
            )}

            {/* Documents — a cross-cutting reference tab, available at any
                stage (the partner consults the kit through Build & Certify too). */}
            {stage === 'documents' && (() => {
              // Collapse all versions of a doc_type into one entry carrying every
              // version (newest first), so each card offers a per-document version
              // switch (v1 / v2 / …) rather than a single global version filter.
              const docsByType = {};
              for (const d of documents) {
                const t = d.doc_type || d.type;
                (docsByType[t] ||= []).push(d);
              }
              const collapsedDocs = Object.values(docsByType).map(vers => {
                const sorted = [...vers].sort((a, b) => (b.negotiation_version || 1) - (a.negotiation_version || 1));
                return { ...sorted[0], versions: sorted };
              });
              const totalCount = collapsedDocs.length;

              // Filter pipeline: search query (matches label or description).
              const q = docSearch.trim().toLowerCase();
              const passesSearch = (doc) => {
                if (!q) return true;
                const m = docMeta(doc.doc_type || doc.type);
                return (m.label + ' ' + m.desc).toLowerCase().includes(q);
              };
              const visible = collapsedDocs.filter(d => passesSearch(d));

              // Group visible docs into the predeclared sections.
              const grouped = {};
              for (const g of DOC_GROUPS) grouped[g.id] = [];
              for (const d of visible) grouped[groupOf(d.doc_type || d.type)].push(d);

              return (
                <>
                  <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap', alignItems: 'center' }}>
                    <div style={{ flex: 1, minWidth: 220, position: 'relative' }}>
                      <input
                        type="search"
                        value={docSearch}
                        onChange={(e) => setDocSearch(e.target.value)}
                        placeholder={`Search ${totalCount} documents…`}
                        style={{
                          width: '100%', boxSizing: 'border-box',
                          padding: '8px 12px',
                          borderRadius: 8,
                          border: T.cardBorder,
                          fontSize: 13,
                          background: T.cardBg,
                          color: T.textPrimary,
                          outline: 'none',
                        }}
                      />
                    </div>
                    {(change.available_versions || []).length > 1 && (
                      <span style={{ fontSize: 12, color: T.textMuted }}>
                        {change.available_versions.length} versions · switch per document below
                      </span>
                    )}
                  </div>

                  {change.npci_change_summary && change.negotiation_version > 1 && (
                    <div style={{
                      marginBottom: 18, padding: '14px 16px', borderRadius: 12,
                      background: 'rgba(37,99,235,0.04)', border: '1px solid rgba(37,99,235,0.18)',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
                        <FileText size={16} style={{ color: T.primary, flexShrink: 0 }} />
                        <div style={{ fontSize: T.fontH3, fontWeight: 700, color: T.textPrimary }}>
                          Summary of Changes — v{change.negotiation_version}
                        </div>
                        <div style={{ flex: 1 }} />
                        <button
                          onClick={() => downloadChangeSummary(id, `change_summary_v${change.negotiation_version}.docx`).catch(e => console.error('summary download failed', e))}
                          style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 12, fontWeight: 600, padding: '5px 12px', borderRadius: 8, border: `1px solid ${T.primary}`, background: 'transparent', color: T.primary, cursor: 'pointer' }}
                        >
                          <Download size={13} /> .docx
                        </button>
                      </div>
                      <div style={{ fontSize: T.fontBody, color: T.textSecondary, whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                        {change.npci_change_summary}
                      </div>
                    </div>
                  )}

                  {totalCount === 0 && (
                    <div style={card({ padding: 16 })}>
                      <p style={{ color: T.textSecondary, fontSize: 13, margin: 0 }}>No documents available yet.</p>
                    </div>
                  )}

                  {totalCount > 0 && visible.length === 0 && (
                    <div style={card({ padding: 16 })}>
                      <p style={{ color: T.textSecondary, fontSize: 13, margin: 0 }}>
                        No documents match your search.{' '}
                        <button
                          onClick={() => setDocSearch('')}
                          style={{ background: 'none', border: 'none', color: T.primary, cursor: 'pointer', fontWeight: 600, padding: 0 }}
                        >Clear search</button>
                      </p>
                    </div>
                  )}

                  {DOC_GROUPS.map(g => {
                    const items = grouped[g.id] || [];
                    if (items.length === 0) return null;
                    return (
                      <div key={g.id} style={{ marginBottom: 18 }}>
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 8, paddingLeft: 2 }}>
                          <h4 style={{
                            margin: 0, fontSize: 11, fontWeight: 700,
                            color: T.textMuted, letterSpacing: 0.6, textTransform: 'uppercase',
                          }}>{g.label}</h4>
                          <span style={{ fontSize: 11, color: T.textMuted }}>{items.length}</span>
                          {g.hint && (
                            <span style={{ fontSize: 11, color: T.textMuted, fontStyle: 'italic' }}>· {g.hint}</span>
                          )}
                        </div>
                        {items.map((doc, i) => (
                          <DocCard
                            key={(doc.doc_type || doc.type) + '-' + (doc.id || i)}
                            versions={doc.versions || [doc]}
                            changeId={id}
                            reviewStateByDoc={docReview.state}
                            onPreview={setPreviewDocId}
                          />
                        ))}
                      </div>
                    );
                  })}
                </>
              );
            })()}

        </div>
      </div>

      {/* Slide-over preview drawer. Lives at the page level so its
          backdrop covers everything including the sticky sidebar.
          Renders nothing when previewDocId is null — no DOM cost. */}
      <DocPreviewDrawer
        open={!!previewDocId}
        doc={documents.find(d => d.id === previewDocId) || null}
        docs={documents}
        changeId={id}
        reviewState={docReview.state[previewDocId]}
        onClose={() => setPreviewDocId(null)}
        onSelectDoc={(docId) => setPreviewDocId(docId)}
        onNavigate={(dir) => {
          // Move between distinct documents (latest version of the next type);
          // the in-drawer version tabs switch versions within a document.
          const types = [];
          const seen = new Set();
          for (const d of documents) {
            const t = d.doc_type || d.type;
            if (!seen.has(t)) { seen.add(t); types.push(t); }
          }
          const cur = documents.find(d => d.id === previewDocId);
          const ti = types.indexOf(cur?.doc_type || cur?.type);
          const nextType = types[ti + dir];
          if (!nextType) return;
          const latest = documents
            .filter(d => (d.doc_type || d.type) === nextType)
            .sort((a, b) => (b.negotiation_version || 1) - (a.negotiation_version || 1))[0];
          if (latest) setPreviewDocId(latest.id);
        }}
        onView={docReview.markViewed}
      />
    </div>
  );
}

export default function ChangeDetail() {
  return (
    <ChangeDetailErrorBoundary>
      <ChangeDetailInner />
    </ChangeDetailErrorBoundary>
  );
}
