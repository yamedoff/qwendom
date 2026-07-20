# Third-party and asset provenance

Audit date: 2026-07-13. This inventory supports hackathon submission review;
it is not legal advice and does not choose Qwendom's own project license.

## Frontend dependency inventory

The committed `frontend/package-lock.json` contained 119 installed package
records and license metadata for every record at audit time:

| License | Package records |
|---|---:|
| MIT | 109 |
| ISC | 6 |
| Apache-2.0 | 2 |
| BSD-3-Clause | 1 |
| CC-BY-4.0 | 1 |

The CC-BY-4.0 record is `caniuse-lite@1.0.30001799`. Direct dependencies were:

| Package | Audited installed version | License |
|---|---:|---|
| `@vitejs/plugin-react` | 4.7.0 | MIT |
| `lucide-react` | 0.468.0 | ISC |
| `vite` | 6.4.3 | MIT |
| `typescript` | 5.9.3 | Apache-2.0 |
| `react` | 19.2.7 | MIT |
| `react-dom` | 19.2.7 | MIT |
| `@types/react` | 19.2.17 | MIT |
| `@types/react-dom` | 19.2.3 | MIT |

The UI does not download or bundle an external font. Icons come from the
ISC-licensed `lucide-react` package.

## Backend dependency inventory

The direct requirements and their audited installed licenses were:

| Package | Audited installed version | License metadata |
|---|---:|---|
| `agno` | 2.6.19 | Apache-2.0 |
| `mcp` | 1.28.0 | MIT |
| `fastapi` | 0.129.0 | MIT |
| `uvicorn` | 0.41.0 | BSD-3-Clause |
| `pydantic` | 2.12.5 | MIT |
| `pydantic-settings` | 2.13.1 | MIT |
| `python-dotenv` | 1.2.1 | BSD-3-Clause |
| `wuying-agentbay-sdk` | 0.22.3 | Apache-2.0 |
| `httpx` | 0.28.1 | BSD-3-Clause |

The installed transitive closure reported only Apache-2.0, BSD-2-Clause,
BSD-3-Clause, ISC, MIT, MIT-or-Apache-2.0, MPL-2.0, PSF, and PSF-2.0 license
families. The environment-specific `pywin32` dependency is PSF-licensed.
Because `backend/requirements.txt` uses compatible ranges rather than a lock
file, repeat this metadata audit again if the final deployment environment
installs different versions. The local final-readiness refresh on July 13
matched every direct version and license listed above; the frontend lockfile
still contained 119 licensed installed-package records with the same counts.

## Repository media and data

- The benchmark incident packet and deterministic ground truth in
  `backend/benchmarks/fixtures/` were authored for this repository; they do not
  contain an imported external dataset.
- No repository-owned audio, stock photography, external font, or third-party
  logo asset was found in the publish set.

Do not add music or stock media to the final video without recording its source
and redistribution license here.

## Final public-release checks

1. Root Apache-2.0 `LICENSE` is present.
2. Preserve dependency license files in distributed dependency bundles.
3. Frontend lockfile and backend direct-environment inventories were rerun on
   July 13 and matched this document.
4. Confirm every final screenshot/video contains only authorized content and no secrets.
5. Verify GitHub recognizes the selected project license.
