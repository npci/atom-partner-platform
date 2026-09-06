// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import axios from 'axios';
import { t } from '../strings'
import { API_BASE, LOGIN_PATH } from '../utils/basePath';

// The session JWT lives in an httpOnly cookie (set by the backend on login),
// so `withCredentials` ships it automatically and there is no Bearer token to
// read from JS. Same-origin under nginx, so the cookie is sent on every call.
const api = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
});

// 401 interceptor — verify the session is actually dead before bouncing the
// user. Previously a single 401 on any POST/PUT/PATCH/DELETE would instantly
// redirect, which cost the operator their workflow context on any transient
// backend hiccup (rebuild race, etc). Now we probe `/auth/me` via the
// no-redirect silentApi instance — only if THAT also 401s do we treat the
// session as truly dead. The cookie is cleared server-side once it expires;
// a full-page nav to /login remounts the app and re-runs the /auth/me probe.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const status = error.response?.status;
    const detail = error.response?.data?.detail || '';
    const method = (error.config?.method || 'get').toLowerCase();
    const isAuthFailure = status === 401 || (status === 403 && detail === 'Not authenticated');
    const isBackground  = error.config?._skipAuthRedirect || method === 'get';
    if (isAuthFailure && !isBackground) {
      let sessionIsDead = true;
      try {
        await silentApi.get('/auth/me');
        sessionIsDead = false; // probe succeeded — original 401 was transient
      } catch (_probeErr) { /* probe also failed — session is gone */ }
      if (sessionIsDead && window.location.pathname !== LOGIN_PATH) {
        window.location.href = LOGIN_PATH;
      }
    }
    return Promise.reject(error);
  }
);

// Silent API — for background/polling calls that must never redirect on 401
export const silentApi = axios.create({
  baseURL: API_BASE,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
});
silentApi.interceptors.response.use((r) => r, (err) => Promise.reject(err));

// ── Document download — auth'd blob fetch ───────────────────────────────────

// Synthesise a file download from in-memory bytes. Callers fetch through the
// api instance (which carries the session cookie + sets the right Accept) and
// hand the bytes here so we control the filename via the `download` attribute.
function triggerBlobDownload(data, filename, mimeType) {
  const url = window.URL.createObjectURL(
    new Blob([data], mimeType ? { type: mimeType } : undefined),
  );
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  // Click a *detached* anchor — modern browsers fire click() without it being
  // in the DOM, so there's no appendChild of a dynamic node.
  a.click();
  // Free the object URL on the next tick — doing it before the click handler
  // completes can cancel the download (IE/Safari quirks aside).
  setTimeout(() => window.URL.revokeObjectURL(url), 0);
}

export async function downloadChangeDocument(changeId, docId, filename) {
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/download`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'document.docx');
}


// Promo/explainer MP4 — fetch as a blob (auth'd) so a <video> can play it.
// Returns the Blob; the caller owns the Object URL lifecycle.
export async function fetchChangeDocumentVideo(changeId, docId) {
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/video`,
    { responseType: 'blob' },
  );
  return resp.data;
}

export async function downloadChangeDocumentVideo(changeId, docId, filename) {
  const blob = await fetchChangeDocumentVideo(changeId, docId);
  triggerBlobDownload(blob, filename || 'video.mp4', 'video/mp4');
}

export async function downloadChangeSummary(changeId, filename) {
  // the authority's "Summary of Changes" for this kit version, as a .docx.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/change-summary.docx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'change_summary.docx');
}

export async function downloadChangeDocumentPptx(changeId, docId, filename) {
  // D7 — Product Deck companion. Same auth'd-blob-fetch pattern as
  // the docx helper above; differs only in the URL suffix and the
  // default filename extension.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/download/pptx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'presentation.pptx');
}

export async function downloadChangeDocumentXlsx(changeId, docId, filename) {
  // cert_test_cases workbook. Same auth'd-blob-fetch pattern as the
  // docx/pptx helpers; differs only in URL suffix and default extension.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/download/xlsx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'testcases.xlsx');
}

export async function downloadChangeDocumentZip(changeId, docId, filename) {
  // Multi-schema XSD bundle (.zip). Same auth'd-blob-fetch pattern as the
  // docx/pptx/xlsx helpers; differs only in URL suffix and default extension.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/download/zip`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'schemas.zip');
}

export async function downloadChangeDocumentNative(changeId, docId, filename) {
  // Native single-format artifacts: manifest -> .yaml, xsd -> .xsd,
  // prototype_screens -> .html. The backend extracts the raw fenced block
  // from the doc's markdown content. Same auth'd-blob-fetch pattern.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/documents/${encodeURIComponent(docId)}/download/native`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'artifact');
}


