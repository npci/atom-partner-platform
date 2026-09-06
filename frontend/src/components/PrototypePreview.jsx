// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

import { useEffect, useRef } from 'react';
import { hardenFrameHtml } from '../utils/safeHtmlFrame';
import { withBasePath } from '../utils/basePath';

// Prototype preview frame.
//
// ── WHY THIS IS NOT JUST <iframe srcDoc={html}> ─────────────────────────────
//
// It used to be. The app now ships a strict CSP (`script-src 'self'`, no
// 'unsafe-inline' — see frontend/scripts/generate-csp.mjs), and a document
// loaded from a `srcDoc` string has no origin of its own, so it INHERITS the
// embedding page's policy. Verified in Chrome: under the app policy, the
// inline scripts inside a srcDoc prototype were blocked and the preview
// rendered as a dead mockup. A `blob:` URL behaves identically — also verified,
// after initially assuming it would not.
//
// The prototypes require inline script (onclick="go('screen-2')" handlers and
// setTimeout blocks drive the walkthrough), so the fix is to give them a
// document with its own policy: /preview-shell.html, a static page nginx serves
// with a separate, egress-free CSP. Markup is handed to it over postMessage.
// See public/preview-shell.html for the full threat model.
//
// The security posture is unchanged from the srcDoc version and is enforced in
// three independent places: `sandbox="allow-scripts"` without
// `allow-same-origin` (opaque origin — no parent DOM, cookies or session
// token), the shell's own `default-src 'none'; connect-src 'none'` policy (no
// network egress), and hardenFrameHtml()'s injected meta CSP, which is retained
// as defence in depth so the markup stays caged even if it is ever rendered
// somewhere without the shell's header.
export default function PrototypePreview({ html, title, style }) {
  const frameRef = useRef(null);

  // Keep the markup in a ref as well as sending it: the shell announces
  // readiness asynchronously, and the handler must post whatever is current at
  // that moment rather than the value captured when the listener was attached.
  const htmlRef = useRef(html);
  htmlRef.current = html;

  useEffect(() => {
    function onMessage(event) {
      const frame = frameRef.current;
      if (!frame || event.source !== frame.contentWindow) return;
      if (!event.data || event.data.type !== 'preview-shell-ready') return;

      // targetOrigin '*': the shell is sandboxed WITHOUT allow-same-origin, so
      // it is in an opaque origin and would reject any concrete origin string
      // (including our own). The payload is the prototype markup — content the
      // frame is about to render anyway — so there is nothing here to leak.
      frame.contentWindow.postMessage(
        { type: 'render', html: hardenFrameHtml(htmlRef.current) },
        '*',
      );
    }

    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, []);

  // Remount on content change. The shell renders once (see its `rendered`
  // latch), so switching documents or versions needs a fresh frame rather than
  // a second render message.
  return (
    <iframe
      key={html}
      ref={frameRef}
      title={title}
      src={withBasePath('preview-shell.html')}
      sandbox="allow-scripts"
      style={style}
    />
  );
}
