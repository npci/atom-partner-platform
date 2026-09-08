## What this changes, and why

<!-- The diff says what. This says why now, and what you considered instead. -->

Closes #

## Type

- [ ] `feat` — new capability
- [ ] `fix` — behaviour was wrong before
- [ ] `docs` / `refactor` / `perf` / `test` / `build` / `ci` / `chore`

## Checks

- [ ] Every commit is signed off (`git commit -s`) — **PRs without a DCO sign-off on every commit cannot be merged**
- [ ] Commit messages follow Conventional Commits
- [ ] `pytest` passes
- [ ] `cd frontend && npm run build` passes
- [ ] New behaviour has tests; a bug fix has a regression test

## Verification

<!-- "The tests should pass" is not "I ran the tests". Say what you actually ran. -->

## Conventions this change touches

- [ ] **A2A wire code** — `a2a_common/{hmac_signer,protocol,executor_base}.py` are mirrored with the Authority platform. Neither repository's CI can see the other's copies, so a change here is a release-coordination duty, not a build-time guarantee
- [ ] **New table** — built by `create_all`; an added COLUMN needs an `_ensure_*()` helper, because this platform has no alembic
- [ ] **Outbound sender** — did not introduce `asyncio.run` in a path reachable from the event loop, and did not swap it for `anyio.from_thread` in a path reached from `handlers/_background.py` (those threads come from `asyncio.to_thread` and are not anyio workers)