export async function downloadCertSignoff(changeId, filename) {
  // the authority Certification Result certificate shipped on the all-PASS
  // cert_completion_signoff A2A task. Same auth'd-blob-fetch pattern;
  // keyed by change (one sign-off per change), not by document id.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/signoff/download`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || `${t('cert.filePrefix')}_Result.docx`);
}


export async function downloadCertSignoffPdf(changeId, filename) {
  // On-demand PDF certificate rendered server-side by the partner backend
  // from cert_summary + profile — always available once cert_status is
  // 'certified', doesn't require the A2A signoff docx to have arrived.
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/cert-signoff.pdf`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || `${t('cert.filePrefix')}_Signoff.pdf`);
}


// ── Auth ────────────────────────────────────────────────────────────────────

export async function login(username, password) {
  const { data } = await api.post('/auth/login', { username, password });
  return data;
}

export async function getMe() {
  const { data } = await api.get('/auth/me');
  return data;
}

export async function logout() {
  // Clears the httpOnly session cookie server-side and revokes the token.
  try { await api.post('/auth/logout'); } catch { /* best-effort */ }
}

export async function changePassword(currentPassword, newPassword) {
  const { data } = await api.post('/auth/change-password', {
    current_password: currentPassword,
    new_password:     newPassword,
  });
  return data;
}

// ── Users ───────────────────────────────────────────────────────────────────

export async function listUsers() {
  const { data } = await api.get('/users');
  return data;
}

export async function createUser(user) {
  const { data } = await api.post('/users', user);
  return data;
}

export async function updateUser(id, updates) {
  const { data } = await api.put(`/users/${id}`, updates);
  return data;
}

export async function deactivateUser(id) {
  const { data } = await api.delete(`/users/${id}`);
  return data;
}

// ── Changes ─────────────────────────────────────────────────────────────────

export async function listChanges() {
  const { data } = await api.get('/changes');
  return data;
}

export async function getChange(id, version) {
  const { data } = await api.get(`/changes/${id}`, {
    params: version != null ? { version } : undefined,
  });
  return data;
}

export async function getChangeVersions(id) {
  const { data } = await api.get(`/changes/${id}/versions`);
  return data;
}

export async function submitQuery(changeId, message) {
  const { data } = await api.post(`/changes/${changeId}/query`, { message });
  return data;
}

// Cert-channel messaging — strictly separate from general Phase C
// queries (different A2A task_type, different authority thread inbox, no
// shared rows on either side).
export async function submitCertQuery(changeId, message) {
  const { data } = await api.post(`/changes/${changeId}/cert-query`, { message });
  return data;
}

export async function listCertQueries(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/cert-queries`);
  return data;
}

// Cert lifecycle status — 4-step linear flow:
// received → deployed → tested → ready_for_certification.
// Wires through A2A task_type=cert_status_update; the authority flips the
// matching ChangePartnerAssignment.status enum.
export async function getCertStatus(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/cert-status`);
  return data;
}

// The partner's own per-case certification evidence (full-trace
// observability): what the application was asked, what it answered (raw
// payloads, capped), and how the round's contract graded it.
export async function listCertExecutions(npciChangeId) {
  const { data } = await api.get('/integration-testing/cert-executions', {
    params: { npci_change_id: npciChangeId },
  });
  return data;
}

export async function updateCertStatus(changeId, body) {
  // body: { status, role?, test_data? }
  const { data } = await api.post(`/changes/${changeId}/cert-status`, body);
  return data;
}

// Demo-mode: partner UI posts a synthesised UPI cert result set here when
// the authority's orchestrator isn't publishing results back on this environment.
// Backend persists cert_summary + cert_status='certified' and best-effort
// mirrors the run to the authority over A2A (cert_test_response).
export async function certMockComplete(changeId, cert_summary) {
  const { data } = await api.post(`/changes/${changeId}/cert-mock-complete`, { cert_summary });
  return data;
}

// ── Decision: Accept / Counter-propose (rollout-contract) ──────────────────

export async function acceptChange(changeId, body = {}) {
  const { data } = await api.post(`/changes/${changeId}/accept`, body);
  return data;
}

export async function counterPropose(changeId, justification, requestCategory, requestPayload) {
  const body = { justification }
  if (requestCategory) body.request_category = requestCategory
  if (requestPayload) body.request_payload = requestPayload
  const { data } = await api.post(`/changes/${changeId}/counter`, body);
  return data;
}

export async function acceptNegotiationVersion(changeId) {
  const { data } = await api.post(`/changes/${changeId}/accept-version`);
  return data;
}

// Accept just the open authority counter (resolves the negotiation thread).
// Does NOT accept the rollout — that still requires acceptChange().
export async function acceptCounter(changeId, counterProposalId) {
  const { data } = await api.post(
    `/changes/${changeId}/counter-proposals/${counterProposalId}/accept`,
  );
  return data;
}

export async function reportBlocker(changeId, body) {
  const { data } = await api.post(`/changes/${changeId}/blocker`, body);
  return data;
}

// Post-freeze break-glass channel — only meaningful once the change is frozen
// (negotiation_version >= 3). body: { title, description, severity }.
export async function raiseEmergencyIssue(changeId, body) {
  const { data } = await api.post(`/changes/${changeId}/emergency-issue`, body);
  return data;
}

// ── Auto-suggested query drafts ─────────────────────────────────────────────

// Background — use silentApi so a 403 on suggest never logs the user out
export async function suggestQueryDrafts(changeId) {
  const { data } = await silentApi.post(`/changes/${changeId}/queries/suggest`);
  return data;
}

export async function listQueryDrafts(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/query-drafts`);
  return data;
}

