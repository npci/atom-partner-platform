// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Badge — status/label chip. Composes the token-based .badge classes in
// index.css. Pairs an optional icon with the label so status is never
// conveyed by colour alone (accessibility). Replaces the four hand-rolled
// copies of pill logic across the panels. See DESIGN_SYSTEM.md.
//
// Props:
//   tone    — 'accent' | 'success' | 'warning' | 'danger' | 'neutral'
//             (default 'neutral')
//   solid   — filled chip (white text on the solid tone) instead of soft tint
//   icon    — optional leading Lucide icon component
//   …rest   — title, style, etc. pass through
const ICON_SIZE = 11;

export default function Badge({
  tone = 'neutral',
  solid = false,
  icon: Icon,
  className = '',
  children,
  ...rest
}) {
  // Solid only exists for accent/success/warning/danger; neutral has no solid.
  const toneClass = solid && tone !== 'neutral'
    ? `badge-solid-${tone}`
    : `badge-${tone}`;
  const classes = ['badge', toneClass, className].filter(Boolean).join(' ');

  return (
    <span className={classes} {...rest}>
      {Icon && <Icon size={ICON_SIZE} />}
      {children}
    </span>
  );
}
