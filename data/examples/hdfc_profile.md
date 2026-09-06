---
partner: HDFC Bank
profile_version: 1.0
last_updated: 2026-05-27
maintained_by: HDFC UPI Platform Team
---

# Partner Profile — HDFC Bank

> EXAMPLE profile (worked reference). Copy this to `data/partner_profile.md`
> and adapt, or point `PARTNER_PROFILE_PATH` at your own. Authoritative
> engineering brief loaded as context by the feasibility analyser when
> evaluating any incoming NPCI change_communication. Treat every statement
> here as load-bearing.

## Quick reference

- **UPI roles played:** Payer PSP · Payee PSP · Remitter Bank · Beneficiary Bank · Sponsor Bank (selective TPAP / PPI) · UPI International acquirer (NRE / NRO).
- **Core banking:** Oracle FLEXCUBE (retail) + TCS BaNCS (treasury). UPI changes that touch treasury cross a vendor boundary.
- **UPI switch:** Mindgate Solutions (acquired by PayU 2024-25). Single-vendor dependency for switch-level changes; 6-week change-window for schema additions.
- **Mobile apps:** HDFC MobileBanking + PayZapp (ground-up rebuild 2022-23 with Pixel digital credit card integration).
- **Per-txn caps:** ₹1L P2P/P2M · ₹2L mutual funds / credit card / insurance / loans · ₹5L IPO and RBI Retail Direct · ₹5K first 24h post-registration.
- **Daily / count cap:** ₹1L or 20 txns per 24h, whichever first.
- **UPI Lite posture:** ₹5K wallet · ₹1K per-txn · ₹4K daily. NPCI ceiling, no internal deviation.
- **RuPay-on-UPI:** Issuer live since mid-2023. Debit-only by NPCI design — inward credit not supported.
- **NRI UPI International:** Live in 10 of 12 NPCI-approved geographies (Canada and Hong Kong in active rollout).
- **Engineering org:** Digital Factory + Enterprise Factory. 3-week cadence for acquisition journeys; UPI rail changes ride a dedicated monthly UPI release window.
- **Freeze windows:** FY-end soft freeze (last week of March → 1 April). Quarter-end book-close (last 2 business days of every quarter). External IT-audit windows around the annual RBI ISE inspection throttle non-essential change.
- **Regulatory weight:** D-SIB. RBI Master Directions on KYC, Digital Payment Security Controls (2021), and Outsourcing of IT Services (2023) in scope.

## 1. Identity and roles in UPI

We are a Scheduled Commercial Bank licensed under the Banking Regulation Act 1949 and an NPCI member. The RBI has designated us a Domestic Systemically Important Bank.

**UPI roles played:**
- **Payer PSP** — we issue the `@hdfcbank` handle and onboard customers as payers via the MobileBanking app and PayZapp.
- **Payee PSP** — used by merchants via SmartHub Vyapar and our UPI Collect products.
- **Remitter Bank** — among the top 3 remitter banks by monthly UPI volume.
- **Beneficiary Bank** — accept inbound UPI credits to our accounts.
- **Sponsor Bank** — we sponsor a contracted set of TPAPs and PPIs on UPI under bilateral arrangements. Sponsorship is not auto-rolled-out for new flows; each new flow requires explicit re-contracting with the sponsored party.
- **UPI International acquirer** — Global Scan & Pay enabled for NRE / NRO customers.

**Customer footprint:** ~9,500 branches and ~21,000 ATMs across India, with >50% of branches in semi-urban and rural areas. >93% of customer engagement is digital. UPI-enabled customer base is at multi-crore scale.

## 2. Tech stack

**Core banking — retail:** Oracle FLEXCUBE on a current vendor-supported major version. Bank-specific customisations live in a separate payments adapter layer; the adapter is what NPCI-driven changes typically touch first. Direct FLEXCUBE schema changes are rare and follow Oracle's release channel.

**Core banking — treasury:** TCS BaNCS. Treasury operations are isolated from the retail UPI path under normal flows. Any UPI change that bridges treasury (large-value rails, settlement-bank role if assumed, FX legs for UPI International) crosses the FLEXCUBE → BaNCS boundary and inherits both release cadences. Plan for 2× lead time on cross-boundary changes.

**UPI switch:** Mindgate Solutions, deployed in primary + DR. Mindgate is on a quarterly release pipeline. Schema additions and new transaction types require a 6-week change-window negotiation. Emergency patches move faster only with NPCI-level urgency. PayU's 2024-25 acquisition of Mindgate has not yet altered our governance cadence but is monitored.

**Mobile apps:** HDFC MobileBanking app and PayZapp. Both native iOS + Android. PayZapp was rebuilt ground-up in 2022-23 with the Pixel digital credit card integration. Pre-2022 PayZapp wallet history is read-only via batch lookup; no online API.

**Middleware:** Enterprise Factory stack on cloud-native middleware, decoupled from FLEXCUBE via event-driven adapters. Open-source primary with vendor-supported hardened paths.

**UPI schema version:** Aligned with NPCI's current production schema covering AutoPay, Lite, RuPay-on-UPI, and IPO mandates. Schema upgrades require the Mindgate switch upgrade as a precondition.

