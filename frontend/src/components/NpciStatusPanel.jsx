// NpciStatusPanel — "what does NPCI actually have?"
//
// Every stage the partner sees on this page is derived from our OWN mirror,
// built out of inbound A2A pushes. When a push is lost — or a local write
// fails after NPCI already acked our send — the mirror drifts and nothing in
// the UI reveals it. This panel pulls NPCI's authoritative record on demand
// (GET /api/changes/{id}/npci-status) and shows, field by field, where the two
// disagree.
//
// On demand, never polled: the request costs an auth handshake plus a round
// trip to NPCI, and the answer only matters when someone is questioning the
// state. The panel is read-only on both sides — checking can never itself
// change what it reports.
import { useQuery } from '@tanstack/react-query';
import {
  AlertTriangle, ChevronDown, ChevronRight, CircleCheck,
  Loader2, RefreshCw, ShieldCheck,
} from 'lucide-react';
import { useState } from 'react';

import { formatDateTime, formatRelative } from '../lib/datetime';
import { getNpciStatus } from '../services/api';

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

const STAGE_LABELS = {
  communicated:   'Communicated',
  accepted:       'Accepted',
  design:         'Design',
  coding:         'Coding',
  testing:        'Testing',
  ready_for_cert: 'Ready for Certification',
  certified:      'Certified',
};

const prettyStage = (key) => STAGE_LABELS[key] || (key || '—').replace(/_/g, ' ');

function Field({ label, value }) {
  return (
    <div style={{ minWidth: 130 }}>
      <div style={{
        fontSize: 10, color: T.textMuted, textTransform: 'uppercase',
        letterSpacing: 0.4, fontWeight: 600, marginBottom: 2,
      }}>{label}</div>
      <div style={{ fontSize: 13, color: T.textPrimary, fontWeight: 500 }}>
        {value ?? '—'}
      </div>
    </div>
  );
}