export async function updateQueryDraft(draftId, text) {
  const { data } = await api.patch(`/query-drafts/${draftId}`, { text });
  return data;
}

export async function discardQueryDraft(draftId) {
  const { data } = await api.delete(`/query-drafts/${draftId}`);
  return data;
}

export async function sendQueryDraft(draftId) {
  const { data } = await api.post(`/query-drafts/${draftId}/send`);
  return data;
}

export async function reportProgress(changeId, step) {
  const { data } = await api.post(`/changes/${changeId}/progress`, { step });
  return data;
}

export async function declareReady(changeId, body = {}) {
  // Body shape: { role: 'PAYER_PSP' | 'PAYEE_PSP' | 'REMITTER_BANK' |
  // 'BENEFICIARY_BANK', test_data: { payer_vpa, payee_vpa, … } }.
  // Empty body is back-compat with the legacy declare-ready button —
  // server falls back to {status: ready_for_cert} on the wire.
  const { data } = await api.post(`/changes/${changeId}/ready`, body);
  return data;
}

// ── Settings ────────────────────────────────────────────────────────────────

export async function getSettings() {
  const { data } = await api.get('/settings');
  return data;
}

export async function updateSettings(settings) {
  const { data } = await api.put('/settings', settings);
  return data;
}

export async function testConnection(url, apiKey) {
  const { data } = await api.post('/settings/test-connection', { url, api_key: apiKey });
  return data;
}

// ── Partner profile (PARTNER.md) ─────────────────────────────────────────────

// The active capability profile the feasibility analyser runs against. DB-backed
// and UI-editable; the editor pre-populates from the mounted seed file when no
// row exists yet (exists=false, source='file').
export async function getProfile() {
  const { data } = await api.get('/profile');
  return data;
}

export async function updateProfile(profile) {
  const { data } = await api.put('/profile', profile);
  return data;
}

// ── Feasibility analyser ────────────────────────────────────────────────────

