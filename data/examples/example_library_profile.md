---
partner: Anna Memorial Library
profile_version: 1.0
last_updated: 2026-05-27
maintained_by: Anna Library Systems Team
---

# Partner Profile — Anna Memorial Library

> EXAMPLE profile (worked reference) for a FICTIONAL library. Every name,
> vendor, number and date below is invented to show the expected shape and level
> of detail — none of it describes a real institution.
>
> This example exists alongside `example_bank_profile.md` to demonstrate that
> the §1–§11 structure is NOT industry-specific. The two describe completely
> different ecosystems using the same sections, which is the point: the profile
> captures what a deployment runs, and the domain pack (`DOMAIN_PACK`) supplies
> the vocabulary. If a section only makes sense for one industry, the template
> is wrong — not the deployment.
>
> Pairs with `backend/app/packs/nlln/nlln.yaml`.
>
> This is the authoritative engineering brief loaded as context by the
> feasibility analyser when evaluating any incoming change_communication. The
> analyser treats every statement as load-bearing, so an inaccurate profile
> produces confidently wrong feasibility verdicts. Keep it current.

## Quick reference

- **Roles played:** Lending Library · Borrowing Library · Member Registry for the eastern regional cluster.
- **Core systems of record:** Koha-derived catalogue (self-hosted) + a separate membership system. Changes touching membership cross a vendor boundary.
- **Integration gateway:** single NLLN protocol adapter, primary + standby. 4-week change window for schema additions.
- **Channels / surfaces:** branch desk client, public OPAC, member mobile app.
- **Per-request limits:** 8 concurrent loans per member · 3 active recalls per member · 40 inter-library requests per branch per day.
- **Volume ceiling:** ~12,000 protocol messages per 24h at peak (term start).
- **Freeze windows:** term-start fortnight (Jun, Jan), annual stock-take in Sep.
- **Regulatory weight:** state library authority reporting; member data under national privacy law.

## 1. Identity and roles in the ecosystem

Public reference library, 240k catalogued titles across 11 branches, ~86k active
members. Within the National Library Lending Network we act as a Lending Library
(we hold and dispatch), a Borrowing Library (we request on behalf of members),
and the Member Registry for the eastern regional cluster — meaning other
libraries call us to verify standing for members registered here.

We are not a Cataloguing Authority; we consume the national bibliographic record
rather than publishing to it.

## 2. Tech stack

Catalogue is a Koha-derived system, self-hosted, heavily customised around
holdings and shelf location. Membership and standing live in a separate
commercial system from a different vendor — this boundary matters: anything
touching `Member.standing` or loan ceilings needs that vendor's release train.

NLLN protocol is terminated by a single adapter service we maintain in-house
(`nlln-contract` v1.1 schema). Branch desk client is a thick client; OPAC and
mobile app talk to a shared read API.

Hosted in a state-government data centre; member data does not leave the region.

## 3. API and integration patterns

Inbound requests from the authority arrive at the protocol adapter, which
validates against the published XSD before anything reaches the catalogue. All
mutating operations are idempotent on `msgId`; we retain a 48h dedupe window.

Retries: 3 attempts with exponential backoff, then park to a manual queue.
Timeout posture is 4s at the adapter, 10s end-to-end. We reconcile loan state
nightly against the authority's report and raise discrepancies through the
standard channel.

## 4. Channels

Branch desk client (all 11 branches, live), public OPAC (live, read-only for
lending state), member mobile app (live, supports reservations but NOT recalls —
recall acknowledgement is desk-only today).

## 5. Vendor map

| Vendor            | Scope                        | Change window | SLA          |
|-------------------|------------------------------|---------------|--------------|
| Catalogue vendor  | Koha derivative, holdings    | 4 weeks       | 99.5%        |
| Membership vendor | member records, standing     | 8 weeks       | 99.9%        |
| Mobile partner    | member app                   | 2 weeks       | best-effort  |
| DC operator       | hosting, network             | 6 weeks       | 99.95%       |

The membership vendor's 8-week window is the long pole on anything touching
member standing or loan ceilings.

## 6. Operational envelope

- 8 concurrent loans per member; 12 for staff and research members.
- 3 active recalls per member.
- Standard loan 21 days; short loan 7 days; reference material non-circulating.
- Renewal permitted twice unless a recall is outstanding.
- 40 inter-library requests per branch per day (soft cap, alerts at 90%).
- Latency target 2s at the desk client for a loan operation.
- Maintenance window Sunday 02:00–05:00.

## 7. Implementation patterns

Quarterly release train with a monthly patch slot. We prefer extending the
adapter over changing the catalogue core, because the catalogue carries local
customisation that is expensive to re-test. Certification is engaged early — we
run the authority's cases against a staging catalogue seeded with synthetic
members before declaring readiness.

## 8. Known constraints

- **Membership vendor boundary (8 weeks).** Any change to `Member.standing`
  semantics, loan ceilings, or suspension rules waits on their release train.
  This is the single most common reason a proposed deadline does not work.
- **Mobile app cannot acknowledge recalls.** A change requiring member-side
  recall acknowledgement needs mobile work we have not scoped.
- **Term-start freeze (Jun, Jan).** Two-week freeze at each term start; peak
  load and zero appetite for change.
- **Stock-take (Sep).** Catalogue is read-mostly for two weeks; holdings updates
  are queued, so anything depending on live holdings accuracy should avoid it.
- **Schema v1.1.** We have not adopted the optional `LoanPeriod` element; we
  still send `loanDays`. A change assuming v1.2 requires adapter work.

## 9. Recent rollouts

| Change                          | Status | Posture                         |
|---------------------------------|--------|---------------------------------|
| Recall notification (CR-1)      | Live   | Phased, 3 branches then all     |
| Availability check v1.1         | Live   | Big-bang, low risk              |
| Member standing sync            | Live   | Delayed 6 weeks by vendor train |
| Renewal event (CR-2)            | In UAT | Adapter-only, on track          |
| Loan period restructure (CR-3)  | Not started | Blocked on membership vendor |

## 10. Regulatory posture

Reporting to the state library authority twice yearly. Member data falls under
national privacy law: we hold borrowing history for 24 months, then aggregate.
Members may request erasure of borrowing history, which we honour within 30 days
except where a loan is outstanding. Annual internal audit each October.
Complaints escalate through the library ombudsman.

## 11. Org capabilities

Systems team of 9: 4 on catalogue and adapter, 2 on integrations, 2 on desk
client, 1 SRE. Delivery track record is steady rather than fast — the last four
protocol changes shipped within their committed window, but each was scoped to
avoid the membership vendor. Release management is disciplined: change advisory
board weekly, no unreviewed production changes, full rollback plan required
before any adapter deployment.
