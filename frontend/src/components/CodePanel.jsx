// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// CodePanel — renders the partner-side implementation plan produced by the Code
// agent. Shows a grounding banner (green when repo-grounded via Code RAG, amber
// when spec-only) and an "Open MR" action that generates whole files from the
// plan and opens a merge request on the indexed repo. Mirrors DesignPanel/TestingPanel.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { t } from '../strings'
import { formatDateTime } from '../lib/datetime';
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Code2, Download, FileCode2,
  GitPullRequest, Info, Loader2, RefreshCw, ShieldCheck, Sparkles, Wrench,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';

import {
  applyCodeFix, downloadCodeDocx, generateCode, getAgentJob, getCodeMergeRequest,
  getCodeReport, getCodeReview, getCodeReviewHistory, getGeneratedFiles,
  openCodeMergeRequest, runCodeAnalysis, runCodeReview,
} from '../services/api';
import { buildMergeRequestUrl } from '../utils/mrUrl';
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
  plan_ready:        { label: 'Plan ready',        colour: T.success, tone: 'success' },
  needs_repo_context:{ label: 'Needs repo context', colour: T.warning, tone: 'warning' },
  risky:             { label: 'Risky',             colour: T.warning, tone: 'warning' },
  blocked:           { label: 'Blocked',           colour: T.danger,  tone: 'danger' },
};

const CONF_COLOUR = { high: T.success, medium: T.warning, low: T.danger };

// Review finding severities → chip colour. critical/high block hardest visually.
const SEV_COLOUR = {
  critical: T.danger, high: T.danger, medium: T.warning, low: T.textMuted, info: T.textMuted,
};
const REVIEWER_LABEL = {
  code_quality: 'Code Quality',
  security: 'Security',
  lint: 'Lint (deterministic)',
  design_alignment: 'Design Alignment (advisory)',
};

const card = {
  background: T.cardBg, border: T.cardBorder, borderRadius: T.cardRadius,
  boxShadow: T.cardShadow, padding: 18, marginBottom: 18,
};

/**
 * The merge-request reference, rendered as a link only when the URL can be
 * built locally from build-time configuration.
 *
 * The API deliberately no longer returns `mr_url`; it sends `project_path` and
 * `mr_iid`, and `buildMergeRequestUrl` assembles the address from a build-time
 * base. That is what removed the `data` → `href` dataflow Checkmarx reported
 * here (Client DOM XSS, Reflected XSS path 1, Client DOM Open Redirect).
 *
 * When the URL cannot be built — VITE_GITLAB_BASE_URL unset, or a pre-migration
 * row with no stored `project_path` — this renders the MR number as plain text
 * rather than a dead '#' anchor. An inert link that looks clickable is the UX
 * bug the ArtifactRef change fixed; repeating it here would be a regression.
 */
function MergeRequestLink({ projectPath, iid }) {
  const label = iid ? `!${iid}` : 'view MR';
  const url = buildMergeRequestUrl(projectPath, iid);

  if (!url) {
    return (
      <strong
        title="Set VITE_GITLAB_BASE_URL at build time to link merge requests directly."
        style={{ color: T.textSecondary, fontWeight: 600 }}
      >
        {label}
      </strong>
    );
  }

  return (
    <a href={url} target="_blank" rel="noopener noreferrer" style={{ color: T.primary, fontWeight: 600 }}>
      {label}
    </a>
  );
}

