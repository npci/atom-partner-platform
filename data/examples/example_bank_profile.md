---
partner: Meridian Commercial Bank
profile_version: 1.0
last_updated: 2026-05-27
maintained_by: Meridian UPI Platform Team
---

# Partner Profile — Meridian Commercial Bank

> EXAMPLE profile (worked reference) for a FICTIONAL bank. Every name, vendor,
> number and date below is invented to show the expected shape and level of
> detail — none of it describes a real institution. Copy this to
> `data/partner_profile.md` and replace it wholesale with your own facts, or
> point `PARTNER_PROFILE_PATH` at a file of your own.
>
> This is the authoritative engineering brief loaded as context by the
> feasibility analyser when evaluating any incoming change_communication. The
> analyser treats every statement as load-bearing, so an inaccurate profile
> produces confidently wrong feasibility verdicts. Keep it current.

## Quick reference

- **UPI roles played:** Payer PSP · Payee PSP · Remitter Bank · Beneficiary Bank · Sponsor Bank (selective TPAP / PPI) · UPI International acquirer (NRE / NRO).
- **Core banking:** vendor retail core + separate treasury core. UPI changes that touch treasury cross a vendor boundary.
- **UPI switch:** single third-party switch vendor, primary + DR. Single-vendor dependency for switch-level changes; 6-week change-window for schema additions.
- **Mobile apps:** Meridian Mobile (flagship banking app) + Meridian Pay (standalone payments app).
- **Per-txn caps:** ₹1L P2P/P2M · ₹2L mutual funds / credit card / insurance / loans · ₹5L IPO and Retail Direct · ₹5K first 24h post-registration.
- **Daily / count cap:** ₹1L or 20 txns per 24h, whichever first.
- **UPI Lite posture:** ₹5K wallet · ₹1K per-txn · ₹4K daily. Network ceiling, no internal deviation.
- **Credit-card-on-UPI:** Issuer live since mid-2023. Debit-only by network design — inward credit not supported.
- **NRI UPI International:** Live in 10 of 12 approved geographies (two in active rollout).
- **Engineering org:** two delivery factories. 3-week cadence for acquisition journeys; UPI rail changes ride a dedicated monthly release window.
- **Freeze windows:** FY-end soft freeze (last week of March → 1 April). Quarter-end book-close (last 2 business days of every quarter). External IT-audit windows throttle non-essential change.
- **Regulatory weight:** designated systemically important. Master Directions on KYC, Digital Payment Security Controls, and Outsourcing of IT Services in scope.

## 1. Identity and roles in UPI

We are a Scheduled Commercial Bank and a member of the payments network. The regulator has designated us a Domestic Systemically Important Bank.

**UPI roles played:**
- **Payer PSP** — we issue the `@meridian` handle and onboard customers as payers via Meridian Mobile and Meridian Pay.
- **Payee PSP** — used by merchants via our merchant-acceptance suite and UPI Collect products.
- **Remitter Bank** — a top-tier remitter bank by monthly UPI volume.
- **Beneficiary Bank** — accept inbound UPI credits to our accounts.
- **Sponsor Bank** — we sponsor a contracted set of TPAPs and PPIs under bilateral arrangements. Sponsorship is not auto-rolled-out for new flows; each new flow requires explicit re-contracting with the sponsored party.
- **UPI International acquirer** — cross-border scan-and-pay enabled for NRE / NRO customers.

**Customer footprint:** ~9,500 branches and ~21,000 ATMs, with over half of branches in semi-urban and rural areas. >93% of customer engagement is digital. UPI-enabled customer base is at multi-crore scale.

## 2. Tech stack

**Core banking — retail:** vendor retail core on a current vendor-supported major version. Bank-specific customisations live in a separate payments adapter layer; the adapter is what network-driven changes typically touch first. Direct core schema changes are rare and follow the vendor's release channel.

**Core banking — treasury:** a separate treasury core from a different vendor. Treasury operations are isolated from the retail UPI path under normal flows. Any UPI change that bridges treasury (large-value rails, settlement-bank role if assumed, FX legs for UPI International) crosses the retail → treasury boundary and inherits both release cadences. Plan for 2× lead time on cross-boundary changes.

**UPI switch:** third-party switch vendor, deployed in primary + DR, on a quarterly release pipeline. Schema additions and new transaction types require a 6-week change-window negotiation. Emergency patches move faster only with network-level urgency.

**Mobile apps:** Meridian Mobile and Meridian Pay. Both native iOS + Android. Meridian Pay was rebuilt ground-up in 2022-23. Pre-rebuild wallet history is read-only via batch lookup; no online API.

**Middleware:** cloud-native middleware decoupled from the core via event-driven adapters. Open-source primary with vendor-supported hardened paths.

