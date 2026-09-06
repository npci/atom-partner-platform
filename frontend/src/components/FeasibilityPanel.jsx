// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// FeasibilityPanel — renders the analyser's per-area assessment for an
// incoming change. Sits above the tab bar on ChangeDetail so the
// partner ops user sees the verdict before drilling into documents.
//
// Behaviour:
//   - On mount, fetches the latest persisted report (auto-run from
//     change_communication usually populates v1 within seconds).
//   - 404 → renders the empty-state CTA ("Run feasibility analysis").
//   - On success → header pill + 6 area cards + next steps + footer meta.
//   - "Re-run" button POSTs to /analyse/{id} and refreshes.

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { t } from '../strings'
import {
  AlertTriangle, BadgeCheck, Brain, CheckSquare, ChevronDown, ChevronRight, ChevronUp,
  Clock, Copy, Loader2, MessageSquare, RefreshCw, Send,
} from 'lucide-react';
import { useState } from 'react';

import {
  getFeasibilityReport,
  runFeasibilityAnalysis,
  submitQuery,
} from '../services/api';
import Button from './ui/Button';
import { formatDateTime } from '../lib/datetime';

// ── Theme tokens (kept local to avoid coupling to ChangeDetail's T object) ──

const T = {
  cardBg:        '#ffffff',
  cardBorder:    '1px solid rgba(15, 23, 42, 0.06)',
  cardRadius:    16,
  cardShadow:    '0 1px 2px rgba(15, 23, 42, 0.04), 0 2px 6px rgba(15, 23, 42, 0.04)',
  textPrimary:   '#0f172a',
  textSecondary: '#475569',
  textMuted:     '#94a3b8',
  primary:       '#2563eb',
  success:       '#10b981',
  warning:       '#f59e0b',
  danger:        '#ef4444',
  bgSoft:        '#f8fafc',
  borderSubtle:  'rgba(15, 23, 42, 0.06)',
};

// ── Area metadata — label + ordering. Must match AREAS in the backend service. ──

const AREA_META = {
  production_deadline:   { label: 'Production deadline',   order: 1 },
  scope:                 { label: 'Scope',                 order: 2 },
  limits:                { label: 'Limits & thresholds',   order: 3 },
  technical_spec:        { label: 'Technical spec',        order: 4 },
  upstream_dependencies: { label: 'Upstream dependencies', order: 5 },
  certification_role:    { label: 'Certification role',    order: 6 },
};

const STATUS_COLOURS = {
  fits:         { bg: 'rgba(16,185,129,0.10)', fg: T.success, border: 'rgba(16,185,129,0.22)', label: 'Fits' },
  partial:      { bg: 'rgba(245,158,11,0.10)', fg: T.warning, border: 'rgba(245,158,11,0.22)', label: 'Partial' },
  gap:          { bg: 'rgba(239,68,68,0.10)',  fg: T.danger,  border: 'rgba(239,68,68,0.22)',  label: 'Gap' },
  out_of_scope: { bg: 'rgba(15,23,42,0.06)',   fg: T.textMuted, border: T.borderSubtle,        label: 'Out of scope' },
  unknown:      { bg: 'rgba(37,99,235,0.08)',  fg: T.primary, border: 'rgba(37,99,235,0.22)',  label: 'Unknown' },
};

const POSTURE_COLOURS = {
  ready:                   { bg: 'rgba(16,185,129,0.10)', fg: T.success, border: 'rgba(16,185,129,0.22)', label: 'Ready' },
  ready_with_conditions:   { bg: 'rgba(245,158,11,0.10)', fg: T.warning, border: 'rgba(245,158,11,0.22)', label: 'Ready with conditions' },
  needs_negotiation:       { bg: 'rgba(239,68,68,0.10)',  fg: T.danger,  border: 'rgba(239,68,68,0.22)',  label: 'Needs negotiation' },
  out_of_scope:            { bg: 'rgba(15,23,42,0.06)',   fg: T.textMuted, border: T.borderSubtle,        label: 'Out of scope' },
};

// Single neutral tone for every drafted authority message. The partner no longer
// pre-classifies messages as query vs counter — the authority handles them all through
// one channel — so there is one look and one send path (/query).
const MSG_TONE = { bg: 'rgba(37,99,235,0.10)', fg: T.primary, border: 'rgba(37,99,235,0.22)', label: `To ${t('term.authority')}` };