// Latest persisted report for this change. 404 → no analysis has run yet,
// surface as null so the UI can render the "run analyser" CTA.
export async function getFeasibilityReport(changeId) {
  try {
    const { data } = await silentApi.get(`/feasibility/report/${changeId}`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// Force a new analyser run. Persists a new version row server-side and
// returns the full report. Use to refresh after the partner edits their
// PARTNER.md or after the authority updates the change documents.
export async function runFeasibilityAnalysis(changeId) {
  const { data } = await api.post(`/feasibility/analyse/${changeId}`);
  return data;
}

// Wiring sanity check — used by Settings/health UI to confirm PARTNER.md
// is mounted into the container at the expected path.
export async function getProfileStatus() {
  const { data } = await silentApi.get('/feasibility/profile/status');
  return data;
}

// ── Design agent ────────────────────────────────────────────────────────────

// Latest persisted design document for this change. 404 → not run yet → null,
// so the UI renders the "run design" CTA.
export async function getDesignReport(changeId) {
  try {
    const { data } = await silentApi.get(`/changes/${changeId}/design/report`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// Run the design agent — persists a new version row server-side and returns it.
export async function runDesignAnalysis(changeId) {
  const { data } = await api.post(`/changes/${changeId}/design/analyse`);
  return data;
}

// Download the latest design document as .docx (auth'd blob fetch — same
// pattern as downloadChangeDocument).
export async function downloadDesignDocx(changeId, filename) {
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/design/document.docx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'design.docx');
}

// ── Test agent ──────────────────────────────────────────────────────────────

// Latest persisted test plan. 404 → not run yet → null (UI shows the CTA).
export async function getTestReport(changeId) {
  try {
    const { data } = await silentApi.get(`/changes/${changeId}/test/report`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// Run the test agent — persists a new version row server-side and returns it.
export async function runTestAnalysis(changeId) {
  const { data } = await api.post(`/changes/${changeId}/test/analyse`);
  return data;
}

// Download the latest test plan as .docx (auth'd blob fetch).
export async function downloadTestDocx(changeId, filename) {
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/test/plan.docx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'test_plan.docx');
}

// ── Code agent (MVP — spec-grounded, no codebase) ───────────────────────────

// Latest persisted implementation plan. 404 → not run yet → null.
export async function getCodeReport(changeId) {
  try {
    const { data } = await silentApi.get(`/changes/${changeId}/code/report`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// Run the code agent — persists a new version row server-side and returns it.
export async function runCodeAnalysis(changeId) {
  const { data } = await api.post(`/changes/${changeId}/code/analyse`);
  return data;
}

// Download the latest implementation plan as .docx (auth'd blob fetch).
export async function downloadCodeDocx(changeId, filename) {
  const resp = await api.get(
    `/changes/${encodeURIComponent(changeId)}/code/plan.docx`,
    { responseType: 'blob' },
  );
  triggerBlobDownload(resp.data, filename || 'implementation_plan.docx');
}

// Latest merge request opened for this change (Code RAG 3.4). 404 → null.
export async function getCodeMergeRequest(changeId) {
  try {
    const { data } = await silentApi.get(`/changes/${changeId}/code/merge-request`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// Generate the whole-file change set from the latest plan and store it as a new
// iteration (a reviewable artifact) — does NOT open an MR. Returns 202 + job id;
// poll getAgentJob(id,'codegen'), then getGeneratedFiles.
export async function generateCode(changeId) {
  const { data } = await api.post(`/changes/${changeId}/code/generate`);
  return data;
}

// List the current (latest) iteration's generated files (path, action, size).
export async function getGeneratedFiles(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/code/files`);
  return data;
}

// Run code-quality + security review over the current generated iteration.
// Returns 202 + job id; poll getAgentJob(id,'review'), then getCodeReview.
export async function runCodeReview(changeId) {
  const { data } = await api.post(`/changes/${changeId}/code/review`);
  return data;
}

// Merged review findings of the latest reviewed iteration + loop status
// (current_iteration, reviewed_iteration, status, findings_count, can_open_mr).
export async function getCodeReview(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/code/review/report`);
  return data;
}

// Per-iteration review timeline: findings at each iteration + which were fixed
// by the following round. Drives the "fixed issues per iteration" view.
export async function getCodeReviewHistory(changeId) {
  const { data } = await silentApi.get(`/changes/${changeId}/code/review/history`);
  return data;
}

// Apply the latest review findings to the code (re-gen → new iteration).
// Only valid when status == issues_found. Returns 202 + job id; poll
// getAgentJob(id,'fix'), then re-run review.
export async function applyCodeFix(changeId) {
  const { data } = await api.post(`/changes/${changeId}/code/fix`);
  return data;
}

// Generate whole-file contents from the latest plan and open an MR (no merge).
export async function openCodeMergeRequest(changeId) {
  const { data } = await api.post(`/changes/${changeId}/code/merge-request`);
  return data;
}

// ── Agent jobs (202 + poll pattern) ─────────────────────────────────────────
// The long agent POSTs (design/code/test analyse, Open MR) return 202 with a
// job id; panels poll the latest job for (change, kind) to drive button state.
// kind: 'design' | 'code' | 'testing' | 'mr'. 404 → never run → null.
export async function getAgentJob(changeId, kind) {
  try {
    const { data } = await silentApi.get(`/changes/${changeId}/jobs/${kind}/latest`);
    return data;
  } catch (err) {
    if (err?.response?.status === 404) return null;
    throw err;
  }
}

// ── Knowledge base (Document RAG) ───────────────────────────────────────────

export async function getKnowledgeDocs() {
  const { data } = await api.get('/knowledge');
  return data;
}

export async function createKnowledgeDoc({ title, content, source }) {
  const { data } = await api.post('/knowledge', { title, content, source: source || null });
  return data;
}

export async function deleteKnowledgeDoc(id) {
  const { data } = await api.delete(`/knowledge/${id}`);
  return data;
}

// ── Code repositories (Code RAG — Settings panel) ───────────────────────────

// List registered repos + their index status, plus whether a GitLab token is set.
export async function getCodeRepos() {
  const { data } = await api.get('/code-repo');
  return data;  // { token_set, repos: [...] }
}

export async function createCodeRepo(body) {
  const { data } = await api.post('/code-repo', body);
  return data;
}

export async function deleteCodeRepo(id) {
  const { data } = await api.delete(`/code-repo/${id}`);
  return data;
}

// Set the GitLab access token (write-only — stored server-side, never returned).
export async function setGitlabToken(token) {
  const { data } = await api.put('/code-repo/token', { token });
  return data;
}

// Kick a background full re-index. Returns immediately (202); poll getCodeRepos.
export async function indexCodeRepo(id) {
  const { data } = await api.post(`/code-repo/${id}/index`);
  return data;
}

export default api;
