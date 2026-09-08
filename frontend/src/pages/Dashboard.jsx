// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useQuery } from '@tanstack/react-query';
import { t } from '../strings'
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BadgeCheck, ChevronRight, Clock, FileText, Inbox, RefreshCw, Search, Sparkles,
} from 'lucide-react';
import { listChanges } from '../services/api';
import StatTile, { StatTileRow } from '../components/common/StatTile';
import Badge from '../components/ui/Badge';
import { formatRelative as relativeTime } from '../lib/datetime';

// Per-status presentation: badge tone + row icon + tinted icon background.
const STATUS_META = {
  new:         { label: 'New',         tone: 'accent',  icon: Sparkles,   fg: 'var(--accent)',  bg: 'var(--accent-soft)' },
  in_progress: { label: 'In progress', tone: 'warning', icon: Clock,      fg: 'var(--warning)', bg: 'var(--warning-soft)' },
  ready:       { label: 'Ready',       tone: 'success', icon: FileText,   fg: 'var(--success)', bg: 'var(--success-soft)' },
  certified:   { label: 'Certified',   tone: 'success', icon: BadgeCheck, fg: 'var(--success)', bg: 'var(--success-soft)' },
};

function metaFor(status) {
  return STATUS_META[status] || STATUS_META.new;
}

const FILTERS = [
  { key: 'all',         label: 'All' },
  { key: 'new',         label: 'New' },
  { key: 'in_progress', label: 'In progress' },
  { key: 'certified',   label: 'Certified' },
];

export default function Dashboard() {
  const navigate = useNavigate();
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');

  const { data: changes, isLoading, isFetching, error } = useQuery({
    queryKey: ['changes'],
    queryFn: listChanges,
    // Inbox poll — a new product kit arrives server-to-server (A2A
    // CHANGE_COMMUNICATION) without any UI action, so poll to surface it
    // without a manual browser refresh.
    refetchInterval: 8000,
    retry: (failureCount, err) => {
      const s = err?.response?.status;
      if (s === 401 || s === 403) return false;
      return failureCount < 1;
    },
  });

  const status   = error?.response?.status;
  const authDead = status === 401 || status === 403;
  const list = useMemo(() => changes || [], [changes]);

  const statusOf = (c) => c.status || 'new';
  const isCertified = (c) => c.status === 'certified' || c.cert_status === 'certified';

  const counts = {
    all:         list.length,
    new:         list.filter((c) => statusOf(c) === 'new').length,
    in_progress: list.filter((c) => c.status === 'in_progress').length,
    certified:   list.filter(isCertified).length,
  };

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return list.filter((c) => {
      const matchesFilter =
        filter === 'all' ? true
        : filter === 'certified' ? isCertified(c)
        : statusOf(c) === filter;
      const matchesSearch = !q || (c.title || '').toLowerCase().includes(q);
      return matchesFilter && matchesSearch;
    });
  }, [list, filter, search]);

  return (
    <div className="page">
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 'var(--sp-4)', marginBottom: 'var(--sp-5)' }}>
        <h1 className="page-title" style={{ marginBottom: 0 }}>Incoming Changes</h1>
        {!error && (
          <span className="poll-dot">
            {isFetching ? <><RefreshCw size={12} className="pp-spin" /> Refreshing…</> : 'Live · auto-refresh'}
          </span>
        )}
      </div>

      <StatTileRow>
        <StatTile label="Total"       value={counts.all}         accent="var(--text-strong)" onClick={() => setFilter('all')} />
        <StatTile label="New"         value={counts.new}         accent="var(--accent)"  onClick={() => setFilter('new')}
                  hint={counts.new ? 'awaiting decision' : null} />
        <StatTile label="In Progress" value={counts.in_progress} accent="var(--warning)" onClick={() => setFilter('in_progress')} />
        <StatTile label="Certified"   value={counts.certified}   accent="var(--success)" onClick={() => setFilter('certified')} />
      </StatTileRow>

      {/* Toolbar — search + status filter */}
      <div className="toolbar">
        <div className="search-box">
          <Search size={16} />
          <input
            type="text"
            placeholder="Search changes…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search changes"
          />
        </div>
        <div className="filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`filter-chip ${filter === f.key ? 'active' : ''}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
              <span className="chip-count">{counts[f.key]}</span>
            </button>
          ))}
        </div>
      </div>

      {isLoading && <div className="loading">Loading changes…</div>}

      {error && (
        <div className="card">
          {authDead ? (
            <>
              <p style={{ color: 'var(--danger)', marginTop: 0 }}>
                Your session has expired. Please sign in again to continue.
              </p>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => {
                  navigate('/login', { replace: true });
                }}
              >
                Go to login
              </button>
            </>
          ) : (
            <p style={{ color: 'var(--danger)' }}>
              Failed to load changes. The backend returned
              {' '}<code>{status || 'a network error'}</code>.
              Make sure the partner backend is running and try again.
            </p>
          )}
        </div>
      )}

      {/* Empty — nothing received at all */}
      {!isLoading && !error && list.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">
            <Inbox size={48} strokeWidth={1.2} />
          </div>
          <h3>No changes received yet</h3>
          <p>When {t('term.authority')} publishes a change, it will appear here automatically.</p>
        </div>
      )}

      {/* No matches for the current filter/search */}
      {!isLoading && !error && list.length > 0 && filtered.length === 0 && (
        <div className="empty-state">
          <div className="empty-icon">
            <Search size={44} strokeWidth={1.2} />
          </div>
          <h3>No matching changes</h3>
          <p>Try a different search term or filter.</p>
        </div>
      )}

      {/* List */}
      {!error && filtered.map((change) => {
        const m = metaFor(isCertified(change) ? 'certified' : statusOf(change));
        const Icon = m.icon;
        return (
          <div
            key={change.id}
            className="change-row"
            onClick={() => navigate(`/changes/${change.id}`)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); navigate(`/changes/${change.id}`); } }}
          >
            <div className="change-row-icon" style={{ background: m.bg, color: m.fg }}>
              <Icon size={19} />
            </div>
            <div className="change-row-body">
              <div className="change-row-title">{change.title}</div>
              <div className="change-row-meta">
                Received {relativeTime(change.received_at || change.created_at)}
              </div>
            </div>
            <Badge tone={m.tone} icon={m.icon}>{m.label}</Badge>
            <ChevronRight size={18} style={{ color: 'var(--text-muted)', flexShrink: 0 }} />
          </div>
        );
      })}
    </div>
  );
}