export default function CodePanel({ changeId }) {
  const queryClient = useQueryClient();
  // Plan body minimized by default — the posture/summary/file-change chips
  // carry the signal; the full markdown opens on the accordion toggle.
  const [expanded, setExpanded] = useState(false);
  const [showHistory, setShowHistory] = useState(false);

  const runMutation = useMutation({
    mutationFn: () => runCodeAnalysis(changeId),
    // POST is a fast 202 now — refresh the job query so polling starts at once.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'code'] }),
  });

  // 202 + poll pattern: job queries drive button/progress state and survive
  // refresh/navigation. Two job streams here: the plan run and the MR push.
  const { data: planJob } = useQuery({
    queryKey: ['job', changeId, 'code'],
    queryFn: () => getAgentJob(changeId, 'code'),
    enabled: Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 4000 : false),
  });
  const planRunning = planJob?.status === 'running';
  const planBusy = planRunning || runMutation.isPending;

  const { data, isLoading, isError } = useQuery({
    queryKey: ['code-plan', changeId],
    queryFn: () => getCodeReport(changeId),
    enabled: Boolean(changeId),
    // While the job runs, poll for the persisted report too.
    refetchInterval: planRunning ? 10000 : false,
  });

  useEffect(() => {
    if (planJob && planJob.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['code-plan', changeId] });
    }
  }, [planJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const mrMutation = useMutation({
    mutationFn: () => openCodeMergeRequest(changeId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'mr'] }),
  });

  const { data: mrJob } = useQuery({
    queryKey: ['job', changeId, 'mr'],
    queryFn: () => getAgentJob(changeId, 'mr'),
    enabled: Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 4000 : false),
  });
  const mrRunning = mrJob?.status === 'running';
  const mrBusy = mrRunning || mrMutation.isPending;

  const { data: mr } = useQuery({
    queryKey: ['code-mr', changeId],
    queryFn: () => getCodeMergeRequest(changeId),
    enabled: Boolean(changeId),
    refetchInterval: mrRunning ? 10000 : false,
  });

  useEffect(() => {
    if (mrJob && mrJob.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['code-mr', changeId] });
    }
  }, [mrJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Code-review / security-review loop ────────────────────────────────────
  // Three job streams (generate files → review → apply fixes) + the review
  // report. Each mutation kicks a 202 job; the report query drives the findings
  // list + the can_open_mr gate. Status is derived server-side.
  const genMutation = useMutation({
    mutationFn: () => generateCode(changeId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'codegen'] }),
  });
  const reviewMutation = useMutation({
    mutationFn: () => runCodeReview(changeId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'review'] }),
  });
  const fixMutation = useMutation({
    mutationFn: () => applyCodeFix(changeId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['job', changeId, 'fix'] }),
  });

  const jobOpts = (kind) => ({
    queryKey: ['job', changeId, kind],
    queryFn: () => getAgentJob(changeId, kind),
    enabled: Boolean(changeId),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 4000 : false),
  });
  const { data: genJob } = useQuery(jobOpts('codegen'));
  const { data: reviewJob } = useQuery(jobOpts('review'));
  const { data: fixJob } = useQuery(jobOpts('fix'));
  const genRunning = genJob?.status === 'running';
  const reviewRunning = reviewJob?.status === 'running';
  const fixRunning = fixJob?.status === 'running';
  const loopBusy = genRunning || reviewRunning || fixRunning
    || genMutation.isPending || reviewMutation.isPending || fixMutation.isPending;

  const { data: review } = useQuery({
    queryKey: ['code-review', changeId],
    queryFn: () => getCodeReview(changeId),
    enabled: Boolean(changeId),
    refetchInterval: (genRunning || reviewRunning || fixRunning) ? 5000 : false,
  });
  const { data: genFiles } = useQuery({
    queryKey: ['code-files', changeId],
    queryFn: () => getGeneratedFiles(changeId),
    enabled: Boolean(changeId),
    refetchInterval: (genRunning || fixRunning) ? 5000 : false,
  });
  const { data: history } = useQuery({
    queryKey: ['code-review-history', changeId],
    queryFn: () => getCodeReviewHistory(changeId),
    enabled: Boolean(changeId),
    refetchInterval: (reviewRunning || fixRunning) ? 5000 : false,
  });

  // Refresh the report + file list whenever any loop job finishes.
  useEffect(() => {
    if (genJob && genJob.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['code-review', changeId] });
      queryClient.invalidateQueries({ queryKey: ['code-files', changeId] });
    }
  }, [genJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (reviewJob && reviewJob.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['code-review', changeId] });
      queryClient.invalidateQueries({ queryKey: ['code-review-history', changeId] });
    }
  }, [reviewJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (fixJob && fixJob.status !== 'running') {
      queryClient.invalidateQueries({ queryKey: ['code-review', changeId] });
      queryClient.invalidateQueries({ queryKey: ['code-files', changeId] });
      queryClient.invalidateQueries({ queryKey: ['code-review-history', changeId] });
    }
  }, [fixJob?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const planJobLine = (planRunning || planJob?.status === 'error') && (
    <div style={{ marginTop: 10, fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6, color: planJob?.status === 'error' ? T.danger : T.textSecondary }}>
      {planRunning
        ? <><Loader2 size={12} className="pp-spin" /> {planJob.progress || 'running…'}</>
        : <><AlertTriangle size={12} /> last plan run failed: {planJob.error}</>}
    </div>
  );

  const mrJobLine = (mrRunning || mrJob?.status === 'error') && (
    <div style={{ marginTop: 10, fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6, color: mrJob?.status === 'error' ? T.danger : T.textSecondary }}>
      {mrRunning
        ? <><Loader2 size={12} className="pp-spin" /> MR: {mrJob.progress || 'running…'}</>
        : <><AlertTriangle size={12} /> last MR attempt failed: {mrJob.error}</>}
    </div>
  );

  if (!isLoading && !isError && data == null) {
    return (
      <div style={{ ...card }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <Code2 size={24} color={T.primary} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary, marginBottom: 2 }}>
              Implementation plan
            </div>
            <div style={{ fontSize: 12, color: T.textSecondary }}>
              No plan yet. Generate a spec-grounded implementation plan + change skeleton from the design and {t('term.authority')} documents.
            </div>
          </div>
          <Button
            variant="primary"
            icon={Code2}
            loading={planBusy}
            loadingText="Planning…"
            onClick={() => runMutation.mutate()}
          >
            Run code plan
          </Button>
        </div>
        {planJobLine}
      </div>
    );
  }

  if (isLoading) {
    return (
      <div style={{ ...card, display: 'flex', alignItems: 'center', gap: 10, color: T.textMuted, fontSize: 13 }}>
        <Loader2 size={15} className="pp-spin" /> Loading implementation plan…
      </div>
    );
  }

  if (isError) {
    return (
      <div style={{ ...card, background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.22)', display: 'flex', alignItems: 'center', gap: 8, color: T.danger, fontSize: 13 }}>
        <AlertTriangle size={14} /> Could not load the implementation plan.
      </div>
    );
  }

  const report = data.report || {};
  const p = POSTURE_META[report.code_posture] || { label: report.code_posture || 'unknown', colour: T.textMuted };
  const isMock = report?._meta?.mock;
  const grounded = report?._meta?.grounded === true;
  const workItems = Array.isArray(report.work_items) ? report.work_items : [];
  const fileChanges = Array.isArray(report.file_changes) ? report.file_changes : [];

  // ── Review-loop display state (derived server-side; see _review_status) ──
  const rvStatus = review?.status || 'draft';
  const currentIt = review?.current_iteration ?? genFiles?.iteration ?? 0;
  const reviewedIt = review?.reviewed_iteration ?? 0;
  const findingsCount = review?.findings_count ?? 0;
  const fileCount = genFiles?.files?.length ?? 0;
  const stale = currentIt > 0 && reviewedIt > 0 && reviewedIt < currentIt;
  const STATUS_BADGE = {
    draft:        { label: 'Not generated', tone: 'neutral' },
    generated:    { label: stale ? 'Re-review needed' : 'Needs review', tone: 'warning' },
    issues_found: { label: `${findingsCount} issue${findingsCount !== 1 ? 's' : ''}`, tone: 'danger' },
    clean:        { label: 'Reviewed clean', tone: 'success' },
  }[rvStatus] || { label: rvStatus, tone: 'neutral' };

  const loopJobLine = (job, prefix) => (job?.status === 'running' || job?.status === 'error') && (
    <div style={{ marginTop: 8, fontSize: 11.5, display: 'flex', alignItems: 'center', gap: 6, color: job.status === 'error' ? T.danger : T.textSecondary }}>
      {job.status === 'running'
        ? <><Loader2 size={12} className="pp-spin" /> {prefix}: {job.progress || 'running…'}</>
        : <><AlertTriangle size={12} /> {prefix} failed: {job.error}</>}
    </div>
  );

  return (
    <div style={card}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
        <Code2 size={20} color={T.primary} style={{ flexShrink: 0, marginTop: 2 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 14, fontWeight: 700, color: T.textPrimary }}>Implementation plan</span>
            <Badge solid tone={p.tone || 'neutral'}>{p.label}</Badge>
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
            onClick={() => downloadCodeDocx(changeId, `implementation_plan_v${data.version}.docx`)}>
            .docx
          </Button>
          <Button variant="secondary" size="sm" icon={RefreshCw} loading={planBusy} title="Re-run the code agent"
            onClick={() => runMutation.mutate()}>
            Re-run
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={GitPullRequest}
            loading={mrBusy}
            disabled={isMock || !review?.can_open_mr}
            title={review?.can_open_mr
              ? 'Push the reviewed-clean files as a merge request (no auto-merge)'
              : 'Generate the files and pass code + security review before opening an MR'}
            onClick={() => mrMutation.mutate()}
          >
            Open MR
          </Button>
        </div>
      </div>

      {planJobLine}
      {mrJobLine}

      {/* Merge request status — mutation errors are the synchronous 4xx
          preconditions (no plan / no repo); job errors render in mrJobLine. */}
      {mrMutation.isError && (
        <div style={{ marginTop: 10, padding: '8px 12px', background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.25)', borderRadius: 8, fontSize: 11.5, color: T.danger, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>{mrMutation.error?.response?.data?.detail || 'Failed to open merge request.'}</span>
        </div>
      )}
      {mr && mr.status === 'opened' && mr.mr_iid && (
        <div style={{ marginTop: 10, padding: '8px 12px', background: 'rgba(37,99,235,0.05)', border: '1px solid rgba(37,99,235,0.22)', borderRadius: 8, fontSize: 11.5, color: T.textSecondary, display: 'flex', gap: 8, alignItems: 'center' }}>
          <GitPullRequest size={13} color={T.primary} style={{ flexShrink: 0 }} />
          <span>
            Merge request open on <code>{mr.branch}</code> ({mr.files_count} files, plan v{mr.code_report_version}) —{' '}
            <MergeRequestLink projectPath={mr.project_path} iid={mr.mr_iid} />. Review before merging.
          </span>
        </div>
      )}

      {/* Grounding banner — green when repo-grounded, amber when spec-only */}
      {grounded ? (
        <div style={{ marginTop: 12, padding: '8px 12px', background: 'rgba(34,197,94,0.07)', border: '1px solid rgba(34,197,94,0.28)', borderRadius: 8, fontSize: 11.5, color: T.textSecondary, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <Info size={13} color={T.success} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            <strong>Repository-grounded plan</strong> — generated using excerpts retrieved from your indexed codebase.
            File paths and symbols reference your real repository. Use <strong>Open MR</strong> to generate whole files and raise a merge request.
          </span>
        </div>
      ) : (
        <div style={{ marginTop: 12, padding: '8px 12px', background: 'rgba(245,158,11,0.07)', border: '1px solid rgba(245,158,11,0.28)', borderRadius: 8, fontSize: 11.5, color: T.textSecondary, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
          <Info size={13} color={T.warning} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            <strong>Un-grounded plan</strong> — generated from the design + {t('term.authority')} documents only, <em>without</em> your repository.
            File paths are best-guesses (see confidence per file); a developer must refine against the real codebase.
            Index a repository under Settings to get a repository-grounded plan.
          </span>
        </div>
      )}

      {/* Counts */}
      <div style={{ marginTop: 12, fontSize: 12, color: T.textSecondary }}>
        {workItems.length} work item{workItems.length !== 1 ? 's' : ''} · {fileChanges.length} proposed file change{fileChanges.length !== 1 ? 's' : ''}
      </div>

      {/* File changes with confidence */}
      {fileChanges.length > 0 && (
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {fileChanges.map((f, i) => (
            <div key={`${f.path || 'file'}-${i}`} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5 }}>
              <span style={{ fontSize: 10, fontWeight: 700, color: '#fff', background: f.action === 'add' ? T.success : T.primary, padding: '1px 5px', borderRadius: 4, textTransform: 'uppercase' }}>
                {f.action || 'modify'}
              </span>
              <code style={{ color: T.textPrimary, background: T.bgSoft, padding: '1px 6px', borderRadius: 4, fontFamily: 'ui-monospace, monospace' }}>{f.path}</code>
              {f.confidence && (
                <span style={{ fontSize: 10, color: CONF_COLOUR[f.confidence] || T.textMuted }}>{f.confidence} conf.</span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Plan body */}
      <button
        onClick={() => setExpanded((v) => !v)}
        style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 6, background: 'none', border: 'none', cursor: 'pointer', color: T.textSecondary, fontSize: 12.5, fontWeight: 600, padding: 0 }}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Implementation plan
      </button>
      {expanded && (
        <div className="pp-markdown" style={{ marginTop: 10, paddingTop: 12, borderTop: `1px solid ${T.borderSubtle}`, fontSize: 13, color: T.textPrimary, lineHeight: 1.6 }}>
          <ReactMarkdown>{report.plan_markdown || '_No plan body._'}</ReactMarkdown>
        </div>
      )}

      {/* ── Code review + security review loop ───────────────────────────── */}
      <div style={{ marginTop: 16, paddingTop: 14, borderTop: `1px solid ${T.borderSubtle}` }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <ShieldCheck size={16} color={T.primary} />
          <span style={{ fontSize: 13, fontWeight: 700, color: T.textPrimary }}>Code & security review</span>
          <Badge solid tone={STATUS_BADGE.tone}>{STATUS_BADGE.label}</Badge>
          {currentIt > 0 && (
            <span style={{ fontSize: 11, color: T.textMuted, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <FileCode2 size={11} /> {fileCount} file{fileCount !== 1 ? 's' : ''} · iteration {currentIt}
            </span>
          )}
        </div>

        <div style={{ fontSize: 12, color: T.textSecondary, marginTop: 6 }}>
          Generate the change files, then run code-quality + security review.
          <strong> Auto-fix loops fix → review until zero findings</strong> (capped); the merge
          request stays locked until both reviews pass on the current files.
        </div>

        {isMock ? (
          <div style={{ marginTop: 10, fontSize: 11.5, color: T.warning, display: 'flex', alignItems: 'center', gap: 6 }}>
            <AlertTriangle size={13} /> Needs an LLM key — generation + review are unavailable in mock mode.
          </div>
        ) : (
          <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button
              variant={currentIt === 0 ? 'primary' : 'secondary'} size="sm" icon={Sparkles}
              loading={genRunning || genMutation.isPending} loadingText="Generating…"
              disabled={loopBusy}
              title="Generate whole files from the plan (a new iteration)"
              onClick={() => genMutation.mutate()}
            >
              {currentIt === 0 ? 'Generate files' : 'Re-generate'}
            </Button>
            <Button
              variant={rvStatus === 'generated' ? 'primary' : 'secondary'} size="sm" icon={ShieldCheck}
              loading={reviewRunning || reviewMutation.isPending} loadingText="Reviewing…"
              disabled={loopBusy || currentIt === 0}
              title="Run code-quality + security review over the current files"
              onClick={() => reviewMutation.mutate()}
            >
              Run review
            </Button>
            {rvStatus === 'issues_found' && (
              <Button
                variant="primary" size="sm" icon={Wrench}
                loading={fixRunning || fixMutation.isPending} loadingText="Auto-fixing…"
                disabled={loopBusy}
                title="Fix all findings, re-review, and repeat automatically until zero issues (capped)"
                onClick={() => fixMutation.mutate()}
              >
                Auto-fix until clean
              </Button>
            )}
          </div>
        )}

        {loopJobLine(genJob, 'Generate')}
        {loopJobLine(reviewJob, 'Review')}
        {loopJobLine(fixJob, 'Fix')}

        {[genMutation, reviewMutation, fixMutation].map((m, i) => m.isError && (
          <div key={i} style={{ marginTop: 8, padding: '8px 12px', background: 'rgba(239,68,68,0.06)', border: '1px solid rgba(239,68,68,0.25)', borderRadius: 8, fontSize: 11.5, color: T.danger, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
            <AlertTriangle size={13} style={{ flexShrink: 0, marginTop: 1 }} />
            <span>{m.error?.response?.data?.detail || 'Action failed.'}</span>
          </div>
        ))}

        {stale && rvStatus === 'generated' && (
          <div style={{ marginTop: 10, fontSize: 11.5, color: T.warning, display: 'flex', alignItems: 'center', gap: 6 }}>
            <AlertTriangle size={13} /> Files changed since the last review — re-run review before opening an MR.
          </div>
        )}

        {rvStatus === 'clean' && (
          <div style={{ marginTop: 10, padding: '8px 12px', background: 'rgba(16,185,129,0.07)', border: '1px solid rgba(16,185,129,0.28)', borderRadius: 8, fontSize: 11.5, color: T.textSecondary, display: 'flex', gap: 8, alignItems: 'center' }}>
            <CheckCircle2 size={14} color={T.success} style={{ flexShrink: 0 }} />
            <span><strong>Both reviews passed</strong> with no findings — ready to open the merge request.</span>
          </div>
        )}

        {/* Findings, grouped by reviewer.
            The design-alignment lens is advisory and deliberately carries an
            EMPTY `findings` array (a non-empty one would block the MR), so it
            gets its own branch — the generic renderer would otherwise show a
            meaningless "0 findings / No issues" row and hide the deviations. */}
        {(review?.reviews || []).map((rv) => (rv.reviewer === 'design_alignment' ? (
          <div key={rv.reviewer} style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: T.textPrimary, display: 'flex', alignItems: 'center', gap: 6 }}>
              {REVIEWER_LABEL[rv.reviewer]}
              <span style={{ fontSize: 10.5, fontWeight: 600, color: rv.aligned === false ? T.warning || T.danger : T.success }}>
                {rv.aligned === false
                  ? `${(rv.deviations || []).length} deviation${(rv.deviations || []).length !== 1 ? 's' : ''}`
                  : 'aligned'}
              </span>
            </div>
            <div style={{ fontSize: 11.5, color: T.textMuted, marginTop: 3 }}>{rv.summary}</div>
            {(rv.deviations || []).length > 0 && (
              <ul style={{ margin: '6px 0 0 18px', padding: 0, fontSize: 11.5, color: T.textSecondary, lineHeight: 1.6 }}>
                {(rv.deviations || []).map((d, i) => (
                  <li key={i}>{typeof d === 'string' ? d : (d.detail || d.title || JSON.stringify(d))}</li>
                ))}
              </ul>
            )}
            <div style={{ fontSize: 10.5, color: T.textMuted, marginTop: 5, fontStyle: 'italic' }}>
              Advisory only — does not block the merge request.
            </div>
          </div>
        ) : (
          <div key={rv.reviewer} style={{ marginTop: 12 }}>
            <div style={{ fontSize: 12, fontWeight: 700, color: T.textPrimary, display: 'flex', alignItems: 'center', gap: 6 }}>
              {REVIEWER_LABEL[rv.reviewer] || rv.reviewer}
              <span style={{ fontSize: 10.5, fontWeight: 600, color: (rv.findings || []).length ? T.danger : T.success }}>
                {(rv.findings || []).length} finding{(rv.findings || []).length !== 1 ? 's' : ''}
              </span>
            </div>
            {(rv.findings || []).length === 0 ? (
              <div style={{ fontSize: 11.5, color: T.textMuted, marginTop: 3 }}>No issues.</div>
            ) : (
              <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
                {(rv.findings || []).map((f, i) => (
                  <div key={i} style={{ padding: '8px 10px', background: T.bgSoft, borderRadius: 8, border: `1px solid ${T.borderSubtle}` }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ fontSize: 9.5, fontWeight: 800, color: '#fff', background: SEV_COLOUR[f.severity] || T.textMuted, padding: '1px 6px', borderRadius: 4, textTransform: 'uppercase' }}>
                        {f.severity}
                      </span>
                      <span style={{ fontSize: 12, fontWeight: 600, color: T.textPrimary }}>{f.title}</span>
                      {f.file && (
                        <code style={{ fontSize: 10.5, color: T.textSecondary, background: '#fff', padding: '1px 5px', borderRadius: 4, fontFamily: 'ui-monospace, monospace' }}>
                          {f.file}{f.line ? `:${f.line}` : ''}
                        </code>
                      )}
                    </div>
                    {f.detail && <div style={{ fontSize: 11.5, color: T.textSecondary, marginTop: 4, lineHeight: 1.5 }}>{f.detail}</div>}
                    {f.suggested_fix && (
                      <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}><strong>Fix:</strong> {f.suggested_fix}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )))}

        {/* Fix history — what was fixed per iteration (code-quality + security + lint) */}
        {(() => {
          const iterations = history?.iterations || [];
          const LENSES = ['code_quality', 'security', 'lint'];
          const totalFixed = iterations.reduce(
            (n, it) => n + LENSES.reduce((m, lens) => m + (it.fixed?.[lens]?.length || 0), 0), 0);
          if (iterations.length <= 1 && totalFixed === 0) return null;
          return (
            <div style={{ marginTop: 14, paddingTop: 12, borderTop: `1px solid ${T.borderSubtle}` }}>
              <button
                onClick={() => setShowHistory((v) => !v)}
                style={{ display: 'flex', alignItems: 'center', gap: 6, background: 'none', border: 'none', cursor: 'pointer', color: T.textSecondary, fontSize: 12.5, fontWeight: 600, padding: 0 }}
              >
                {showHistory ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                Fix history — {totalFixed} issue{totalFixed !== 1 ? 's' : ''} fixed across {iterations.length} iteration{iterations.length !== 1 ? 's' : ''}
              </button>
              {showHistory && (
                <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 12 }}>
                  {iterations.map((it) => {
                    const byLens = LENSES.map((lens) => [lens, it.fixed?.[lens] || []]);
                    const fixedCount = byLens.reduce((n, [, items]) => n + items.length, 0);
                    const openCount = LENSES.reduce((n, lens) => n + (it.reviews?.[lens]?.length || 0), 0);
                    return (
                      <div key={it.iteration}>
                        <div style={{ fontSize: 12, fontWeight: 700, color: T.textPrimary }}>
                          Iteration {it.iteration}
                          {fixedCount > 0 ? (
                            <span style={{ fontSize: 11, fontWeight: 600, color: T.success, marginLeft: 6 }}>
                              fixed {byLens.map(([lens, items]) => `${items.length} ${REVIEWER_LABEL[lens] || lens}`).join(' · ')}
                            </span>
                          ) : openCount === 0 ? (
                            <span style={{ fontSize: 11, fontWeight: 600, color: T.success, marginLeft: 6 }}>clean — 0 findings</span>
                          ) : (
                            <span style={{ fontSize: 11, color: T.textMuted, marginLeft: 6 }}>no fixes recorded</span>
                          )}
                        </div>
                        {byLens.map(([lens, items]) => items.length > 0 && (
                          <div key={lens} style={{ marginTop: 5 }}>
                            <div style={{ fontSize: 10.5, fontWeight: 700, color: T.textMuted, textTransform: 'uppercase', letterSpacing: 0.3 }}>
                              {REVIEWER_LABEL[lens]} fixes
                            </div>
                            {items.map((f, i) => (
                              <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginTop: 3, fontSize: 11.5, color: T.textSecondary }}>
                                <CheckCircle2 size={12} color={T.success} style={{ flexShrink: 0, marginTop: 1 }} />
                                <span>
                                  <span style={{ fontSize: 9.5, fontWeight: 800, color: SEV_COLOUR[f.severity] || T.textMuted, textTransform: 'uppercase', marginRight: 6 }}>{f.severity}</span>
                                  {f.title}
                                  {f.file && <code style={{ fontSize: 10, color: T.textMuted, marginLeft: 6 }}>{f.file}{f.line ? `:${f.line}` : ''}</code>}
                                </span>
                              </div>
                            ))}
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })()}
      </div>

      <div style={{ marginTop: 14, fontSize: 11, color: T.textMuted, display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {data.model_used && <span>model: {data.model_used}</span>}
        {report?._meta?.profile_version && <span>profile: {report._meta.profile_version}</span>}
        <span>generated: {formatDateTime(data.generated_at)}</span>
      </div>
    </div>
  );
}