**Data centres:** Dual primary in Mumbai and Bengaluru. All UPI workloads run on India-localised infrastructure per the RBI April 2018 data-localisation circular.

## 3. API and integration patterns

**Inbound flow from NPCI:** NPCI → Mindgate switch → fraud / risk engine (parallel) → CBS (FLEXCUBE) debit → confirmation back to NPCI. All within the NPCI 30-second transaction window.

**Idempotency:** UPI transactions are idempotent on `txnId` at the switch layer. The CBS adapter de-duplicates on `txnId + RRN` within a 24-hour window.

**Retry semantics:** No silent retries on debit operations. Failed debits surface as terminal NPCI response codes (U-series / Z-series). Reconciliation handles late-credit scenarios through the standard NPCI chargeback windows.

**Timeout posture:** 25-second internal timeout on the CBS debit call (5-second buffer under the NPCI 30-second envelope). 8-second timeout on fraud / risk parallel calls. Fraud-call timeout does NOT block the debit path — fraud rules that need to block apply pre-debit.

**Reconciliation:** Deferred via T+0 end-of-day NPCI settlement files plus T+1 in-house reconciliation jobs against switch + ledger.

**RuPay-on-UPI:** Credit-card linkage flows trigger a separate event into our credit-card statementing pipeline (Enterprise Factory event bus → credit-card ledger) for next-cycle billing.

## 4. Channels

- **HDFC MobileBanking app** — primary UPI registration via SMS device-binding + MPIN. Native iOS + Android.
- **PayZapp** — TPAP-style standalone app. UPI, Pixel digital credit card, bill payments, merchant payments.
- **NetBanking (web)** — UPI history, dispute initiation, IPO ASBA-via-UPI mandate flows. Not a live UPI debit surface.
- **SmartHub Vyapar** — merchant acceptance for current-account holders. Tap & Pay, UPI QR, UPI Collect, SMS Pay.
- **SmartGATEWAY** — e-commerce payment gateway; UPI is one method alongside cards and NetBanking.
- **NRE / NRO UPI** — Global Scan & Pay live in 10 of the 12 NPCI-approved geographies (Canada and Hong Kong in active rollout).
- **UPI 123Pay (IVR for feature phones)** — not currently enabled as an issuer; not on the current-quarter roadmap. Enabling requires a dedicated 8–12 week programme.
- **USSD (`*99#`)** — supported at the scheduled-commercial-bank level; not a primary UPI surface.
- **Branch channel** — onboarding and dispute resolution only; no UPI debit.
- **Co-branded / white-label UPI apps** — none currently active.

## 5. Vendor map

- **UPI switch:** Mindgate Solutions. Quarterly release pipeline; 6-week change-window for schema additions; emergency patches via direct vendor escalation.
- **Core banking — retail:** Oracle FLEXCUBE. Major upgrade windows align with our annual platform refresh.
- **Core banking — treasury:** TCS BaNCS. Independent release pipeline from retail.
- **KYC vendor:** Outsourced under RBI's Master Direction on Outsourcing of IT Services. New KYC field-level requirements run on a 4–6 week change-window plus a 2-week pilot.
- **AML / sanctions:** Commercial AML + sanctions-screening platform. Rule-pack updates follow a vendor-managed RFC cycle with a 2-week SLA for new MCC / flow rule additions.
- **Fraud management:** In-house ML platform supplemented by vendor rule-pack. New flow onboarding requires 1–2 weeks of fraud-rule design before live cut-over.
- **Settlement reconciliation:** In-house. Ledger reconciliation against Mindgate switch logs and NPCI settlement files on T+0 and T+1 cycles.
- **Cloud / infra:** Multi-cloud and on-prem hybrid. India-localised per RBI April 2018 circular.

## 6. Operational envelope

**Per-txn caps (consumer rails):**

| Use case | Per-txn | Notes |
|---|---|---|
| P2P / P2M | ₹1,00,000 | OR 20 txns per 24h, whichever first |
| Mutual funds, credit card, insurance, loan repayment | ₹2,00,000 | |
| IPO and RBI Retail Direct | ₹5,00,000 | |
| New users (first 24h on Android, 72h on iPhone) | ₹5,000 | |
| NRE / NRO via international mobile number | ₹1,00,000 | OR 20 txns per 24h |
| RuPay credit card on UPI | ₹1,00,000 daily | ₹5,000 first 24h post-linking; ₹2,00,000 for MCC 5960 / 6300 / 6529 |
| UPI Lite | ₹1,000 per-txn · ₹4,000 daily · ₹5,000 wallet | NPCI ceiling, no internal deviation |

**Fees:** No customer-side fees for standard P2P / P2M UPI. RuPay-on-UPI carries the NPCI-mandated 1.1% interchange on P2M above ₹2,000.

**Latency:** Internal P99 target for issuer-side response is 800ms.

**Throughput:** Capacity engineered for forecast peak load with 30% headroom; refresh cycle is annual.

**Maintenance windows:** Late-night and weekend. Published 48–72 hours ahead of any scheduled UPI service window.

