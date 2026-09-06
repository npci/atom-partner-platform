---
name: Bug report
about: Something behaves differently from how it is documented
title: ''
labels: bug
assignees: ''
---

<!--
SECURITY: do not report vulnerabilities here. Issues are public.
Follow SECURITY.md instead.
-->

## What happened

<!-- Observed behaviour. Paste exact error text if it is short. -->

## What you expected

## Reproduction

1.
2.
3.

## Environment

| | |
|---|---|
| Commit / tag | |
| Deployment | compose / native / other |
| Authority platform it is pointed at | version or commit, if known |

## If this involves the A2A boundary

<!-- Rejections there are layered, and each layer fails differently. Say which
     one you have ruled out: TLS, Bearer JWT, HMAC envelope, mTLS, CIDR
     allow-list, rate limit. SECURITY.md describes how to read each rejection. -->

- [ ] Checked the Authority-side `a2a_messages` audit row for this exchange
- [ ] Confirmed `backend/app/a2a_common/hmac_signer.py` matches the Authority's copy — the two sides hash the same wire bytes, and a one-sided change surfaces here as a generic auth failure
- [ ] This is a first send, and `Message.task_id` was left empty (setting it asks the remote side to continue a task it has never heard of, and it answers *task does not exist*)

## Logs

<!-- Redact tokens, bank identifiers and personal data before pasting. -->

```
```
