// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

// ── Keep security commentary out of the shipped HTML ────────────────────────
//
// Two HTML files here carry long explanatory comments. index.html documents the
// three-layer clickjacking defence, why there is deliberately no CSP <meta>
// tag, and the reasoning behind each decision. public/preview-
// shell.html documents the sandbox arrangement for untrusted prototype markup:
// the CSP inheritance rule it works around, and the three controls that cage
// whatever renders inside it.
//
// Those comments are for whoever edits the files next. They are not for the
// public. Vite copies both through verbatim, so without this plugin every
// visitor could read the app's security architecture and the
// exact mitigations in place — a map for anyone probing it, and its own
// weakness ("Information Exposure Through an HTML Comment"). Fixing
// the permissive-CSP problem by writing a thorough comment would otherwise
// just trade one weakness for another.
//
// Stripping is build-only, so `npm run dev` keeps the comments visible while
// working. It cannot disturb the CSP hashes either: it only removes text
// between comment markers, never the bodies of the inline <script> and <style>
// that generate-csp.mjs hashes. frontend/test/cspPolicy.test.mjs asserts both
// halves of that against the real build output — comments gone, and the
// antiClickjack style plus frame buster still present.
const stripComments = (html) => html
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\n{3,}/g, '\n\n');

function stripHtmlComments() {
  return {
    name: 'strip-html-comments',
    apply: 'build',
    // index.html goes through the HTML pipeline.
    transformIndexHtml: {
      order: 'post',
      handler: stripComments,
    },
    // Files in public/ do not: they are copied byte-for-byte, so they have to
    // be rewritten in the output directory once the copy has happened.
    closeBundle() {
      const outDir = join(import.meta.dirname, 'dist');
      let entries;
      try {
        entries = readdirSync(outDir);
      } catch {
        return;
      }
      for (const name of entries) {
        if (!name.endsWith('.html') || name === 'index.html') continue;
        const file = join(outDir, name);
        const original = readFileSync(file, 'utf8');
        const stripped = stripComments(original);
        if (stripped !== original) writeFileSync(file, stripped);
      }
    },
  };
}

// Where the dev server proxies API traffic. Defaults to the native backend on
// 8001; override when the backend you want is the DOCKERISED one, which compose
// publishes on 18001 — `BACKEND_ORIGIN=http://localhost:18001 npm run dev`.
const BACKEND_ORIGIN = process.env.BACKEND_ORIGIN || 'http://localhost:8001'

// Keep the browser's Host rather than rewriting it to the target. The backend's
// CSRF origin check accepts the request's OWN origin (derived from Host), so
// preserving it makes the dev server same-origin as far as that check goes.
// Rewriting it leaves the browser sending `Origin: localhost:<devport>` against
// a Host of the backend, and every state-changing request 403s
// "Cross-origin request rejected" while GETs sail through — the app looks fine
// until the first POST. Production is unaffected: there the SPA and API share
// one edge origin.
const changeOrigin = false

export default defineConfig({
  base: './',
  plugins: [react(), stripHtmlComments()],
  server: {
    port: Number(process.env.PORT) || 3001,
    // Dev-server mirror of deploy/edge.nginx.conf. The app is served under the
    // context path `/a2a-partner/` (start vite with --base=/a2a-partner/), and
    // `utils/basePath.js` therefore builds every API call as
    // `/a2a-partner/api/...` — so the dev proxy must strip that prefix exactly
    // like the edge's `rewrite ^/a2a-partner/(.*)$ /$1`.
    //
    // The previous single `/a2a` key could not work: vite proxy keys are PREFIX
    // matches, so `/a2a` swallowed `/a2a-partner/` itself — every request for
    // the app's own HTML and assets was forwarded to the backend, which has no
    // such route, so the dev server answered 404 at the only path the router
    // will render. Served at `/` instead, the router refuses to render at all
    // (basename mismatch), which presents as a blank white page with no
    // network error to point at.
    //
    // Ordering matters: `/a2a-partner/api` must precede the broader keys, and
    // `/a2a-rpc` is named exactly rather than as `/a2a` so it cannot shadow the
    // UI path again.
    proxy: {
      '/a2a-partner/api': {
        target: BACKEND_ORIGIN,
        changeOrigin,
        rewrite: (p) => p.replace(/^\/a2a-partner/, ''),
      },
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin,
      },
      '/a2a-rpc': {
        target: BACKEND_ORIGIN,
        changeOrigin,
      },
      '/.well-known': {
        target: BACKEND_ORIGIN,
        changeOrigin,
      },
    },
  },
});
