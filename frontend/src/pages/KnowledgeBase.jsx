// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// KnowledgeBase — admin page to manage the partner knowledge base (Document
// RAG). Uploaded docs are chunked + embedded and retrieved by the design/code/
// test agents as cross-change prior knowledge.
import { useEffect, useState } from 'react';
import { t } from '../strings'

import { createKnowledgeDoc, deleteKnowledgeDoc, getKnowledgeDocs } from '../services/api';
import { formatDateTime } from '../lib/datetime';

export default function KnowledgeBase() {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({ title: '', source: '', content: '' });
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  async function load() {
    try {
      setDocs(await getKnowledgeDocs());
    } catch (err) {
      console.error('Failed to load knowledge base', err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreate(e) {
    e.preventDefault();
    setError('');
    if (!form.title.trim() || !form.content.trim()) {
      setError('Title and content are required.');
      return;
    }
    setSaving(true);
    try {
      await createKnowledgeDoc(form);
      setForm({ title: '', source: '', content: '' });
      await load();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to add document.');
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(id, title) {
    if (!window.confirm(`Delete "${title}" from the knowledge base? Its indexed chunks are removed too.`)) return;
    try {
      await deleteKnowledgeDoc(id);
      await load();
    } catch (err) {
      setError(err.response?.data?.detail || 'Failed to delete document.');
    }
  }

  return (
    <div style={{ maxWidth: 920, margin: '0 auto', padding: '24px 16px' }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, marginBottom: 4 }}>Knowledge Base</h1>
      <p style={{ color: 'var(--text-secondary)', fontSize: 13, marginBottom: 20 }}>
        Specs, {t('term.authority')} circulars, past change kits, and internal standards. These are indexed and
        retrieved by the Design / Code / Test agents as cross-change prior knowledge.
      </p>

      {error && (
        <div style={{ background: 'var(--danger-soft)', border: '1px solid var(--danger)', color: 'var(--danger)', borderRadius: 'var(--radius-sm)', padding: '8px 12px', fontSize: 13, marginBottom: 16 }}>
          {error}
        </div>
      )}

      {/* Add form */}
      <form onSubmit={handleCreate} style={{ background: 'var(--surface-card)', border: '1px solid var(--border)', borderRadius: 12, padding: 16, marginBottom: 24 }}>
        <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 10 }}>Add a document</div>
        <div style={{ display: 'flex', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
          <input
            placeholder={t('ph.kb.title')}
            value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })}
            style={inputStyle}
          />
          <input
            placeholder="Source (optional — filename / URL)"
            value={form.source}
            onChange={(e) => setForm({ ...form, source: e.target.value })}
            style={inputStyle}
          />
        </div>
        <textarea
          placeholder="Paste the document content (markdown or plain text)…"
          value={form.content}
          onChange={(e) => setForm({ ...form, content: e.target.value })}
          rows={8}
          style={{ ...inputStyle, width: '100%', fontFamily: 'ui-monospace, monospace', resize: 'vertical' }}
        />
        <div style={{ marginTop: 10 }}>
          <button type="submit" disabled={saving} className="btn btn-primary btn-sm">
            {saving ? 'Indexing…' : 'Add + index'}
          </button>
        </div>
      </form>

      {/* List */}
      <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 10 }}>
        Documents {docs.length > 0 && <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>({docs.length})</span>}
      </div>
      {loading ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>Loading…</div>
      ) : docs.length === 0 ? (
        <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>No documents yet. Add one above to seed the knowledge base.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {docs.map((d) => (
            <div key={d.id} style={{ background: 'var(--surface-card)', border: '1px solid var(--border)', borderRadius: 10, padding: '10px 14px', display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13.5, color: 'var(--text-strong)' }}>{d.title}</div>
                <div style={{ fontSize: 11.5, color: 'var(--text-muted)', marginTop: 2 }}>
                  {d.source ? `${d.source} · ` : ''}{d.chunk_count} chunk{d.chunk_count !== 1 ? 's' : ''} · {formatDateTime(d.created_at)}
                </div>
              </div>
              <button onClick={() => handleDelete(d.id, d.title)} className="btn btn-outline btn-sm">Delete</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

const inputStyle = {
  flex: 1, minWidth: 220, padding: '8px 10px', fontSize: 13,
  border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
  background: 'var(--surface-sunken)', color: 'var(--text-default)', outline: 'none',
};
