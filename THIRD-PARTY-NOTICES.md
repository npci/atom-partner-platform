# Third-party notices

This product bundles no third-party source. It depends on packages installed at
build time, whose licences are summarised here. The per-package attribution
list lives in [`NOTICE`](NOTICE); this file carries the analysis — what is
copyleft, why it does not reach this project's own code, and what a lawyer
should check before publication.

Both closures are fully pinned, so these counts are exact rather than an
estimate of what a resolve might produce:

| Closure | Lockfile | Packages |
|---|---|---:|
| Python | `backend/requirements.lock` (SHA-256 per distribution, installed with `--require-hashes`) | 70 |
| Node | `frontend/package-lock.json` | 160 |

Regenerate whenever `requirements.txt` or `package.json` changes, and publish an
SBOM per release:

```bash
docker build -t atom-partner-backend:latest ./backend
syft scan docker:atom-partner-backend:latest --output syft-json=sbom.json
```

## Licence distribution

### Python — 70 locked distributions, 71 in the image

| Licence | Packages |
|---|---:|
| MIT (incl. `MIT-0`, `0BSD`) | 28 |
| BSD (3-clause and unversioned) | 15 |
| Apache-2.0 (incl. variants) | 10 |
| Copyleft / weak-copyleft (below) | 5 |
| BSD-2-Clause | 4 |
| Dual permissive (`MIT OR Apache-2.0`, `Apache-2.0 OR BSD-3-Clause`, `Apache-2.0 OR BSD-2-Clause`) | 3 |
| PSF-2.0 (`typing-extensions`, `greenlet`) | 2 |
| HPND / MIT-CMU (`Pillow`) | 1 |
| No licence metadata | 3 |

The table sums to 71 because it is measured on the built image, which carries
one package the lock does not: `pip`, shipped by the `python:3.12-slim` base
rather than declared as a dependency. Base-image packages are inventoried in
[`NOTICE`](NOTICE) §4, so the lock's 70 is the number that describes this
project's own closure.

The three without metadata ship no licence field and should be confirmed before
publication: `aiologic`, `aiosqlite`, `culsans`. All three are permissive
upstream, so this is a metadata gap rather than a licensing one — but it is a
gap, and an earlier revision of this file implied there were none.

71 distributions rather than the 72 `==` pins in the lock: `psycopg[binary]`
appears both as the extra and as its resolved `psycopg-binary` wheel, and is
counted once.

### Node — 160 locked packages

| Licence | Packages |
|---|---:|
| MIT | 143 |
| MPL-2.0 (`lightningcss` and its 11 platform binaries) | 12 |
| ISC | 3 |
| Apache-2.0 | 1 |
| BSD-3-Clause | 1 |

Every Node package resolves to a declared licence — there are no
`UNKNOWN`-metadata entries to chase before release, which is worth stating
because the Authority platform has twelve.

## Copyleft and weak-copyleft dependencies

Read this section before distributing binaries or a container image. **None of
these change the licence of this project's own code**, which is MIT —
they are imported at runtime, unmodified, as installed from PyPI or npm.

| Package | Version | Declared | Direct? | Notes |
|---|---|---|---|---|
| `psycopg[binary]` | 3.3.4 | LGPL-3.0-or-later | direct | The main Postgres driver. Imported unmodified; the binary wheel is upstream's own build. |
| `python-gitlab` | 4.9.0 | LGPL-3.0-or-later | direct | Code-RAG source fetch. Imported unmodified. |
| `chardet` | 7.6.0 | LGPL-2.1-or-later | transitive | Via `python-gitlab` → `requests-toolbelt`. |
| `certifi` | 2026.7.22 | MPL-2.0 | transitive | CA bundle. File-level copyleft; unmodified use is fine. |
| `tqdm` | 4.70.0 | `MPL-2.0 AND MIT` | transitive | Progress bars inside SDK clients. Fine. |
| `lightningcss` | (npm) | MPL-2.0 | transitive, build-time | CSS transform in the Vite pipeline. Runs at build; no MPL code reaches the served bundle. |

**No GPL or AGPL package appears in either closure.** That is the finding most
reviews are actually looking for, so it is stated plainly rather than left to be
inferred from the absence of a row.

### Differences from the Authority platform

Both repositories ship the same A2A contract code, so a reviewer comparing the
two will notice the dependency inventories do not match. The differences are
deliberate:

- **`psycopg3`, not `psycopg2-binary`.** This service moved to a dedicated
  Postgres instance and took the modern driver with it. Both are LGPL, so the
  position is unchanged.
- **No `torch` / `sentence-transformers`.** The multi-gigabyte local reranker is
  Authority-side only. It is why this repository needs one lockfile where the
  Authority needs one per architecture.
- **No `ldap3`, no `asyncssh`.** No directory integration and no SSH path here,
  which removes two of the Authority platform's copyleft rows.
- **`PyJWT`, not `python-jose`.** `python-jose` declares `ecdsa` as an
  unconditional dependency, and `ecdsa` carries CVE-2024-23342 (HIGH) with no
  fix upstream. PyJWT has no required dependencies. See the rationale comment in
  `backend/requirements.txt`.

### The position, and its limits

On the standard reading — a Python `import` is dynamic linking, and LGPL §6 is
satisfied by shipping unmodified upstream packages via pip — none of the above
requires this project to change its licence. The MPL packages are file-level
copyleft, which reaches modified MPL files only, and no MPL file here is
modified.

**That is a legal conclusion, and this file is not legal advice.** It is written
so counsel has the facts in one place rather than an SBOM to wade through.
Confirm it before publication.

## Known reconciliation points

**Resolved as of 2026-09-03.** An earlier revision of this file recorded four
pins where `backend/requirements.txt` ran ahead of the generated
`backend/requirements.lock`, the most serious being `PyJWT`: the application
does `import jwt` in two modules and no package in the lock provided it, which
made the lock unbuildable rather than merely out of date.

The lock has since been regenerated and all four now agree:

| Package | `requirements.txt` | `requirements.lock` |
|---|---|---|
| `fastapi` | 0.141.1 | 0.141.1 |
| `starlette` | 1.3.1 | 1.3.1 |
| `PyJWT` | 2.13.0 | 2.13.0 |
| `pyasn1` | 0.6.4 | 0.6.4 |

`python-jose` and `ecdsa` are gone from the closure entirely. Since the image
installs from the lock (`pip install --require-hashes -r requirements.lock`),
**the lock is what actually ships**, and the licence facts above are drawn from
a scan of the built image for that reason.

The corresponding *Known gaps* entry in [`SECURITY.md`](SECURITY.md) should be
retired with it.

For licence review purposes the two candidates are equivalent: `python-jose`,
`PyJWT` and `ecdsa` are all MIT, so whichever way the reconciliation lands, no
copyleft obligation changes. The outcome does change the CVE exposure.

## What is not covered here

Container base images and the OS packages inside them are inventoried in
[`NOTICE`](NOTICE) §4. Distributing a built image distributes those too, and
they are not covered by this project's MIT grant.

---

*Last reviewed: 2026-09-03, against a locally built backend image (71 Python distributions).*
