// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Operator-supplied branding — see the authority frontend's `src/brand.js` for the
// full rationale. Short version: the MIT License conveys no trademark rights
// (it says nothing about them at all — see TRADEMARKS.md), and this app
// previously bundled a third party's bank logo, which we have no right to
// sublicense to forks. Set VITE_BRAND_LOGO_URL to a path served by the
// frontend (e.g. `/brand-logo.png` in `public/`); with it unset the wordmark
// renders as text alone.
export const BRAND_LOGO_URL = import.meta.env.VITE_BRAND_LOGO_URL || ''
export const BRAND_NAME = import.meta.env.VITE_BRAND_NAME || 'Partner Platform'
