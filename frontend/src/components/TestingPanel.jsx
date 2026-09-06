// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// TestingPanel — renders the partner-side test plan produced by the Test agent.
// The agent AUTHORS the plan + maps coverage to the authority cert cases; the
// authoritative certification run is delegated to the authority via the existing
// cert-readiness lifecycle (a note points there). Mirrors DesignPanel.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { t } from '../strings'
import { formatDateTime } from '../lib/datetime';
import {
  AlertTriangle, ChevronDown, ChevronRight, Download, FlaskConical,
  Loader2, RefreshCw,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';

import { downloadTestDocx, getAgentJob, getTestReport, runTestAnalysis } from '../services/api';
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

const READINESS_META = {
  ready_to_certify: { label: 'Ready to certify', colour: T.success, tone: 'success' },
  needs_test_data:  { label: 'Needs test data',  colour: T.warning, tone: 'warning' },
  gaps:             { label: 'Gaps',             colour: T.warning, tone: 'warning' },
  blocked:          { label: 'Blocked',          colour: T.danger,  tone: 'danger' },
};

const card = {
  background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
  boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
};

export default function TestingPanel({ changeId }) {
  const queryClient = useQueryClient();
  // Plan body minimized by default — readiness + cert-coverage + counts carry
  // the signal; the full markdown opens on the accordion toggle.
  const [expanded, setExpanded] = useState(false);

  const runMutation = useMutation({
    mutationFn: () => runTestAnalysis(changeId),
    // POST is a fast 202 now — refresh the job query so polling starts at once.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'testing'] }),
  });

  // 202 + poll pattern: the job query drives button/progress state and
  // survives refresh/navigation.
  const { data: job } = useQuery({
    queryKey: ['job', changeId, 'testing'],
    queryFn: () => getAgentJob(changeId, 'testing'),
    enabled: Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 4000 : false),
  });
  const jobRunning = job?.status === 'running';
  const busy = jobRunning || runMutation.isPending;

  const { data, isLoading, isError } = useQuery({
    queryKey: ['test-plan', changeId],
    queryFn: () => getTestReport(changeId),
    enabled: Boolean(changeId),
    // While the job runs, poll for the persisted report too.
    refetchInterval: jobRunning ? 10000 : false,
  });

  // Job finished → pull the fresh report once.
  useEffect(() => {
    if (job && job.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['test-plan', changeId] });
    }
  }, [job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const jobStatusLine = (jobRunning || job?.status === 'error') && (
    <div style={{ marginTop: 10, fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6, color: job?.status === 'error' ? T.danger : T.textSecondary }}>
      {jobRunning
        ? <><Loader2 size={12} className="pp-spin" /> {job.progress || 'running…'}</>
        : <><AlertTriangle size={12} /> last run failed: {job.error}</>}
    </div>
  );

  if (!isLoading && !isError && data == null) {
    return (
      <div style={{ ...card }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <FlaskConical size={24} color={T.primary} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary, marginBottom: 2 }}>
              Test plan
            </div>
            <div style={{ fontSize: 12, color: T.textSecondary }}>
              No test plan yet. Generate partner-side test cases and cert-coverage mapping from the {t('term.authority')} documents.
            </div>
          </div>
          <Button
            variant="primary"
            icon={FlaskConical}
            loading={busy}
            loadingText="Planning…"
            onClick={() => runMutation.mutate()}
          >
            Run test plan
          </Button>
        </div>
        {jobStatusLine}
      </div>
    );
  }

  if (isLoading) {
    return (
      <div style={{ ...card, display: 'flex', alignItems: 'center', gap: 10, color: T.textMuted, fontSize: 13 }}>
        <Loader2 size={15} className="pp-spin" /> Loading test plan…
      </div>
    );
  }

  if (isError) {
    return (
      <div style={{ ...card, background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.22)', display: 'flex', alignItems: 'center', gap: 8, color: T.danger, fontSize: 13 }}>
        <AlertTriangle size={14} /> Could not load the test plan.
      </div>
    );
  }

  const report = data.report || {};
  const r = READINESS_META[report.readiness] || { label: report.readiness || 'unknown', colour: T.textMuted };
  const isMock = report?._meta?.mock;
  const suites = Array.isArray(report.suites) ? report.suites : [];
  const cov = report.cert_coverage || {};
  const caseCount = suites.reduce((n, s) => n + (Array.isArray(s.cases) ? s.cases.length : 0), 0);

  return (
    <div style={card}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <FlaskConical size={20} color={T.primary} style={{ flexShrink: 0, marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>Test plan</span>
            <Badge solid tone={r.tone || 'neutral'}>{r.label}</Badge>
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
            onClick={() => downloadTestDocx(changeId, `test_plan_v${data.version}.docx`)}>
            .docx
          </Button>
          <Button variant="secondary" size="sm" icon={RefreshCw} loading={busy} title="Re-run the test agent"
            onClick={() => runMutation.mutate()}>
            Re-run
          </Button>
        </div>
      </div>

      {jobStatusLine}

      {/* Cert coverage — the delegation bridge */}
      <div style={{ marginTop: 14, display: 'flex', flexWrap: 'wrap', gap: 14, alignItems: 'center', fontSize: 12, color: T.textSecondary }}>
        <span>
          {t('term.authorityCap')} cert coverage:{' '}
          <strong style={{ color: T.textPrimary }}>{cov.covered ?? 0}/{cov.npci_cases_total ?? 0}</strong>
          {' '}cases
        </span>
        {caseCount > 0 && <span>· {caseCount} partner test cases across {suites.length} suites</span>}
        {Array.isArray(cov.gaps) && cov.gaps.length > 0 && (
          <span style={{ color: T.warning }}>· {cov.gaps.length} coverage gap{cov.gaps.length !== 1 ? 's' : ''}</span>
        )}
      </div>

      {/* Test plan body */}
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 6, background: 'none', border: 'none', cursor: 'pointer', color: T.textSecondary, fontSize: 12.5, fontWeight: 600, padding: 0 }}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Test plan
      </button>
      {expanded && (
        <div className="pp-markdown" style={{ marginTop: 10, paddingTop: 12, borderTop: `1px solid ${T.borderSubtle}`, fontSize: 13, color: T.textPrimary, lineHeight: 1.6 }}>
          <ReactMarkdown>{report.test_plan_markdown || '_No plan body._'}</ReactMarkdown>
        </div>
      )}

      {/* Delegation note — execution runs on the authority's side */}
      <div style={{ marginTop: 14, padding: '8px 12px', background: T.bgSoft, border: `1px solid ${T.borderSubtle}`, borderRadius: 8, fontSize: 11.5, color: T.textSecondary }}>
        Execution: the authoritative certification suite is run by {t('term.authority')} after you declare readiness — submit per-TC test data and declare readiness in the <strong>Certification</strong> section.
      </div>

      <div style={{ marginTop: 12, fontSize: 11, color: T.textMuted, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {data.model_used && <span>model: {data.model_used}</span>}
        {report?._meta?.profile_version && <span>profile: {report._meta.profile_version}</span>}
        <span>generated: {formatDateTime(data.generated_at)}</span>
      </div>
    </div>
  );
}
