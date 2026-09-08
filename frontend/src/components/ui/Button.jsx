// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Button — the one button primitive for the partner platform.
// Composes the token-based .btn classes in index.css (real :hover/:disabled/
// :focus-visible live there) and layers icon + loading affordances on top.
// Replaces the per-panel inline runBtn/ghostBtn objects and the phantom
// .btn-secondary className. See DESIGN_SYSTEM.md.
//
// Props:
//   variant      — 'primary' | 'secondary' | 'ghost' | 'danger' | 'success'
//   size         — 'sm' | 'md'  (default 'md')
//   icon         — optional leading Lucide icon component (e.g. icon={Download})
//   loading      — when true, shows a spinner and disables the button
//   loadingText  — optional label shown while loading (falls back to children)
//   block        — full-width
//   …rest        — onClick, type, title, disabled, etc. pass straight through
import { Loader2 } from 'lucide-react';

const ICON_SIZE = { sm: 13, md: 14 };

export default function Button({
  variant = 'primary',
  size = 'md',
  icon: Icon,
  loading = false,
  loadingText,
  block = false,
  disabled = false,
  className = '',
  children,
  ...rest
}) {
  const iconSize = ICON_SIZE[size] || ICON_SIZE.md;
  const classes = [
    'btn',
    `btn-${variant}`,
    size === 'sm' ? 'btn-sm' : '',
    block ? 'btn-block' : '',
    className,
  ].filter(Boolean).join(' ');

  return (
    <button
      type={rest.type || 'button'}
      className={classes}
      disabled={disabled || loading}
      {...rest}
    >
      {loading
        ? <Loader2 size={iconSize} className="pp-spin" />
        : Icon && <Icon size={iconSize} />}
      {loading ? (loadingText ?? children) : children}
    </button>
  );
}
