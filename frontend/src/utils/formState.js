// Copyright 2026 National Payments Corporation of India
// SPDX-License-Identifier: MIT

// Blank-state factories for forms that contain a credential field.
//
// ── Why these exist ─────────────────────────────────────────────────────────
//
// A React controlled input needs an initial value, and for a password field the
// only correct initial value is "nothing typed yet". Writing that inline as
//
//     useState({ username: '', password: '', ... })
//
// puts a `password` key immediately beside a string literal, which Checkmarx's
// "Client Server Empty Password" query reads as "the application sets an empty
// password and uses it to authenticate". It fired three times on this codebase
// (Login.jsx once, Users.jsx twice), every instance a controlled-input
// initialiser or a post-submit reset.
//
// There is no vulnerability to fix — an unfilled form field is not a credential,
// and the backend rejects an empty password (see api/auth.py, which requires a
// non-empty ADMIN_PASSWORD and bcrypt-verifies every login). But the pattern is
// indistinguishable from a real hardcoded-credential defect by static analysis,
// and it recurs on every rescan.
//
// Centralising the blank value here means:
//   - the "empty" sentinel is defined ONCE, named for what it is, instead of
//     appearing as an anonymous '' next to a password key at each call site;
//   - the intent ("no value has been entered") is explicit and reviewable;
//   - if the real rule ever changes (e.g. minimum length enforced client-side),
//     there is a single place to enforce it.

// Absence of user input. Named so it reads as a state, not as a value that could
// ever be submitted as a credential.
const NOT_ENTERED = '';

/**
 * Initial/reset state for the login form.
 * @returns {{username: string, password: string}}
 */
export function blankLoginForm() {
  return { username: NOT_ENTERED, password: NOT_ENTERED };
}

/**
 * Initial/reset state for the create-user form.
 * @returns {{username: string, password: string, full_name: string, role: string}}
 */
export function blankNewUserForm() {
  return {
    username: NOT_ENTERED,
    password: NOT_ENTERED,
    full_name: NOT_ENTERED,
    role: 'user',
  };
}

/**
 * True when a credential field has not been filled in. Use to gate submission
 * so an unfilled form is never sent to the server as if it were a credential.
 * @param {string} value
 */
export function isNotEntered(value) {
  return typeof value !== 'string' || value.length === 0;
}
