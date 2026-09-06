// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useState, useEffect, useRef } from 'react';
import { t } from '../strings'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Eye, EyeOff, Wifi, Save, Shield, Lock, Sparkles, GitBranch, FileText, Upload } from 'lucide-react';
import { getSettings, updateSettings, testConnection, getProfile, updateProfile } from '../services/api';
import CodeRepoSettings from '../components/CodeRepoSettings';
import { formatDateTime } from '../lib/datetime';

// Flat `key: value` frontmatter reader — mirrors the backend's parser so an
// uploaded .md's `partner` / `profile_version` populate the structured fields.
// Frontmatter is a short header — `partner` and `profile_version` are the only
// keys read. The content comes from an operator-uploaded .md whose size this
// function does not control, so the line count is untrusted input. The loop
// counts against this build-time constant and uses the tainted length only as a
// `break` guard, keeping any unchecked value out of the loop condition.
const MAX_FRONTMATTER_LINES = 200;

function parseFrontmatter(raw) {
  if (!raw.startsWith('---')) return {};
  const end = raw.indexOf('\n---', 3);
  if (end < 0) return {};
  const out = {};
  const lines = raw.slice(3, end).trim().split('\n');
  for (let n = 0; n < MAX_FRONTMATTER_LINES; n += 1) {
    if (n >= lines.length) break;
    const line = lines[n];
    const i = line.indexOf(':');
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

export default function Settings() {
  const qc = useQueryClient();
  // Which settings section is shown — replaces the long single-scroll stack.
  const [section, setSection] = useState('connection');
  const [npciUrl, setNpciUrl] = useState('');
  // Direct service URL used by the A2A SDK for partner -> authority outbound
  // (distinct from npciUrl, which the UI uses for the test-connection
  // button). Defaults to the docker service name when blank; operators
  // running on a host need to set this explicitly.
  const [npciA2aUrl, setNpciA2aUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [partnerName, setPartnerName] = useState('');
  const [anthropicKey, setAnthropicKey] = useState('');
  // Slice 3 / 5 of A2A security hardening — the two long-lived the authority
  // secrets that gate inbound A2A calls. Held in component state in
  // plaintext while the operator is pasting/editing; cleared on save.
  const [npciJwtSecret, setNpciJwtSecret] = useState('');
  const [npciHmacSecret, setNpciHmacSecret] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [showAnthropicKey, setShowAnthropicKey] = useState(false);
  const [showJwtSecret, setShowJwtSecret] = useState(false);
  const [showHmacSecret, setShowHmacSecret] = useState(false);
  const [toast, setToast] = useState(null);

  // Partner Profile (PARTNER.md) — the capability brief the feasibility
  // analyser runs against. Editable markdown body + two structured fields.
  const [profileContent, setProfileContent] = useState('');
  const [profilePartnerName, setProfilePartnerName] = useState('');
  const [profileVersion, setProfileVersion] = useState('');
  // 'edit' once the operator types/uploads; flips to 'upload' on a file import
  // so the saved row records how it got there.
  const [profileSource, setProfileSource] = useState('edit');
  const fileInputRef = useRef(null);

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: getSettings,
  });

  const { data: profile } = useQuery({
    queryKey: ['profile'],
    queryFn: getProfile,
  });

  useEffect(() => {
    if (settings) {
      setNpciUrl(settings.npci_platform_url || '');
      setNpciA2aUrl(settings.npci_a2a_url || '');
      setApiKey('');  // Don't pre-fill masked value — leave empty unless user wants to change
      setPartnerName(settings.partner_name || '');
      setAnthropicKey('');
      setNpciJwtSecret('');
      setNpciHmacSecret('');
    }
  }, [settings]);

  useEffect(() => {
    if (profile) {
      setProfileContent(profile.content || '');
      setProfilePartnerName(profile.partner_name || '');
      setProfileVersion(profile.profile_version || '');
      setProfileSource('edit');
    }
  }, [profile]);

  function showToast(message, type = 'success') {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  }

  const saveMutation = useMutation({
    mutationFn: () => updateSettings({
      npci_platform_url: npciUrl,
      npci_a2a_url: npciA2aUrl,
      partner_api_key: apiKey,
      partner_name: partnerName,
      partner_anthropic_api_key: anthropicKey,
      npci_jwt_secret: npciJwtSecret,
      npci_hmac_secret: npciHmacSecret,
    }),
    onSuccess: () => {
      showToast('Settings saved');
      // Re-pull so the new "configured" badges replace the empty state.
      qc.invalidateQueries({ queryKey: ['settings'] });
      setNpciJwtSecret('');
      setNpciHmacSecret('');
    },
    onError: () => showToast('Failed to save settings', 'error'),
  });

  const profileSaveMutation = useMutation({
    mutationFn: () => updateProfile({
      content: profileContent,
      partner_name: profilePartnerName,
      profile_version: profileVersion,
      source: profileSource,
    }),
    onSuccess: () => {
      showToast('Partner profile saved');
      qc.invalidateQueries({ queryKey: ['profile'] });
    },
    onError: () => showToast('Failed to save partner profile', 'error'),
  });

  function handleProfileUpload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '');
      setProfileContent(text);
      setProfileSource('upload');
      // Best-effort: pull partner / profile_version out of the frontmatter so
      // the structured fields track the uploaded file without manual re-entry.
      const fm = parseFrontmatter(text);
      if (fm.partner) setProfilePartnerName(fm.partner);
      if (fm.profile_version) setProfileVersion(fm.profile_version);
      showToast(`Loaded ${file.name}`);
    };
    reader.onerror = () => showToast('Could not read file', 'error');
    reader.readAsText(file);
    e.target.value = '';  // allow re-uploading the same filename
  }

  const testMutation = useMutation({
    mutationFn: () => testConnection(npciUrl, apiKey),
    onSuccess: (data) => {
      if (data.status === 'ok') {
        showToast(data.message || 'Connection successful');
      } else {
        showToast(data.message || 'Connection failed', 'error');
      }
    },
    onError: () => showToast('Connection failed', 'error'),
  });

  if (isLoading) return <div className="page" style={{ maxWidth: 1100 }}><div className="loading">Loading settings...</div></div>;

  const checks = [
    !!settings?.npci_platform_url,
    !!settings?.has_api_key,
    !!settings?.has_anthropic_api_key,
    !!settings?.has_npci_jwt_secret,
    !!settings?.has_npci_hmac_secret,
  ];
  const configuredCount = checks.filter(Boolean).length;
  const totalCount = checks.length;

  // Section nav model. `ok` drives the status dot; null = managed elsewhere
  // (Code Repository keeps its own state in CodeRepoSettings).
  const connectionOk = !!settings?.npci_platform_url && !!settings?.has_api_key;
  const aiOk = !!settings?.has_anthropic_api_key;
  const securityOk = !!(settings?.has_npci_jwt_secret && settings?.has_npci_hmac_secret);
  const profileOk = !!(profileContent && profileContent.trim());
  const SECTIONS = [
    { id: 'connection', label: `${t('term.authorityCap')} Connection`, icon: Wifi,      ok: connectionOk },
    { id: 'profile',    label: 'Partner Profile', icon: FileText,  ok: profileOk },
    { id: 'ai',         label: 'AI Drafting',     icon: Sparkles,  ok: aiOk },
    { id: 'security',   label: 'A2A Security',    icon: Shield,    ok: securityOk },
    { id: 'repo',       label: 'Code Repository', icon: GitBranch, ok: null },
  ];

  const cardStyle = { padding: 20, marginBottom: 16 };
  const sectionHeaderStyle = { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4 };
  const sectionTitleStyle = { margin: 0, fontSize: 15, fontWeight: 600 };
  const sectionSubtitleStyle = { fontSize: 12, color: 'var(--text-muted)', marginBottom: 16 };
  const labelRowStyle = { display: 'flex', alignItems: 'center', gap: 8 };
  const helpTextStyle = { fontSize: 11, color: 'var(--text-muted)', marginTop: 4 };

  const Badge = ({ ok }) => (
    <span style={{
      fontSize: 10, padding: '2px 8px', borderRadius: 999, fontWeight: 600,
      letterSpacing: 0.4, textTransform: 'uppercase',
      background: ok ? 'rgba(76,175,125,0.14)' : 'rgba(224,108,108,0.12)',
      color: ok ? 'var(--success, #4caf7d)' : 'var(--danger, #e06c6c)',
    }}>
      {ok ? 'Configured' : 'Not set'}
    </span>
  );

  return (
    // Settings is a form page — keep the canvas narrower than the
    // global .page cap so the inputs stay scannable. 1100px is wide
    // enough for two-column field rows but stops short of feeling
    // stretched on ultra-wide displays.
    <div className="page" style={{ maxWidth: 1100 }}>
      <div style={{ marginBottom: 20 }}>
        <h1 className="page-title" style={{ marginBottom: 4 }}>Settings</h1>
        <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>
          {configuredCount === totalCount
            ? `All ${totalCount} required credentials configured.`
            : `${configuredCount} of ${totalCount} required credentials configured.`}
        </div>
      </div>

      <div className="settings-layout">
        {/* Section nav — one section at a time instead of a long scroll. */}
        <nav className="settings-nav">
          {SECTIONS.map(({ id, label, icon: Icon, ok }) => (
            <button
              key={id}
              className={`settings-nav-item ${section === id ? 'active' : ''}`}
              onClick={() => setSection(id)}
            >
              <Icon size={16} />
              {label}
              {ok != null && (
                <span
                  className="nav-dot"
                  title={ok ? 'Configured' : 'Not set'}
                  style={{ background: ok ? 'var(--success)' : 'var(--border-strong)' }}
                />
              )}
            </button>
          ))}
        </nav>

      <div className="settings-content">

        {/* ── the authority Connection ─────────────────────────────────────── */}
        {section === 'connection' && (
        <section className="card" style={cardStyle}>
          <div style={sectionHeaderStyle}>
            <Wifi size={16} />
            <h3 style={sectionTitleStyle}>{t('term.authorityCap')} Connection</h3>
          </div>
          <div style={sectionSubtitleStyle}>
            Where to reach {t('term.authority')}'s platform and how this partner identifies itself on outbound calls.
          </div>

          <div className="input-group">
            <label>{t('term.authorityCap')} Platform URL</label>
            <input
              className="input-field"
              placeholder="https://npci-platform.example.com"
              value={npciUrl}
              onChange={(e) => setNpciUrl(e.target.value)}
            />
          </div>

          <div className="input-group">
            <label>{t('term.authorityCap')} A2A Service URL</label>
            <input
              className="input-field"
              placeholder="http://npci_backend:8000  (host-mode: http://127.0.0.1:8000)"
              value={npciA2aUrl}
              onChange={(e) => setNpciA2aUrl(e.target.value)}
            />
            <div style={helpTextStyle}>
              Direct URL the A2A SDK uses for outbound partner&nbsp;&rarr;&nbsp;{t('term.authority')} calls (card discovery + JSON-RPC). Leave blank to use the docker default <code>http://npci_backend:8000</code>; set explicitly when running on a host (e.g. <code>http://127.0.0.1:8000</code>).
            </div>
          </div>

          <div className="input-group">
            <label style={labelRowStyle}>
              Partner API Key
              <Badge ok={!!settings?.has_api_key} />
            </label>
            <div className="input-with-icon">
              <input
                className="input-field"
                type={showKey ? 'text' : 'password'}
                placeholder={settings?.has_api_key ? 'API key configured — enter new key to change' : 'Enter your API key'}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
              <button className="input-icon" onClick={() => setShowKey(!showKey)}>
                {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          <div className="input-group">
            <label>Partner Name</label>
            <input
              className="input-field"
              placeholder="e.g. SBI, PhonePe, Paytm"
              value={partnerName}
              onChange={(e) => setPartnerName(e.target.value)}
            />
          </div>

          <div style={{ marginTop: 12 }}>
            <button
              className="btn btn-outline"
              onClick={() => testMutation.mutate()}
              disabled={!npciUrl || testMutation.isPending}
            >
              <Wifi size={14} />
              {testMutation.isPending ? 'Testing...' : 'Test Connection'}
            </button>
          </div>
        </section>
        )}

        {/* ── Partner Profile (PARTNER.md) ────────────────────────── */}
        {section === 'profile' && (
        <section className="card" style={cardStyle}>
          <div style={sectionHeaderStyle}>
            <FileText size={16} />
            <h3 style={sectionTitleStyle}>Partner Profile</h3>
            <Badge ok={profileOk} />
          </div>
          <div style={sectionSubtitleStyle}>
            Your bank's capability brief (PARTNER.md). The feasibility analyser and the
            design / code / testing agents read this as context when evaluating an
            incoming {t('term.authority')} change. Upload a <code>.md</code> or edit it inline.
          </div>

          <div style={{ display: 'flex', gap: 12 }}>
            <div className="input-group" style={{ flex: 1 }}>
              <label>Partner Name</label>
              <input
                className="input-field"
                placeholder="e.g. SBI"
                value={profilePartnerName}
                onChange={(e) => { setProfilePartnerName(e.target.value); setProfileSource('edit'); }}
              />
            </div>
            <div className="input-group" style={{ width: 160 }}>
              <label>Profile Version</label>
              <input
                className="input-field"
                placeholder="e.g. 1.0"
                value={profileVersion}
                onChange={(e) => { setProfileVersion(e.target.value); setProfileSource('edit'); }}
              />
            </div>
          </div>

          <div className="input-group">
            <div style={{ ...labelRowStyle, justifyContent: 'space-between' }}>
              <label style={{ margin: 0 }}>Profile (Markdown)</label>
              <button
                className="btn btn-outline btn-sm"
                onClick={() => fileInputRef.current?.click()}
                type="button"
              >
                <Upload size={14} />
                Upload .md
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".md,.markdown,text/markdown"
                style={{ display: 'none' }}
                onChange={handleProfileUpload}
              />
            </div>
            <textarea
              className="input-field"
              style={{ minHeight: 360, fontFamily: 'var(--font-mono, monospace)', fontSize: 12, lineHeight: 1.5, resize: 'vertical' }}
              placeholder={'---\npartner: Your Bank\nprofile_version: 1.0\n---\n\n# Partner Profile\n\n## Quick reference\n- UPI roles played: ...'}
              value={profileContent}
              onChange={(e) => { setProfileContent(e.target.value); setProfileSource('edit'); }}
            />
            <div style={helpTextStyle}>
              {profile?.exists === false && (profile?.content
                ? 'Pre-filled from the mounted seed file — save to make it the active profile.'
                : 'No profile configured yet — paste or upload your bank\'s brief.')}
              {profile?.updated_at && (
                <> Last saved {formatDateTime(profile.updated_at)} · source: {profile.source} · {new Blob([profileContent]).size} bytes</>
              )}
            </div>
          </div>

          <div className="btn-row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              className="btn btn-primary"
              onClick={() => profileSaveMutation.mutate()}
              disabled={profileSaveMutation.isPending || !profileContent.trim()}
            >
              <Save size={14} />
              {profileSaveMutation.isPending ? 'Saving...' : 'Save Profile'}
            </button>
          </div>
        </section>
        )}

        {/* ── AI Drafting ─────────────────────────────────────────── */}
        {section === 'ai' && (
        <section className="card" style={cardStyle}>
          <div style={sectionHeaderStyle}>
            <h3 style={sectionTitleStyle}>AI Drafting</h3>
            <Badge ok={!!settings?.has_anthropic_api_key} />
          </div>
          <div style={sectionSubtitleStyle}>
            Used to auto-generate clarification question drafts from shared documents.
          </div>

          <div className="input-group">
            <label>Anthropic API Key</label>
            <div className="input-with-icon">
              <input
                className="input-field"
                type={showAnthropicKey ? 'text' : 'password'}
                placeholder={settings?.has_anthropic_api_key ? 'Key configured — enter new key to change' : 'sk-ant-...'}
                value={anthropicKey}
                onChange={(e) => setAnthropicKey(e.target.value)}
              />
              <button className="input-icon" onClick={() => setShowAnthropicKey(!showAnthropicKey)}>
                {showAnthropicKey ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>
        </section>
        )}

        {/* ── A2A Security ────────────────────────────────────────── */}
        {section === 'security' && (
        <section className="card" style={cardStyle}>
          <div style={sectionHeaderStyle}>
            <Shield size={16} />
            <h3 style={sectionTitleStyle}>A2A Security</h3>
            <Badge ok={!!(settings?.has_npci_jwt_secret && settings?.has_npci_hmac_secret)} />
          </div>
          <div style={sectionSubtitleStyle}>
            {t('term.authorityCap')}-issued secrets used to validate inbound A2A calls. {t('term.authorityCap')} returns these once at
            partner-create or after rotate — paste them here.
          </div>

          <div className="input-group">
            <label style={labelRowStyle}>
              {t('term.authorityCap')} JWT Signing Secret
              <Badge ok={!!settings?.has_npci_jwt_secret} />
            </label>
            <div className="input-with-icon">
              <input
                className="input-field"
                type={showJwtSecret ? 'text' : 'password'}
                placeholder={
                  settings?.has_npci_jwt_secret
                    ? `Configured — current: ${settings.npci_jwt_secret_masked}. Paste new value to rotate.`
                    : `64-char hex — paste from ${t('term.authority')} rotate-jwt-secret response`
                }
                value={npciJwtSecret}
                onChange={(e) => setNpciJwtSecret(e.target.value)}
              />
              <button className="input-icon" onClick={() => setShowJwtSecret(!showJwtSecret)}>
                {showJwtSecret ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <div style={helpTextStyle}>
              <Lock size={10} style={{ verticalAlign: 'text-bottom', marginRight: 3 }} />
              Validates the Bearer JWT {t('term.authority')} signs on inbound /a2a-rpc/rpc calls.
            </div>
          </div>

          <div className="input-group" style={{ marginTop: 14 }}>
            <label style={labelRowStyle}>
              {t('term.authorityCap')} HMAC Envelope Secret
              <Badge ok={!!settings?.has_npci_hmac_secret} />
            </label>
            <div className="input-with-icon">
              <input
                className="input-field"
                type={showHmacSecret ? 'text' : 'password'}
                placeholder={
                  settings?.has_npci_hmac_secret
                    ? `Configured — current: ${settings.npci_hmac_secret_masked}. Paste new value to rotate.`
                    : `64-char hex — paste from ${t('term.authority')} rotate-hmac-secret response`
                }
                value={npciHmacSecret}
                onChange={(e) => setNpciHmacSecret(e.target.value)}
              />
              <button className="input-icon" onClick={() => setShowHmacSecret(!showHmacSecret)}>
                {showHmacSecret ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <div style={helpTextStyle}>
              <Lock size={10} style={{ verticalAlign: 'text-bottom', marginRight: 3 }} />
              Validates X-{t('term.authority')}-Signature on inbound A2A requests (replay + tamper protection).
            </div>
          </div>
        </section>
        )}

        {/* ── Code Repository (Code RAG) — manages its own save ─────── */}
        {section === 'repo' && <CodeRepoSettings onToast={showToast} />}

        {/* One Save persists the three core credential sections at once; the
            Code Repository and Partner Profile sections each save themselves,
            so the shared button is hidden there. */}
        {section !== 'repo' && section !== 'profile' && (
          <div className="btn-row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
            <button
              className="btn btn-primary"
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending}
            >
              <Save size={14} />
              {saveMutation.isPending ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        )}
      </div>
      </div>

      {toast && (
        <div className={`toast toast-${toast.type}`}>
          {toast.message}
        </div>
      )}
    </div>
  );
}