**UPI schema version:** aligned with the network's current production schema covering AutoPay, Lite, credit-card-on-UPI, and IPO mandates. Schema upgrades require the switch upgrade as a precondition.

**Data centres:** dual primary in two domestic regions. All UPI workloads run on locally-resident infrastructure per the applicable data-localisation directive.

## 3. API and integration patterns

**Inbound flow from the network:** network → switch → fraud / risk engine (parallel) → core banking debit → confirmation back to the network. All within the 30-second transaction window.

**Idempotency:** UPI transactions are idempotent on `txnId` at the switch layer. The core adapter de-duplicates on `txnId + RRN` within a 24-hour window.

**Retry semantics:** No silent retries on debit operations. Failed debits surface as terminal network response codes (U-series / Z-series). Reconciliation handles late-credit scenarios through the standard chargeback windows.

**Timeout posture:** 25-second internal timeout on the core debit call (5-second buffer under the 30-second envelope). 8-second timeout on fraud / risk parallel calls. Fraud-call timeout does NOT block the debit path — fraud rules that need to block apply pre-debit.

**Reconciliation:** deferred via T+0 end-of-day network settlement files plus T+1 in-house reconciliation jobs against switch + ledger.

**Credit-card-on-UPI:** credit-card linkage flows trigger a separate event into our credit-card statementing pipeline (event bus → credit-card ledger) for next-cycle billing.

## 4. Channels

- **Meridian Mobile** — primary UPI registration via SMS device-binding + MPIN. Native iOS + Android.
- **Meridian Pay** — TPAP-style standalone app. UPI, digital credit card, bill payments, merchant payments.
- **NetBanking (web)** — UPI history, dispute initiation, IPO mandate flows. Not a live UPI debit surface.
- **Merchant acceptance suite** — for current-account holders. Tap & Pay, UPI QR, UPI Collect, SMS Pay.
- **Payment gateway** — e-commerce gateway; UPI is one method alongside cards and NetBanking.
- **NRE / NRO UPI** — cross-border scan-and-pay live in 10 of the 12 approved geographies (two in active rollout).
- **UPI 123Pay (IVR for feature phones)** — not currently enabled as an issuer; not on the current-quarter roadmap. Enabling requires a dedicated 8–12 week programme.
- **USSD** — supported at the scheduled-commercial-bank level; not a primary UPI surface.
- **Branch channel** — onboarding and dispute resolution only; no UPI debit.
- **Co-branded / white-label UPI apps** — none currently active.

## 5. Vendor map

- **UPI switch:** third-party switch vendor. Quarterly release pipeline; 6-week change-window for schema additions; emergency patches via direct vendor escalation.
- **Core banking — retail:** vendor retail core. Major upgrade windows align with our annual platform refresh.
- **Core banking — treasury:** separate treasury vendor. Independent release pipeline from retail.
- **KYC vendor:** outsourced under the applicable outsourcing directive. New KYC field-level requirements run on a 4–6 week change-window plus a 2-week pilot.
- **AML / sanctions:** commercial AML + sanctions-screening platform. Rule-pack updates follow a vendor-managed RFC cycle with a 2-week SLA for new MCC / flow rule additions.
- **Fraud management:** in-house ML platform supplemented by a vendor rule-pack. New flow onboarding requires 1–2 weeks of fraud-rule design before live cut-over.
- **Settlement reconciliation:** in-house. Ledger reconciliation against switch logs and network settlement files on T+0 and T+1 cycles.
- **Cloud / infra:** multi-cloud and on-prem hybrid, locally resident per the applicable data-localisation directive.

## 6. Operational envelope

**Per-txn caps (consumer rails):**

| Use case | Per-txn | Notes |
|---|---|---|
| P2P / P2M | ₹1,00,000 | OR 20 txns per 24h, whichever first |
| Mutual funds, credit card, insurance, loan repayment | ₹2,00,000 | |
| IPO and Retail Direct | ₹5,00,000 | |
| New users (first 24h on Android, 72h on iPhone) | ₹5,000 | |
| NRE / NRO via international mobile number | ₹1,00,000 | OR 20 txns per 24h |
| Credit card on UPI | ₹1,00,000 daily | ₹5,000 first 24h post-linking; ₹2,00,000 for selected MCCs |
| UPI Lite | ₹1,000 per-txn · ₹4,000 daily · ₹5,000 wallet | Network ceiling, no internal deviation |

**Fees:** no customer-side fees for standard P2P / P2M UPI. Credit-card-on-UPI carries the network-mandated interchange on P2M above ₹2,000.

**Latency:** internal P99 target for issuer-side response is 800ms.

**Throughput:** capacity engineered for forecast peak load with 30% headroom; refresh cycle is annual.

**Maintenance windows:** late-night and weekend. Published 48–72 hours ahead of any scheduled UPI service window.

