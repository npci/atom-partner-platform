---
partner: <YOUR ORG NAME>
profile_version: 0.1
last_updated: <YYYY-MM-DD>
maintained_by: <YOUR TEAM>
---

# Partner Profile — <YOUR ORG NAME>

> Fill this in with your organisation's authoritative engineering brief. It is
> loaded as context by the feasibility agent when evaluating any incoming NPCI
> change_communication, so every statement here is treated as load-bearing fact.
> A complete worked example is in `data/examples/hdfc_profile.md`.
>
> To use: copy this file to `data/partner_profile.md` (the path the platform
> mounts), or point `PARTNER_PROFILE_PATH` at your own file, then replace every
> `<...>` placeholder below. The section structure (§1–§11) is what the
> feasibility prompt references — keep the headings, replace the contents.

## Quick reference

- **UPI roles played:** <Payer PSP / Payee PSP / Remitter / Beneficiary / Sponsor / ...>
- **Core banking:** <CBS vendor(s) and any boundary that matters>
- **UPI switch:** <switch vendor + change-window lead time>
- **Mobile apps / channels:** <apps and surfaces you operate>
- **Per-txn caps:** <your caps by use case>
- **Daily / count cap:** <e.g. ₹X or N txns per 24h>
- **Freeze windows:** <e.g. FY-end, quarter-end, audit windows>
- **Regulatory weight:** <e.g. D-SIB; RBI directions in scope>

## 1. Identity and roles in UPI

<Who you are as an NPCI member, and which UPI roles you play (Payer PSP, Payee PSP,
Remitter, Beneficiary, Sponsor, International acquirer). Note your customer footprint.>

## 2. Tech stack

<Core banking (retail + treasury), UPI switch, mobile apps, middleware, UPI schema
version, data-centre / localisation posture. Call out vendor boundaries that add lead time.>

## 3. API and integration patterns

<Inbound flow from NPCI, idempotency, retry semantics, timeout posture, reconciliation.>

## 4. Channels

<List the channels/surfaces you operate and which are live UPI debit surfaces.>

## 5. Vendor map

<Each vendor (switch, CBS, KYC, AML, fraud, settlement, cloud) + its change-window / SLA.>

## 6. Operational envelope

<Per-txn caps table, fees, latency targets, throughput headroom, maintenance windows.>

## 7. Implementation patterns

<Release cadence, phased vs big-bang, microservice vs core extension, cert engagement style,
change-window discipline.>

## 8. Known constraints

<The load-bearing engineering quirks the analyser must treat as definitive: vendor
boundaries, switch lead times, freeze windows, product-specific limitations, etc.>

## 9. Recent UPI rollouts

<Table of features you've shipped, their status, and delivery posture.>

## 10. Regulatory posture

<RBI directions in scope, recent penalties, data localisation, audit cycle, ombudsman.>

## 11. Org capabilities

<Engineering org, delivery velocity track record, release-management discipline, freeze windows.>