export default function NpciStatusPanel({ changeId }) {
  const [showDetail, setShowDetail] = useState(false);

  // enabled:false — nothing fires until the partner asks. refetch() on click.
  const { data, error, isFetching, refetch, isFetched } = useQuery({
    queryKey: ['npciStatus', changeId],
    queryFn: () => getNpciStatus(changeId),
    enabled: false,
    retry: false,
    gcTime: 0,
  });

  const upstreamError = error?.response?.data?.detail || error?.message;
  const discrepancies = data?.discrepancies || [];
  const mismatches = discrepancies.filter((d) => d.severity === 'mismatch');

  return (
    <div style={{
      background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
      boxShadow: T.cardShadow, padding: '16px 22px', marginBottom: 16,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <ShieldCheck size={16} style={{ color: T.primary, flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>
            NPCI record
          </div>
          <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 2 }}>
            {isFetched && data
              ? `Checked ${formatRelative(data.checked_at)} — NPCI's database is the source of truth.`
              : 'Compare the stage shown above against what NPCI actually holds for us.'}
          </div>
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          disabled={isFetching}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 6,
            padding: '8px 14px', fontSize: 12, fontWeight: 600,
            color: '#fff', background: T.primary,
            border: 'none', borderRadius: 8,
            cursor: isFetching ? 'wait' : 'pointer',
            opacity: isFetching ? 0.7 : 1,
          }}
        >
          {isFetching
            ? <Loader2 size={14} style={{ animation: 'pp-spin 1s linear infinite' }} />
            : <RefreshCw size={14} />}
          {isFetching ? 'Checking…' : isFetched ? 'Re-check' : 'Verify with NPCI'}
        </button>
      </div>

      {error && (
        <div style={{
          marginTop: 12, padding: '10px 14px', borderRadius: 10,
          background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.22)',
          fontSize: 12.5, color: '#7f1d1d',
          display: 'flex', alignItems: 'flex-start', gap: 8,
        }}>
          <AlertTriangle size={14} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{upstreamError || 'Could not reach NPCI.'}</span>
        </div>
      )}

      {data && (
        <>
          {/* Verdict */}
          <div style={{
            marginTop: 14, padding: '12px 16px', borderRadius: 10,
            background: data.in_sync ? 'rgba(16,185,129,0.07)' : 'rgba(245,158,11,0.08)',
            border: `1px solid ${data.in_sync ? 'rgba(16,185,129,0.24)' : 'rgba(245,158,11,0.26)'}`,
            display: 'flex', alignItems: 'center', gap: 10,
          }}>
            {data.in_sync
              ? <CircleCheck size={16} style={{ color: T.success, flexShrink: 0 }} />
              : <AlertTriangle size={16} style={{ color: T.warning, flexShrink: 0 }} />}
            <div style={{ fontSize: 13, fontWeight: 600, color: T.textPrimary }}>
              {data.in_sync
                ? 'In sync — our record matches NPCI on every tracked field.'
                : `${mismatches.length} field${mismatches.length === 1 ? '' : 's'} disagree with NPCI.`}
            </div>
          </div>

          {/* Stage, side by side */}
          <div style={{
            marginTop: 14, display: 'flex', gap: 28, flexWrap: 'wrap',
            padding: '12px 16px', background: T.bgSoft, borderRadius: 10,
          }}>
            <Field label="Our stage" value={prettyStage(data.local?.stage?.current)} />
            <Field label="NPCI stage" value={prettyStage(data.npci?.stage?.current)} />
            <Field
              label="NPCI assignment status"
              value={(data.npci?.assignment?.status || '—').replace(/_/g, ' ')}
            />
            <Field label="Kit version at NPCI" value={data.npci?.negotiation?.version} />
            <Field
              label="Negotiation"
              value={data.npci?.negotiation?.frozen ? 'Frozen' : 'Open'}
            />
          </div>

          {/* Discrepancies */}
          {discrepancies.length > 0 && (
            <div style={{ marginTop: 14 }}>
              {discrepancies.map((d) => {
                const isInfo = d.severity === 'info';
                return (
                  <div
                    key={d.field}
                    style={{
                      padding: '10px 14px', marginBottom: 8, borderRadius: 10,
                      background: isInfo ? 'rgba(37,99,235,0.05)' : 'rgba(245,158,11,0.06)',
                      border: `1px solid ${isInfo ? 'rgba(37,99,235,0.18)' : 'rgba(245,158,11,0.22)'}`,
                    }}
                  >
                    <div style={{
                      display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'baseline',
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: T.textPrimary, minWidth: 150 }}>
                        {d.label}
                      </div>
                      <div style={{ fontSize: 12.5, color: T.textSecondary }}>
                        Ours: <strong style={{ color: T.textPrimary }}>{String(d.local)}</strong>
                        <span style={{ margin: '0 8px', color: T.textMuted }}>·</span>
                        NPCI: <strong style={{ color: T.textPrimary }}>{String(d.npci)}</strong>
                      </div>
                    </div>
                    <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 4 }}>
                      {d.hint}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Full NPCI record */}
          <button
            type="button"
            onClick={() => setShowDetail((v) => !v)}
            style={{
              marginTop: 8, display: 'inline-flex', alignItems: 'center', gap: 4,
              background: 'none', border: 'none', padding: 0, cursor: 'pointer',
              fontSize: 12, fontWeight: 600, color: T.primary,
            }}
          >
            {showDetail ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            {showDetail ? 'Hide NPCI record' : 'Show full NPCI record'}
          </button>

          {showDetail && (
            <div style={{ marginTop: 12, fontSize: 12.5, color: T.textSecondary }}>
              {/* Milestones NPCI has recorded */}
              <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 12 }}>
                {['design_completed', 'coding_completed', 'testing_completed'].map((k) => (
                  <Field
                    key={k}
                    label={k.replace(/_/g, ' ')}
                    value={data.npci?.progress_steps?.[k]
                      ? formatDateTime(data.npci.progress_steps[k])
                      : 'Not recorded'}
                  />
                ))}
              </div>

              {data.npci?.certification?.latest_run && (
                <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 12 }}>
                  <Field label="Cert run" value={`#${data.npci.certification.latest_run.run_number}`} />
                  <Field label="Run status" value={data.npci.certification.latest_run.status} />
                  <Field
                    label="Results"
                    value={`${data.npci.certification.latest_run.passed ?? 0} pass / ${data.npci.certification.latest_run.failed ?? 0} fail`}
                  />
                  <Field
                    label="Started"
                    value={formatDateTime(data.npci.certification.latest_run.started_at)}
                  />
                </div>
              )}

              {/* Wire heartbeat — distinguishes a state disagreement from a
                  transport problem (our sends never landing at all). */}
              <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 12 }}>
                <Field
                  label="Last message NPCI received from us"
                  value={data.npci?.last_message?.inbound
                    ? `${data.npci.last_message.inbound.task_type} — ${formatRelative(data.npci.last_message.inbound.at)}`
                    : 'None'}
                />
                <Field
                  label="Last message NPCI sent us"
                  value={data.npci?.last_message?.outbound
                    ? `${data.npci.last_message.outbound.task_type} — ${formatRelative(data.npci.last_message.outbound.at)}`
                    : 'None'}
                />
              </div>

              {/* Status history — who moved us, and when */}
              {(data.npci?.assignment?.history || []).length > 0 && (
                <div>
                  <div style={{
                    fontSize: 10, color: T.textMuted, textTransform: 'uppercase',
                    letterSpacing: 0.4, fontWeight: 600, marginBottom: 6,
                  }}>
                    Status history at NPCI
                  </div>
                  {data.npci.assignment.history.map((h, i) => (
                    <div key={i} style={{
                      display: 'flex', gap: 12, flexWrap: 'wrap',
                      padding: '5px 0', borderTop: i ? `1px solid ${T.borderSubtle}` : 'none',
                    }}>
                      <span style={{ color: T.textPrimary, fontWeight: 500, minWidth: 200 }}>
                        {(h.from || '—').replace(/_/g, ' ')} → {(h.to || '').replace(/_/g, ' ')}
                      </span>
                      <span style={{ color: T.textMuted }}>{formatDateTime(h.at)}</span>
                      <span style={{ color: T.textMuted }}>by {h.actor}</span>
                      {h.reason && <span style={{ color: T.textSecondary }}>{h.reason}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