// ── "Already sent" persistence ──────────────────────────────────────────────
//
// `sendState` lives in React component state, so a hard refresh resets the
// "Sent" badge even though the backend has already accepted the message.
// We persist a fingerprint per sent message in localStorage and seed the
// state from it on mount. Fingerprint includes change_id + subject + body
// length + first 40 chars of body so a fresh analyser re-run that produces
// a DIFFERENT message with the SAME subject doesn't get mis-marked as sent.
//
// localStorage is per-browser; cross-device "already sent" needs a server-
// side cross-reference (subject/body match against outgoing_queries /
// counter_proposals for this change) — defer until you actually hit it.

const LS_SENT_KEY = 'pp_feasibility_sent_v1';

function _readSentMap() {
  try {
    const raw = localStorage.getItem(LS_SENT_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (_e) {
    return {};
  }
}

function _writeSentMap(m) {
  try { localStorage.setItem(LS_SENT_KEY, JSON.stringify(m)); }
  catch (_e) { /* quota / disabled — silent */ }
}

function _fingerprint(changeId, msg) {
  const body = msg?.draft_message || '';
  return `${changeId}::${msg?.subject || ''}::${body.length}::${body.slice(0, 40)}`;
}

function isAlreadySent(changeId, msg) {
  if (!changeId || !msg) return false;
  return Boolean(_readSentMap()[_fingerprint(changeId, msg)]);
}

function markAsSent(changeId, msg) {
  if (!changeId || !msg) return;
  const m = _readSentMap();
  m[_fingerprint(changeId, msg)] = {
    sent_at: new Date().toISOString(),
  };
  _writeSentMap(m);
}


function Pill({ tone, children }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      fontSize: 11, fontWeight: 700, padding: '3px 9px',
      borderRadius: 999, textTransform: 'uppercase', letterSpacing: 0.4,
      background: tone.bg, color: tone.fg, border: `1px solid ${tone.border}`,
    }}>{children}</span>
  );
}