## 7. Implementation patterns

- **Release cadence — acquisition journeys:** 3-week cycle (Digital Factory).
- **Release cadence — UPI rails:** Dedicated monthly UPI release window. Schema changes ride this window after a Mindgate-side cert pass.
- **Big-bang vs phased:** Phased is our default for any NPCI mandate. Internal pilot → 10% customer rollout → 50% → full. Big-bang is reserved for regulatory-mandated cutovers with no choice.
- **Microservice vs core extension:** New UPI features land as microservices behind the Mindgate switch. The CBS adapter is modified only where the change touches account-level ledger semantics.
- **Cert engagement style:** Mock-first against our internal NPCI simulator → staging cert against NPCI's sandbox → production cut-over with monitoring. We do not do prod-first launches.
- **Change-window discipline:** UPI rail deployments only inside the monthly release window unless an NPCI-level incident overrides.

## 8. Known constraints

The constraints below are the load-bearing engineering quirks the analyser must treat as definitive when evaluating any incoming change:

- **CBS vendor boundary.** Any change touching treasury (large-value rails, settlement-bank flows, UPI International FX legs) crosses the
on UPI do not accept inward credit by NPCI design. Features that presume bidirectional credit flow will not work for this product.
- **Refund flow timelines differ by product.** P2P: 3 working days. P2M: 3–4 working days. RuPay-on-UPI: 4–7 working days. Features changing refund SLAs need to be designed against all three independently.
- **AutoPay mandate lifecycle.** Maximum 30 active mandates per VPA. E-mandate revoke requires customer SMS confirmation (not pure in-app). Retry-on-failed-debit follows NPCI's standard backoff with no internal extension. Features changing any of these need explicit design coordination.
- **PayZapp legacy lineage.** Pre-2022 PayZapp wallet transaction history is read-only via batch lookup; no online API. Features needing a unified historical view across old and new PayZapp must accept this asymmetry.
- **123Pay (IVR) not enabled.** Feature-phone IVR issuer participation requires a dedicated 8–12 week enablement programme on our side.
- **UPI Lite X (offline NFC) not enabled.** Same 8–12 week enablement programme applies if required.
- **UPI Circle / delegated payments not enabled.** No current roadmap commitment.
- **NRI UPI geography coverage.** Live in 10 of 12 NPCI-approved geographies. Canada and Hong Kong are in active rollout but not yet live; features requiring full 12-geography coverage will have a partial-coverage gap until rollout completes.

## 9. Recent UPI rollouts

| Feature | Status | Cohort posture |
|---|---|---|
| UPI Lite (initial launch) | Live since Sep 2022 | Initial 8-bank cohort; on-time |
| UPI Lite Nov 2024 updates | Live since Nov 2024 | On-time with NPCI-wide rollout |
| UPI AutoPay | Live as remitter + PSP | On-time at NPCI go-live |
| RuPay Credit Card on UPI | Live since mid-2023 | Early / leading — first bank to ₹500cr on this rail |
| UPI for IPO (₹5L mandate) | Live via InvestRight + HDFC Securities | On-time; SCSB participant |
| UPI International (Global Scan & Pay) | Live for NRE / NRO outbound | On-time; 10 of 12 geographies, remaining in rollout |
| UPI 123Pay (feature phone) | Not enabled | Not on current roadmap |
| UPI Lite X (offline NFC) | Not enabled | Not on current roadmap |
| UPI Circle (delegated payments) | Not enabled | Not on current roadmap |

## 10. Regulatory posture

- **RBI Master Directions in scope:** KYC, Digital Payment Security Controls (2021), Outsourcing of IT Services (2023). D-SIB framework applies.
- **Recent RBI penalties:**
  - March 2025 — ₹75 lakh, KYC customer risk-tier categorisation, FY2023 inspection.
  - November 2025 — ₹91 lakh, KYC + interest-rate-on-advances + outsourcing-of-financial-services, ISE 2024 inspection.
- **Data localisation:** All UPI payment-system data stored only in India per the RBI April 2018 circular. No cross-border replication.
- **Audit cycle:** RBI Statutory Inspection for Supervisory Evaluation (ISE) annually. Internal audit operates a quarterly review of UPI controls.
- **Ombudsman:** Active participant in the RBI Integrated Ombudsman Scheme 2021. UPI dispute redressal follows NPCI chargeback / arbitration flows.

## 11. Org capabilities

- **UPI / digital payments engineering:** Dedicated UPI platform pod within the Digital Factory. Hiring is active under the 500-person Digital + Enterprise Factory programme.
- **Delivery velocity:** On-time or early on the last five NPCI mandates (UPI Lite cohort, RuPay-on-UPI, IPO ₹5L mandate, NRI UPI International, UPI Lite Nov 2024 updates).
- **Release-management discipline:** Staging-heavy. Change-window-disciplined. Maintenance windows announced 48–72 hours ahead. We avoid hot-deploys for UPI rail changes.
- **Freeze windows:**
  - FY-end: last week of March → 1 April (soft freeze).
  - Quarter-end: last 2 business days of every quarter (book-close).
  - Annual external IT-audit window: non-essential change throttle.
