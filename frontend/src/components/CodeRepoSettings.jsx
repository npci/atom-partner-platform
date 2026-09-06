// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Settings panel for the Code RAG (Phase 3.3): register the partner's own GitLab
// repositories, set a write-only access token, trigger a full re-index, and watch
// status. The indexed source is what makes the code agent repository-grounded
// (Phase 3.2). Backend: app/api/dashboard/code_repo.py.
import { useState } from 'react';
import { t } from '../strings'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { GitBranch, RefreshCw, Trash2, Plus, KeyRound } from 'lucide-react';
import { formatDateTime } from '../lib/datetime';
import {
  getCodeRepos, createCodeRepo, deleteCodeRepo, setGitlabToken, indexCodeRepo,
} from '../services/api';

const STATUS_META = {
  idle: { label: 'Idle', bg: 'rgba(148,163,184,0.16)', fg: 'var(--text-muted)' },
  indexing: { label: 'Indexing…', bg: 'rgba(245,158,11,0.16)', fg: 'var(--warning, #f59e0b)' },
  indexed: { label: 'Indexed', bg: 'rgba(76,175,125,0.16)', fg: 'var(--success, #4caf7d)' },
  error: { label: 'Error', bg: 'rgba(224,108,108,0.14)', fg: 'var(--danger, #e06c6c)' },
};

