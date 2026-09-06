// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// DesignPanel — renders the partner-side design document produced by the Design
// agent for a change, with a run/re-run affordance and a .docx download.
// Mirrors FeasibilityPanel's query/mutation/empty-state pattern; the primary
// artifact is the markdown design document (document_markdown).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { t } from '../strings'
import { formatDateTime } from '../lib/datetime';
import {
  AlertTriangle, Download, DraftingCompass, Eye, EyeOff,
  Loader2, RefreshCw,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';

import { downloadDesignDocx, getAgentJob, getDesignReport, runDesignAnalysis } from '../services/api';
import Badge from './ui/Badge';
import Button from './ui/Button';

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

const POSTURE_META = {
  ready:        { label: 'Ready',        colour: T.success, tone: 'success' },
  needs_review: { label: 'Needs review', colour: T.warning, tone: 'warning' },
  risky:        { label: 'Risky',        colour: T.warning, tone: 'warning' },
  blocked:      { label: 'Blocked',      colour: T.danger,  tone: 'danger' },
};

const card = {
  background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
  boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
};

export default function DesignPanel({ changeId }) {
  const queryClient = useQueryClient();
  // Document body hidden by default — the header summary + posture carry the
  // signal; the full markdown shows on Preview.
  const [expanded, setExpanded] = useState(false);

  const runMutation = useMutation({
    mutationFn: () => runDesignAnalysis(changeId),
    // POST is a fast 202 now — refresh the job query so polling starts at once.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'design'] }),
  });

  // 202 + poll pattern: the analyse POST returns a job id instantly; this
  // query drives the button/progress state and survives refresh/navigation.
  const { data: job } = useQuery({
    queryKey: ['job', changeId, 'design'],
    queryFn: () => getAgentJob(changeId, 'design'),
    enabled: Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 4000 : false),
  });
  const jobRunning = job?.status === 'running';
  const busy = jobRunning || runMutation.isPending;

  const { data, isLoading, isError } = useQuery({
    queryKey: ['design', changeId],
    queryFn: () => getDesignReport(changeId),
    enabled: Boolean(changeId),
    // While the job runs, poll for the persisted report too.
    refetchInterval: jobRunning ? 10000 : false,
  });

  // Job finished → pull the fresh report once.
  useEffect(() => {
    if (job && job.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['design', changeId] });
    }
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const jobStatusLine = (jobRunning || job?.status === 'error') && (
    <div style={{ marginTop: 10, fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6, color: job?.status === 'error' ? T.danger : T.textSecondary }}>
      {jobRunning
        ? <><Loader2 size={12} className="pp-spin" /> {job.progress || 'running…'}</>
        : <><AlertTriangle size={12} /> last run failed: {job.error}</>}
    </div>
  );

  // Empty state — no design produced yet.
  if (!isLoading && !isError && data == null) {
    return (
      <div style={{ ...card }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <DraftingCompass size={24} color={T.primary} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary, marginBottom: 2 }}>
              Design document
            </div>
            <div style={{ fontSize: 12, color: T.textSecondary }}>
              No design yet. Generate a partner-side design from the {t('term.authority')} documents and your PARTNER.md profile.
            </div>
          </div>
          <Button
            variant="primary"
            icon={DraftingCompass}
            loading={busy}
            loadingText="Designing…"
            onClick={() => runMutation.mutate()}
          >
            Run design
          </Button>
        </div>
        {jobStatusLine}
      </div>
    );
  }

  if (isLoading) {
    return (
      <div style={{ ...card, display: 'flex', alignItems: 'center', gap: 10, color: T.textMuted, fontSize: 13 }}>
        <Loader2 size={15} className="pp-spin" /> Loading design document…
      </div>
    );
  }

  if (isError) {
    return (
      <div style={{ ...card, background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.22)', display: 'flex', alignItems: 'center', gap: 8, color: T.danger, fontSize: 13 }}>
        <AlertTriangle size={14} /> Could not load the design document.
      </div>
    );
  }

  const report = data.report || {};
  const posture = POSTURE_META[report.design_posture] || { label: report.design_posture || 'unknown', colour: T.textMuted };
  const isMock = report?._meta?.mock;
  const sections = Array.isArray(report.sections) ? report.sections : [];
  const components = Array.isArray(report.components_touched) ? report.components_touched : [];

  return (
    <div style={card}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <DraftingCompass size={20} color={T.primary} style={{ flexShrink: 0, marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>Design document</span>
            <Badge solid tone={posture.tone || 'neutral'}>{posture.label}</Badge>
            {isMock && (
              <span style={{ fontSize: 10.5, fontWeight: 600, color: T.warning, border: `1px solid ${T.warning}`, padding: '1px 6px', borderRadius: 6 }}>
                mock — no LLM key
              </span>
            )}
            <span style={{ fontSize: 11, color: T.textMuted }}>v{data.version}</span>
          </div>
          {report.one_line_summary && (
            <div style={{ fontSize: 12.5, color: T.textSecondary, marginTop: 3 }}>{report.one_line_summary}</div>
          )}
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <Button variant="secondary" size="sm" icon={Download} title="Download as .docx"
            onClick={() => downloadDesignDocx(changeId, `design_v${data.version}.docx`)}>
            .docx
          </Button>
          <Button variant="secondary" size="sm" icon={RefreshCw} loading={busy} title="Re-run the design agent"
            onClick={() => runMutation.mutate()}>
            Re-run
          </Button>
        </div>
      </div>

      {jobStatusLine}

      {/* Components touched + section index */}
      {components.length > 0 && (
        <div style={{ marginTop: 14, display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {components.map((c, i) => (
            <span key={`${c.component || 'comp'}-${i}`} style={{ fontSize: 11, color: T.textSecondary, background: T.bgSoft, border: `1px solid ${T.borderSubtle}`, padding: '3px 8px', borderRadius: 8 }}>
              <strong>{c.component}</strong>{c.vendor ? ` · ${c.vendor}` : ''}{c.change ? ` — ${c.change}` : ''}
            </span>
          ))}
        </div>
      )}

      {/* The design document body — hidden until Preview */}
      <Button
        variant="secondary"
        size="sm"
        icon={expanded ? EyeOff : Eye}
        style={{ marginTop: 16 }}
        onClick={() => setExpanded((v) => !v)}
        title={expanded ? 'Hide the design document' : 'Preview the design document'}
      >
        {expanded ? 'Hide preview' : 'Preview'}
      </Button>
      {expanded && (
        <div className="pp-markdown" style={{ marginTop: 10, paddingTop: 12, borderTop: `1px solid ${T.borderSubtle}`, fontSize: 13, color: T.textPrimary, lineHeight: 1.6 }}>
          <ReactMarkdown>{report.document_markdown || '_No document body._'}</ReactMarkdown>
        </div>
      )}

      {/* Footer meta */}
      <div style={{ marginTop: 14, fontSize: 11, color: T.textMuted, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {data.model_used && <span>model: {data.model_used}</span>}
        {report?._meta?.profile_version && <span>profile: {report._meta.profile_version}</span>}
        <span>generated: {formatDateTime(data.generated_at)}</span>
        {sections.length > 0 && <span>{sections.length} sections</span>}
      </div>
    </div>
  );
}