// Single drafted authority message — used inside an area card AND in the
// top-level authority summary card. Two action buttons:
//
//   - Copy   — puts "Subject: ...\n\nBody..." on clipboard. Useful when
//              the partner wants to edit before sending.
//   - Send   — POST /api/changes/{id}/query. Every partner message goes
//              through the one query channel; the authority handles clarifications and
//              term-change requests via the same pipeline, so there is no
//              query-vs-counter routing on the partner side.
function NpciMessageCard({ msg, sourceArea, changeId, onSent, collapsedByDefault = false }) {
  const tone = MSG_TONE;
  const [copied, setCopied] = useState(false);
  // Body collapsed-state — true when used inside the top-level authority summary
  // (long list, partner skims subjects then drills in). False inside an
  // already-expanded area card (partner already chose to dig there).
  const [bodyOpen, setBodyOpen] = useState(!collapsedByDefault);
  // Seed send-state from localStorage so a hard refresh after sending keeps
  // showing "Sent" — the backend has the message, the badge should reflect that.
  const [sendState, setSendState] = useState(() =>
    isAlreadySent(changeId, msg) ? 'sent' : 'idle'
  );

  const fullText = `${msg.subject}\n\n${msg.draft_message}`;

  const onCopy = async () => {
    const text = `Subject: ${msg.subject}\n\n${msg.draft_message}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch (_e) { /* clipboard blocked — silent */ }
  };

  const onSend = async () => {
    if (!changeId || sendState === 'sending' || sendState === 'sent') return;
    setSendState('sending');
    try {
      await submitQuery(changeId, fullText);
      // Persist BEFORE updating component state so a parallel mount of
      // the same card (e.g. inside an Area accordion that hasn't been
      // expanded yet) reads the marker correctly.
      markAsSent(changeId, msg);
      setSendState('sent');
      // Hand off to the page so it can switch the Activity tab to the
      // general thread (the one channel partner messages land in) and
      // scroll the chat into view.
      if (onSent) onSent();
    } catch (_e) {
      setSendState('error');
      setTimeout(() => setSendState('idle'), 3000);
    }
  };

  const sendLabel = `Send to ${t('term.authority')}`;
  const sendBusy = sendState === 'sending';
  const sendDone = sendState === 'sent';
  const sendErr  = sendState === 'error';

  return (
    <div style={{
      background: '#fff', border: `1px solid ${tone.border}`,
      borderLeft: `3px solid ${tone.fg}`,
      borderRadius: 8, padding: '10px 12px',
      display: 'flex', flexDirection: 'column', gap: 6,
    }}>
      {/* Row 1 — meta on the left, action buttons on the right.
          Wraps if very narrow. */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
      }}>
        <Pill tone={tone}>{tone.label}</Pill>
        {sourceArea && AREA_META[sourceArea] && (
          <span style={{ fontSize: 10.5, color: T.textMuted }}>
            from {AREA_META[sourceArea].label}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          onClick={onCopy}
          title="Copy subject + body to clipboard"
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '3px 8px', fontSize: 10.5, fontWeight: 600,
            background: copied ? 'rgba(16,185,129,0.10)' : '#fff',
            color: copied ? T.success : T.textSecondary,
            border: `1px solid ${copied ? 'rgba(16,185,129,0.22)' : T.borderSubtle}`,
            borderRadius: 6, cursor: 'pointer',
          }}
        >
          <Copy size={11} /> {copied ? 'Copied' : 'Copy'}
        </button>
        <button
          onClick={onSend}
          disabled={sendBusy || sendDone}
          title={`Send this message to ${t('term.authority')} — opens a thread on the change`}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '3px 9px', fontSize: 10.5, fontWeight: 700,
            background: sendDone
              ? 'rgba(16,185,129,0.10)'
              : sendErr
                ? 'rgba(239,68,68,0.10)'
                : tone.fg,
            color: sendDone
              ? T.success
              : sendErr
                ? T.danger
                : '#fff',
            border: sendDone
              ? '1px solid rgba(16,185,129,0.22)'
              : sendErr
                ? '1px solid rgba(239,68,68,0.22)'
                : `1px solid ${tone.fg}`,
            borderRadius: 6,
            cursor: sendBusy || sendDone ? 'default' : 'pointer',
            opacity: sendBusy ? 0.7 : 1,
          }}
        >
          {sendBusy && <><Loader2 size={11} className="pp-spin" /> Sending…</>}
          {sendDone && <><BadgeCheck size={11} /> Sent</>}
          {sendErr  && <><AlertTriangle size={11} /> Failed</>}
          {sendState === 'idle' && <><Send size={11} /> {sendLabel}</>}
        </button>
      </div>
      {/* Row 2 — full-width subject; whole row is the expand/collapse target. */}
      <button
        onClick={() => setBodyOpen(o => !o)}
        title={bodyOpen ? 'Hide draft body' : 'Show draft body'}
        style={{
          display: 'flex', alignItems: 'flex-start', gap: 6,
          padding: 0, margin: 0,
          background: 'transparent', border: 'none', cursor: 'pointer',
          textAlign: 'left', width: '100%',
        }}
      >
        <span style={{
          color: T.textMuted, flexShrink: 0,
          marginTop: 2, lineHeight: 1,
        }}>
          {bodyOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
        <span style={{
          fontSize: 13, fontWeight: 700, color: T.textPrimary,
          lineHeight: 1.4, flex: 1, minWidth: 0,
        }}>
          {msg.subject}
        </span>
      </button>
      {/* Row 3 — full draft body, only when expanded. */}
      {bodyOpen && (
        <div style={{
          fontSize: 12, color: T.textPrimary, lineHeight: 1.5,
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          paddingLeft: 20,
        }}>
          {msg.draft_message}
        </div>
      )}
    </div>
  );
}


// Reusable collapsible header for the two top-level summary cards. The
// whole header is a click target so the user has a large hit area.
function SummaryCardHeader({ icon, label, count, accent, open, onToggle, hasBody }) {
  return (
    <button
      onClick={onToggle}
      disabled={!hasBody}
      title={hasBody ? (open ? 'Collapse' : 'Expand') : undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 7, width: '100%',
        padding: 0, margin: 0,
        background: 'transparent', border: 'none', textAlign: 'left',
        cursor: hasBody ? 'pointer' : 'default',
      }}
    >
      {icon}
      <span style={{ fontSize: 13, fontWeight: 700, color: T.textPrimary }}>
        {label}
      </span>
      {count != null && (
        <span style={{
          fontSize: 10.5, padding: '1px 7px', borderRadius: 999,
          background: accent.bg, color: accent.fg,
          border: `1px solid ${accent.border}`, fontWeight: 700,
        }}>{count}</span>
      )}
      {hasBody && (
        <span style={{ marginLeft: 'auto', color: T.textMuted, display: 'inline-flex' }}>
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      )}
    </button>
  );
}


// Top-of-panel "Internal work" card — partner's own to-do list aggregated
// across all 6 areas. Sits above the area grid. Header is collapsible so
// the user can hide the list when focused on authority communications.
function InternalSummaryCard({ items }) {
  const hasItems = items && items.length > 0;
  const [open, setOpen] = useState(true);
  const accent = {
    bg: 'rgba(16,185,129,0.10)', fg: T.success, border: 'rgba(16,185,129,0.22)',
  };
  return (
    <div style={{
      background: hasItems ? '#fff' : T.bgSoft,
      border: T.cardBorder, borderRadius: 12,
      borderLeft: hasItems ? `3px solid ${T.success}` : T.cardBorder,
      padding: '14px 16px',
      display: 'flex', flexDirection: 'column', gap: hasItems && open ? 8 : 0,
    }}>
      <SummaryCardHeader
        icon={<CheckSquare size={15} color={T.success} />}
        label="Your team's to-do list"
        count={hasItems ? items.length : null}
        accent={accent}
        open={open}
        onToggle={() => setOpen(o => !o)}
        hasBody={hasItems}
      />
      {!hasItems && (
        <div style={{ fontSize: 12, color: T.textMuted, marginTop: 6 }}>
          No internal-only work needed.
        </div>
      )}
      {hasItems && open && (
        <ol style={{
          margin: 0, paddingLeft: 20,
          fontSize: 12.5, color: T.textPrimary, lineHeight: 1.55,
        }}>
          {items.map((s, i) => <li key={i} style={{ marginBottom: 4 }}>{s}</li>)}
        </ol>
      )}
    </div>
  );
}


// Top-of-panel "Discuss with the authority" card — flat list of drafted messages
// across all areas, each with subject + body + copy / send buttons. Header
// is collapsible so the user can hide the list when focused on internal work.
function NpciSummaryCard({ items, changeId, onSent }) {
  const hasItems = items && items.length > 0;
  const [open, setOpen] = useState(true);
  const accent = {
    bg: 'rgba(37,99,235,0.10)', fg: T.primary, border: 'rgba(37,99,235,0.22)',
  };
  return (
    <div style={{
      background: hasItems ? '#fff' : T.bgSoft,
      border: T.cardBorder, borderRadius: 12,
      borderLeft: hasItems ? `3px solid ${T.primary}` : T.cardBorder,
      padding: '14px 16px',
      display: 'flex', flexDirection: 'column', gap: hasItems && open ? 10 : 0,
    }}>
      <SummaryCardHeader
        icon={<MessageSquare size={15} color={T.primary} />}
        label={`Discuss with ${t('term.authority')}`}
        count={hasItems ? items.length : null}
        accent={accent}
        open={open}
        onToggle={() => setOpen(o => !o)}
        hasBody={hasItems}
      />
      {!hasItems && (
        <div style={{ fontSize: 12, color: T.textMuted, marginTop: 6 }}>
          No {t('term.authority')} communication needed — all terms acceptable as-is.
        </div>
      )}
      {hasItems && open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {items.map((m, i) => (
            <NpciMessageCard
              key={i}
              msg={m}
              sourceArea={m.source_area}
              changeId={changeId}
              onSent={onSent}
              collapsedByDefault
            />
          ))}
        </div>
      )}
    </div>
  );
}


function AreaCard({ area, changeId, onSent }) {
  const meta = AREA_META[area.area] || { label: area.area };
  const tone = STATUS_COLOURS[area.status] || STATUS_COLOURS.unknown;
  const internal = area.internal_actions || [];
  const npci = area.npci_communications || [];
  const [open, setOpen] = useState(false);
  const hasDetail = (area.findings?.length || 0) + internal.length + npci.length > 0;

  return (
    <div style={{
      background: T.cardBg, border: T.cardBorder, borderRadius: 12,
      padding: 14, display: 'flex', flexDirection: 'column', gap: 8,
      minHeight: 140,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: T.textPrimary }}>
          {meta.label}
        </span>
        <Pill tone={tone}>{tone.label}</Pill>
        {area.confidence === 'low' && (
          <span style={{
            fontSize: 10, color: T.textMuted, marginLeft: 'auto',
            fontStyle: 'italic',
          }}>low confidence</span>
        )}
      </div>

      <div style={{ fontSize: 12.5, color: T.textSecondary, lineHeight: 1.45 }}>
        {area.summary}
      </div>

      {/* Counts row — at-a-glance bias for the partner */}
      {(internal.length > 0 || npci.length > 0) && (
        <div style={{
          display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 2,
        }}>
          {internal.length > 0 && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 10.5, fontWeight: 600,
              padding: '2px 7px', borderRadius: 999,
              background: 'rgba(16,185,129,0.08)', color: T.success,
              border: '1px solid rgba(16,185,129,0.18)',
            }}>
              <CheckSquare size={10} />
              {internal.length} internal
            </span>
          )}
          {npci.length > 0 && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 4,
              fontSize: 10.5, fontWeight: 600,
              padding: '2px 7px', borderRadius: 999,
              background: 'rgba(37,99,235,0.08)', color: T.primary,
              border: '1px solid rgba(37,99,235,0.18)',
            }}>
              <Send size={10} />
              {npci.length} to {t('term.authority')}
            </span>
          )}
        </div>
      )}

      {/* Expandable detail — findings + internal actions + the authority messages */}
      {hasDetail && (
        <>
          <button
            onClick={() => setOpen(o => !o)}
            style={{
              display: 'flex', alignItems: 'center', gap: 4,
              padding: '4px 0', fontSize: 11, fontWeight: 600,
              background: 'none', border: 'none', cursor: 'pointer',
              color: T.textMuted, alignSelf: 'flex-start', marginTop: 'auto',
            }}
          >
            {open ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            {open ? 'Hide detail' : 'Show detail'}
          </button>
          {open && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 2 }}>
              {area.findings?.length > 0 && (
                <div>
                  <div style={{
                    fontSize: 10, fontWeight: 700, color: T.textMuted,
                    textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4,
                  }}>Findings</div>
                  <ul style={{ margin: 0, paddingLeft: 16, fontSize: 11.5, color: T.textSecondary, lineHeight: 1.5 }}>
                    {area.findings.map((f, i) => <li key={i} style={{ marginBottom: 3 }}>{f}</li>)}
                  </ul>
                </div>
              )}
              {internal.length > 0 && (
                <div>
                  <div style={{
                    fontSize: 10, fontWeight: 700, color: T.success,
                    textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 4,
                  }}>Internal next steps</div>
                  <ul style={{ margin: 0, paddingLeft: 16, fontSize: 11.5, color: T.textPrimary, lineHeight: 1.5 }}>
                    {internal.map((s, i) => <li key={i} style={{ marginBottom: 3 }}>{s}</li>)}
                  </ul>
                </div>
              )}
              {npci.length > 0 && (
                <div>
                  <div style={{
                    fontSize: 10, fontWeight: 700, color: T.primary,
                    textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 6,
                  }}>Discuss with {t('term.authority')}</div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {npci.map((m, i) => (
                      <NpciMessageCard key={i} msg={m} changeId={changeId} onSent={onSent} />
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}


export default function FeasibilityPanel({ changeId, onMessageSent, decision }) {
  const queryClient = useQueryClient();
  const { data, isLoading, isError } = useQuery({
    queryKey: ['feasibility', changeId],
    queryFn: () => getFeasibilityReport(changeId),
    // Auto-trigger from the A2A handler usually completes in ~10s. Poll
    // gently while the row is missing so the UI lights up without a
    // manual refresh; stop polling once the report lands.
    refetchInterval: (q) => (q.state.data ? false : 5000),
    enabled: Boolean(changeId),
  });

  const runMutation = useMutation({
    mutationFn: () => runFeasibilityAnalysis(changeId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['feasibility', changeId] }),
  });
  // Surface the backend's message (e.g. "No partner profile configured…") rather
  // than a generic "check logs" — the 400 it returns is actionable.
  const runError = runMutation.isError
    ? (runMutation.error?.response?.data?.detail || 'Run failed — check backend logs.')
    : null;

  // Empty-state — no report yet (auto-run may still be in flight, or
  // it failed silently). Either way the partner gets a CTA to run it.
  if (!isLoading && !isError && data == null) {
    return (
      <div style={{
        background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
        boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
        display: 'flex', alignItems: 'center', gap: 14,
      }}>
        <Brain size={24} color={T.primary} style={{ flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary, marginBottom: 2 }}>
            Feasibility analysis
          </div>
          <div style={{ fontSize: 12, color: T.textSecondary }}>
            No report yet for this change. Run the analyser to evaluate it against your PARTNER.md profile.
          </div>
          {runError && (
            <div style={{ fontSize: 12, color: T.danger, marginTop: 6 }}>
              {runError}
            </div>
          )}
        </div>
        <Button
          variant="primary"
          icon={Brain}
          loading={runMutation.isPending}
          loadingText="Analysing…"
          onClick={() => runMutation.mutate()}
        >
          Run analyser
        </Button>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div style={{
        background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
        boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
        display: 'flex', alignItems: 'center', gap: 10, color: T.textMuted, fontSize: 13,
      }}>
        <Loader2 size={15} className="pp-spin" /> Loading feasibility report…
      </div>
    );
  }

  if (isError) {
    return (
      <div style={{
        background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.22)',
        borderRadius: T.cardRadius, padding: 14, marginBottom: 18,
        fontSize: 13, color: T.danger,
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <AlertTriangle size={14} />
        Could not load feasibility report.
      </div>
    );
  }

  const report = data.report;
  const posture = POSTURE_COLOURS[report.overall_posture] || POSTURE_COLOURS.ready_with_conditions;

  // Order the area cards deterministically (the backend may return them
  // in any order; we always render Deadline → Scope → Limits → Spec → Deps → Role).
  const areas = [...(report.areas || [])].sort(
    (a, b) => (AREA_META[a.area]?.order ?? 99) - (AREA_META[b.area]?.order ?? 99)
  );

  return (
    <div style={{
      background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
      boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 14 }}>
        <Brain size={22} color={T.primary} style={{ marginTop: 2, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 15, fontWeight: 700, color: T.textPrimary }}>
              Feasibility analysis
            </span>
            <Pill tone={posture}>{posture.label}</Pill>
          </div>
          <div style={{ fontSize: 13, color: T.textSecondary, lineHeight: 1.45 }}>
            {report.one_line_summary}
          </div>
        </div>
        {decision !== 'accepted' && (
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            loading={runMutation.isPending}
            loadingText="Re-running…"
            title="Re-run analyser against the current PARTNER.md and change documents"
            onClick={() => runMutation.mutate()}
          >
            Re-run
          </Button>
        )}
      </div>

      {/* TWO TOP-LEVEL ACTION CARDS — the primary read for partner ops.
          Left: internal-only work. Right: drafted messages for {t('term.authority')}.
          Stacks vertically on narrow screens. `alignItems: start` so the
          shorter card doesn't stretch to match the taller one — avoids
          the awkward whitespace when content volumes differ significantly. */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
        alignItems: 'start',
        gap: 12, marginBottom: 14,
      }}>
        <InternalSummaryCard items={report.internal_action_summary} />
        <NpciSummaryCard
          items={report.npci_communication_summary}
          changeId={changeId}
          onSent={onMessageSent}
        />
      </div>

      {/* Per-area breakdown — supporting detail under the two summary cards.
          `alignItems: start` so an expanded card's detail doesn't stretch
          its neighbours into matching whitespace. */}
      <div style={{
        fontSize: 10.5, fontWeight: 700, color: T.textMuted,
        textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 8,
      }}>Area breakdown</div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        alignItems: 'start',
        gap: 12, marginBottom: 14,
      }}>
        {areas.map(a => (
          <AreaCard key={a.area} area={a} changeId={changeId} onSent={onMessageSent} />
        ))}
      </div>

      {/* Additional findings (out-of-band observations) */}
      {report.additional_findings?.length > 0 && (
        <div style={{
          padding: '10px 14px', background: 'rgba(37,99,235,0.04)',
          border: '1px solid rgba(37,99,235,0.15)', borderRadius: 10,
          marginBottom: 10,
        }}>
          <div style={{
            fontSize: 10.5, fontWeight: 700, color: T.primary,
            textTransform: 'uppercase', letterSpacing: 0.4, marginBottom: 6,
          }}>Additional findings</div>
          <ul style={{ margin: 0, paddingLeft: 20, fontSize: 12.5, color: T.textPrimary, lineHeight: 1.55 }}>
            {report.additional_findings.map((s, i) => <li key={i} style={{ marginBottom: 3 }}>{s}</li>)}
          </ul>
        </div>
      )}

      {/* Footer meta */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap',
        fontSize: 10.5, color: T.textMuted, paddingTop: 4,
      }}>
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <Clock size={11} /> v{data.version} · {formatDateTime(data.generated_at)}
        </span>
        {data.profile_version && (
          <span>profile {data.profile_version}</span>
        )}
        {data.model_used && (
          <span>{data.model_used}</span>
        )}
        {runError && (
          <span style={{ color: T.danger, marginLeft: 'auto' }}>
            {runError}
          </span>
        )}
      </div>
    </div>
  );
}