## 7. Implementation patterns

- **Release cadence — acquisition journeys:** 3-week cycle.
- **Release cadence — UPI rails:** dedicated monthly release window. Schema changes ride this window after a switch-side cert pass.
- **Big-bang vs phased:** phased is our default for any network mandate. Internal pilot → 10% customer rollout → 50% → full. Big-bang is reserved for regulatory-mandated cutovers with no choice.
- **Microservice vs core extension:** new UPI features land as microservices behind the switch. The core adapter is modified only where the change touches account-level ledger semantics.
- **Cert engagement style:** mock-first against our internal simulator → staging cert against the network sandbox → production cut-over with monitoring. We do not do prod-first launches.
- **Change-window discipline:** UPI rail deployments only inside the monthly release window unless a network-level incident overrides.

## 8. Known constraints

The constraints below are the load-bearing engineering quirks the analyser must treat as definitive when evaluating any incoming change:

- **Core vendor boundary.** Any change touching treasury (large-value rails, settlement-bank flows, UPI International FX legs) crosses the retail → treasury core boundary and inherits both vendors' release cadences. Budget 2× lead time.
- **Credit-card-on-UPI is debit-only.** Credit cards on UPI do not accept inward credit by network design. Features that presume bidirectional credit flow will not work for this product.
- **Refund flow timelines differ by product.** P2P: 3 working days. P2M: 3–4 working days. Credit-card-on-UPI: 4–7 working days. Features changing refund SLAs need to be designed against all three independently.
- **AutoPay mandate lifecycle.** Maximum 30 active mandates per VPA. E-mandate revoke requires customer SMS confirmation (not pure in-app). Retry-on-failed-debit follows the network's standard backoff with no internal extension. Features changing any of these need explicit design coordination.
- **Payments-app legacy lineage.** Pre-rebuild Meridian Pay wallet transaction history is read-only via batch lookup; no online API. Features needing a unified historical view across old and new must accept this asymmetry.
- **123Pay (IVR) not enabled.** Feature-phone IVR issuer participation requires a dedicated 8–12 week enablement programme on our side.
- **UPI Lite X (offline NFC) not enabled.** Same 8–12 week enablement programme applies if required.
- **UPI Circle / delegated payments not enabled.** No current roadmap commitment.
- **NRI UPI geography coverage.** Live in 10 of 12 approved geographies; two are in active rollout but not yet live. Features requiring full coverage will have a partial-coverage gap until rollout completes.

## 9. Recent UPI rollouts

| Feature | Status | Cohort posture |
|---|---|---|
| UPI Lite (initial launch) | Live since Sep 2022 | Initial bank cohort; on-time |
| UPI Lite Nov 2024 updates | Live since Nov 2024 | On-time with network-wide rollout |
| UPI AutoPay | Live as remitter + PSP | On-time at network go-live |
| Credit Card on UPI | Live since mid-2023 | Early / leading adopter |
| UPI for IPO (₹5L mandate) | Live via our broking arm | On-time; SCSB participant |
| UPI International (scan and pay) | Live for NRE / NRO outbound | On-time; 10 of 12 geographies, remaining in rollout |
| UPI 123Pay (feature phone) | Not enabled | Not on current roadmap |
| UPI Lite X (offline NFC) | Not enabled | Not on current roadmap |
| UPI Circle (delegated payments) | Not enabled | Not on current roadmap |

## 10. Regulatory posture

- **Master Directions in scope:** KYC, Digital Payment Security Controls, Outsourcing of IT Services. The systemically-important framework applies.
- **Supervisory history:** two minor monetary penalties in the last 24 months, both arising from routine inspections (KYC customer risk-tier categorisation; outsourcing of financial services). Both remediated and closed. *(Illustrative — a real profile should state its actual position, because the analyser weighs regulatory exposure when judging feasibility.)*
- **Data localisation:** all UPI payment-system data stored locally per the applicable directive. No cross-border replication.
- **Audit cycle:** annual statutory supervisory inspection. Internal audit operates a quarterly review of UPI controls.
- **Ombudsman:** active participant in the integrated ombudsman scheme. UPI dispute redressal follows network chargeback / arbitration flows.

## 11. Org capabilities

- **UPI / digital payments engineering:** dedicated UPI platform pod. Hiring is active under a multi-hundred-person engineering programme.
- **Delivery velocity:** on-time or early on the last five network mandates.
- **Release-management discipline:** staging-heavy. Change-window-disciplined. Maintenance windows announced 48–72 hours ahead. We avoid hot-deploys for UPI rail changes.
- **Freeze windows:**
  - FY-end: last week of March → 1 April (soft freeze).
  - Quarter-end: last 2 business days of every quarter (book-close).
  - Annual external IT-audit window: non-essential change throttle.