export default function CodeRepoSettings({ onToast }) {
  const qc = useQueryClient();
  const [token, setToken] = useState('');
  const [form, setForm] = useState({ label: '', gitlab_repo: '', gitlab_branch: 'main', gitlab_url: '', languages: '' });

  const { data, isLoading } = useQuery({
    queryKey: ['code-repos'],
    queryFn: getCodeRepos,
    // Poll while any repo is mid-index so status + counts update live.
    refetchInterval: (q) => (q.state.data?.repos?.some((r) => r.status === 'indexing') ? 2500 : false),
  });

  const tokenSet = !!data?.token_set;
  const repos = data?.repos || [];

  function refresh() { qc.invalidateQueries({ queryKey: ['code-repos'] }); }

  const tokenMut = useMutation({
    mutationFn: () => setGitlabToken(token.trim()),
    onSuccess: () => { onToast?.('GitLab token saved'); setToken(''); refresh(); },
    onError: () => onToast?.('Failed to save token', 'error'),
  });

  const createMut = useMutation({
    mutationFn: () => createCodeRepo({
      label: form.label.trim(),
      gitlab_repo: form.gitlab_repo.trim(),
      gitlab_branch: (form.gitlab_branch || 'main').trim(),
      gitlab_url: form.gitlab_url.trim() || null,
      languages: form.languages.trim() || null,
    }),
    onSuccess: () => {
      onToast?.('Repository registered');
      setForm({ label: '', gitlab_repo: '', gitlab_branch: 'main', gitlab_url: '', languages: '' });
      refresh();
    },
    onError: () => onToast?.('Failed to register repository', 'error'),
  });

  const indexMut = useMutation({
    mutationFn: (id) => indexCodeRepo(id),
    onSuccess: () => { onToast?.('Indexing started'); refresh(); },
    onError: (err) => onToast?.(err?.response?.data?.detail || 'Failed to start indexing', 'error'),
  });

  const deleteMut = useMutation({
    mutationFn: (id) => deleteCodeRepo(id),
    onSuccess: () => { onToast?.('Repository removed'); refresh(); },
    onError: () => onToast?.('Failed to remove repository', 'error'),
  });

  const sectionHeaderStyle = { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 };
  const sectionTitleStyle = { margin: 0, fontSize: 15, fontWeight: 600 };
  const sectionSubtitleStyle = { fontSize: 12, color: 'var(--text-muted)', marginBottom: 16 };
  const helpTextStyle = { fontSize: 11, color: 'var(--text-muted)', marginTop: 4 };

  const canRegister = form.label.trim() && form.gitlab_repo.trim();

  return (
    <section className="card" style={{ padding: 20, marginBottom: 16 }}>
      <div style={sectionHeaderStyle}>
        <GitBranch size={16} />
        <h3 style={sectionTitleStyle}>Code Repository (Code RAG)</h3>
        <span style={{
          fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 600,
          letterSpacing: 0.4, textTransform: 'uppercase',
          background: tokenSet ? 'rgba(76,175,125,0.14)' : 'rgba(224,108,108,0.12)',
          color: tokenSet ? 'var(--success, #4caf7d)' : 'var(--danger, #e06c6c)',
        }}>
          {tokenSet ? 'Token set' : 'No token'}
        </span>
      </div>
      <div style={sectionSubtitleStyle}>
        Register your GitLab repositories and index them so the code agent grounds its
        implementation plans in your real source (paths, classes, methods) instead of best-guess skeletons.
      </div>

      {/* GitLab token (write-only) */}
      <div className="input-group">
        <label style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <KeyRound size={13} /> GitLab Access Token
        </label>
        <div style={{ display: 'flex', gap: 8 }}>
          <input
            className="input-field"
            type="password"
            placeholder={tokenSet ? 'Token configured — enter new token to rotate' : 'glpat-… (api scope)'}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            className="btn btn-outline"
            onClick={() => tokenMut.mutate()}
            disabled={!token.trim() || tokenMut.isPending}
          >
            {tokenMut.isPending ? 'Saving…' : 'Save Token'}
          </button>
        </div>
        <div style={helpTextStyle}>
          Stored write-only on the server and never returned. Needs <code>api</code> scope — indexing reads the tree + files, and "Open MR" creates a branch, commit and merge request. (<code>read_api</code> alone is enough only if you never push merge requests.)
        </div>
      </div>

      {/* Registered repos */}
      <div style={{ marginTop: 18 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Registered repositories</div>
        {isLoading ? (
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Loading…</div>
        ) : repos.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>None yet — register one below.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {repos.map((r) => {
              const st = STATUS_META[r.status] || STATUS_META.idle;
              return (
                <div key={r.id} style={{
                  border: '1px solid var(--card-border, rgba(148,163,184,0.2))', borderRadius: 8,
                  padding: '10px 12px', display: 'flex', alignItems: 'center', gap: 12,
                }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <span style={{ fontWeight: 600, fontSize: 13 }}>{r.label}</span>
                      <span style={{
                        fontSize: 10, padding: '1px 7px', borderRadius: 999, fontWeight: 600,
                        textTransform: 'uppercase', letterSpacing: 0.3, background: st.bg, color: st.fg,
                      }}>
                        {st.label}
                      </span>
                    </div>
                    <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 2, fontFamily: 'ui-monospace, monospace' }}>
                      {r.gitlab_repo} · {r.gitlab_branch}
                      {r.languages ? ` · ${r.languages}` : ''}
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                      {r.status === 'indexed' && (
                        <>{r.files_count ?? 0} files · {r.chunks_count ?? 0} chunks
                          {r.last_indexed_at ? ` · ${formatDateTime(r.last_indexed_at)}` : ''}
                          {r.last_sha ? ` · ${r.last_sha.slice(0, 8)}` : ''}
                        </>
                      )}
                      {r.status === 'error' && r.last_error && (
                        <span style={{ color: 'var(--danger, #e06c6c)' }}>{r.last_error}</span>
                      )}
                    </div>
                  </div>
                  <button
                    className="btn btn-outline"
                    onClick={() => indexMut.mutate(r.id)}
                    disabled={!tokenSet || r.status === 'indexing' || indexMut.isPending}
                    title={!tokenSet ? 'Set a GitLab token first' : 'Full re-index'}
                  >
                    <RefreshCw size={13} className={r.status === 'indexing' ? 'spin' : undefined} />
                    {r.status === 'indexing' ? 'Indexing…' : 'Index'}
                  </button>
                  <button
                    className="btn btn-outline"
                    onClick={() => { if (window.confirm(`Remove "${r.label}" and its indexed chunks?`)) deleteMut.mutate(r.id); }}
                    disabled={deleteMut.isPending}
                    title="Remove repository + its indexed chunks"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Register a repo */}
      <div style={{ marginTop: 18, borderTop: '1px solid var(--card-border, rgba(148,163,184,0.2))', paddingTop: 16 }}>
        <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8 }}>Register a repository</div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <div className="input-group" style={{ margin: 0 }}>
            <label>Label</label>
            <input className="input-field" placeholder={t('ph.repo.name')} value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </div>
          <div className="input-group" style={{ margin: 0 }}>
            <label>GitLab project (group/project)</label>
            <input className="input-field" placeholder="my-bank/upi-switch" value={form.gitlab_repo}
              onChange={(e) => setForm({ ...form, gitlab_repo: e.target.value })} />
          </div>
          <div className="input-group" style={{ margin: 0 }}>
            <label>Branch</label>
            <input className="input-field" placeholder="main" value={form.gitlab_branch}
              onChange={(e) => setForm({ ...form, gitlab_branch: e.target.value })} />
          </div>
          <div className="input-group" style={{ margin: 0 }}>
            <label>GitLab URL (optional)</label>
            <input className="input-field" placeholder="https://gitlab.com (blank = default)" value={form.gitlab_url}
              onChange={(e) => setForm({ ...form, gitlab_url: e.target.value })} />
          </div>
          <div className="input-group" style={{ margin: 0, gridColumn: '1 / -1' }}>
            <label>Languages (optional, csv)</label>
            <input className="input-field" placeholder="java,python  (blank = all known)" value={form.languages}
              onChange={(e) => setForm({ ...form, languages: e.target.value })} />
            <div style={helpTextStyle}>Limits which file types are indexed. Known: java, python, typescript, javascript, go, kotlin, ruby, csharp.</div>
          </div>
        </div>
        <div style={{ marginTop: 12 }}>
          <button className="btn btn-primary" onClick={() => createMut.mutate()} disabled={!canRegister || createMut.isPending}>
            <Plus size={14} />
            {createMut.isPending ? 'Registering…' : 'Register Repository'}
          </button>
        </div>
      </div>
    </section>
  );
}
