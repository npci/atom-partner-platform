---
partner: <YOUR ORG NAME>
profile_version: 0.1
last_updated: <YYYY-MM-DD>
maintained_by: <YOUR TEAM>
---

# Partner Profile — <YOUR ORG NAME>

> Fill this in with your organisation's authoritative engineering brief. It is
> loaded as context by the feasibility agent when evaluating any incoming
> change_communication from the authority, so every statement here is treated as
> load-bearing fact.
>
> Worked examples for two different domains are in `data/examples/`.
>
> To use: copy this file to `data/partner_profile.md` (the path the platform
> mounts), or point `PARTNER_PROFILE_PATH` at your own file, then replace every
> `<...>` placeholder below.
>
> KEEP THE SECTION NUMBERS. `backend/app/agents/prompts/feasibility.md` cites
> §1, §2, §4, §5, §6 and §8 by number when it explains what to reason about, and
> asks the model to cite them back in its findings. Renumbering or dropping a
> section silently degrades those citations to references to nothing. Rewrite
> the CONTENTS for your domain; leave the numbering alone.
>
> This file is the profile of THIS DEPLOYMENT — what your organisation runs and
> what it has already shipped. It is not where your domain's vocabulary lives;
> that is the domain pack (`DOMAIN_PACK`), which ships with the domain rather
> than being re-authored by every operator.

## Quick reference

- **Roles played:** <the roles you perform in this ecosystem — see the role list your domain pack declares>
- **Core systems of record:** <the systems that own the data this ecosystem touches, and any boundary that matters>
- **Integration gateway:** <the component that terminates the ecosystem protocol, its vendor, and change-window lead time>
- **Channels / surfaces:** <the applications and surfaces you operate>
- **Per-request limits:** <your ceilings by use case>
- **Volume ceiling:** <e.g. N requests per 24h, or the relevant unit>
- **Freeze windows:** <e.g. FY-end, quarter-end, audit windows>
- **Regulatory weight:** <the regime you fall under and its directions in scope>

## 1. Identity and roles in the ecosystem

<Who you are as a participant, and which roles you play. Use the role names your
domain pack declares — they are the same ones the certification flow offers.
Note the footprint you serve.>

## 2. Tech stack

<Systems of record, the gateway that terminates the ecosystem protocol, client
applications, middleware, the schema version you are on, and your hosting /
data-residency posture. Call out vendor boundaries that add lead time.>

## 3. API and integration patterns

<Inbound flow from the authority, idempotency, retry semantics, timeout posture,
and how you reconcile.>

## 4. Channels

<The channels and surfaces you operate, and which are live for this ecosystem's
transactions.>

## 5. Vendor map

<Each vendor on the critical path — gateway, systems of record, identity checks,
risk/fraud screening, settlement or fulfilment, cloud — with its change window
and SLA.>

## 6. Operational envelope

<Limits table, any fees, latency targets, throughput headroom, maintenance
windows.>

## 7. Implementation patterns

<Release cadence, phased vs big-bang, whether you extend the core or build
alongside it, how you engage with certification, change-window discipline.>

## 8. Known constraints

<The load-bearing engineering quirks the analyser must treat as definitive:
vendor boundaries, gateway lead times, freeze windows, product-specific
limitations. This is the section that most often determines whether a proposed
deadline is realistic — be concrete.>

## 9. Recent rollouts

<Table of changes you have shipped in this ecosystem, their status, and delivery
posture.>

## 10. Regulatory posture

<The regulator and directions in scope, recent findings or penalties, data
residency obligations, audit cycle, complaint/escalation route.>

## 11. Org capabilities

<Engineering org, delivery velocity track record, release-management discipline,
freeze windows.>
