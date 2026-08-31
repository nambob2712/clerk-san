# Clerk-san source-license review evidence

Status: reviewed for the exact source-lock snapshot on 2026-08-31.

This file is the public evidence input for `license-policy.json`. It classifies dependency
references in the source archive; it does not approve redistribution of dependency artifacts and is
not legal advice or legal certification.

## Exact input bindings

| Input | SHA-256 | Reviewed rows |
| --- | --- | ---: |
| `requirements.lock` | `114917732b509616ac1a6c23b2f9b5faed4b55f39f4f5cf5a1a94271b2c540f6` | 81 |
| `web/package-lock.json` | `30c583df20c2a1663a310cbc8dc67d6b4b2b828062fb1b3873093d9add7b1a39` | 183 |

The following canonical machine block binds both locks, both complete composite-identity sets, and
the machine policy projection (all policy fields except the evidence envelope itself).

<!-- clerksan-source-license-bindings-v1:start -->
```json
{
  "npmIdentitiesSha256": "e4aeff6dea3bcf50293650b5d7b87419ebc540199081ab08a7fc7f9d444cc63a",
  "npmLockSha256": "30c583df20c2a1663a310cbc8dc67d6b4b2b828062fb1b3873093d9add7b1a39",
  "policyMachineSha256": "0acf9d97ccd54c4b02ac35f6f144a46cf62482025e959624c71bff04213b5a40",
  "pythonIdentitiesSha256": "6ebec811b8da87f69f679ae7472d5c813caa02a5691bb51a8c7be0712638f95a",
  "pythonLockSha256": "114917732b509616ac1a6c23b2f9b5faed4b55f39f4f5cf5a1a94271b2c540f6"
}
```
<!-- clerksan-source-license-bindings-v1:end -->

The Python identity is `(purl, normalized marker)`. The npm identity is
`(package-lock packages path, purl)`. The policy checker must reject a changed digest, identity,
marker, path, license, or scope until this evidence and the policy are reviewed again.

## Python source classifications

These conclusions come from exact-version installed metadata/license files and exact-version
upstream project evidence. They classify source projects. A universal hash lock can select different
wheel contents by platform, so it does not attest the license bundle inside a future selected wheel.

- **MIT (36):** aiosqlite 0.22.1; annotated-doc 0.0.5; annotated-types 0.8.0; anyio
  4.14.2; attrs 26.1.0; blinker 1.9.0; charset-normalizer 3.5.1; et-xmlfile 2.0.0;
  fastapi 0.141.1; h11 0.16.0; httptools 0.8.0; iniconfig 2.3.0; jsonschema
  4.26.0; jsonschema-specifications 2025.9.1; narwhals 2.25.0; openpyxl 3.1.5;
  pgvector 0.5.0; pluggy 1.6.0; pydantic 2.13.4; pydantic-core 2.46.4;
  pydantic-settings 2.15.0; pytest 9.1.1; python-docx 1.2.0; python-magic 0.4.27;
  pytz 2026.3.post1; pyyaml 6.0.3; rapidfuzz 3.14.5; referencing 0.37.0; rpds-py
  2026.6.3; ruff 0.16.4; six 1.17.0; sqlalchemy 2.0.52; toml 0.10.2;
  typing-inspection 0.4.4; urllib3 2.7.0; watchfiles 1.2.0.
- **BSD-3-Clause (19):** altair 6.2.2; click 8.4.2; colorama 0.4.6; httpcore
  1.0.9; httpx 0.28.1; idna 3.19; itsdangerous 2.2.0; jinja2 3.1.6; lxml 6.1.2;
  markupsafe 3.0.3; pandas 2.3.3; protobuf 7.36.0; psutil 7.2.2; pypdf 6.16.2;
  python-dotenv 1.2.3; respx 0.23.1; starlette 1.6.0; uvicorn 0.52.4; websockets
  16.1.1.
- **Apache-2.0 (9):** asyncpg 0.31.0; pyarrow 25.0.1; pydeck 0.9.3;
  pytest-asyncio 1.4.0; python-multipart 0.0.32; requests 2.34.2; streamlit 1.62.0;
  tzdata 2026.3; watchdog 6.0.0.
- **BSD-2-Clause (4):** cobble 0.1.4; imagehash 4.3.2; mammoth 1.12.1; pygments
  2.21.0.
- **BSD-3-Clause source with artifact review required (2):** scipy 1.17.1 and scipy
  1.18.1. Selected wheels carry component terms that remain artifact-specific.
- **BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 (2):** numpy 2.4.6 and
  numpy 2.5.2. Preserve the selected wheel's bundled license evidence if a wheel is distributed.

Other exact rows:

| Package | Source classification | Source-snapshot disposition |
| --- | --- | --- |
| certifi 2026.7.22 | `MPL-2.0` | Source reference accepted; executable/artifact obligations remain separate. |
| greenlet 3.5.5 | `MIT AND PSF-2.0` | Source reference accepted. |
| packaging 26.3 | `Apache-2.0 OR BSD-2-Clause` | Source reference accepted. |
| Pillow 12.3.0 | `MIT-CMU` | Source reference accepted. |
| pypdfium2 5.13.0 | `Apache-2.0 OR BSD-3-Clause` | Source-project classification only; every selected wheel requires platform-specific review of bundled dependency licenses and separate artifact notices. |
| python-dateutil 2.9.0.post0 | `BSD-3-Clause OR Apache-2.0` | Source reference accepted; no artifact branch is selected here. |
| PyWavelets 1.9.0 | `MIT AND BSD-3-Clause` | Source reference accepted; selected-wheel notices remain separate. |
| typing-extensions 4.16.0 | `PSF-2.0` | Source reference accepted. |
| uvloop 0.22.1 | `Apache-2.0 OR MIT` | Source reference accepted; selected-artifact terms remain separate. |

The seven conditional Python identities are:

| Package URL | Normalized marker identity |
| --- | --- |
| `pkg:pypi/colorama@0.4.6` | `sys_platform == "win32"` |
| `pkg:pypi/numpy@2.4.6` | `python_full_version < "3.12"` |
| `pkg:pypi/numpy@2.5.2` | `python_full_version >= "3.12"` |
| `pkg:pypi/scipy@1.17.1` | `python_full_version < "3.12"` |
| `pkg:pypi/scipy@1.18.1` | `python_full_version >= "3.12"` |
| `pkg:pypi/uvloop@0.22.1` | `platform_python_implementation != "PyPy" and sys_platform != "cygwin" and sys_platform != "win32"` |
| `pkg:pypi/watchdog@6.0.0` | `sys_platform != "darwin"` |

All other Python rows use the literal marker identity `unconditional`. `pypdf==6.16.2` is
BSD-3-Clause. No `PyMuPDF`, `pymupdf`, or `fitz` package is accepted.

## npm source classifications and scope

The exact `packages[*].license` value in the bound npm lock is the declared and concluded
source-reference classification. No npm row is missing that field.

| SPDX expression | All rows | Production lock closure | Development/build/test |
| --- | ---: | ---: | ---: |
| MIT | 130 | 24 | 106 |
| Apache-2.0 | 28 | 0 | 28 |
| MPL-2.0 | 14 | 0 | 14 |
| ISC | 3 | 0 | 3 |
| BSD-2-Clause | 2 | 0 | 2 |
| BSD-3-Clause | 2 | 0 | 2 |
| MIT-0 | 2 | 0 | 2 |
| BlueOak-1.0.0 | 1 | 0 | 1 |
| CC0-1.0 | 1 | 0 | 1 |

The 14 MPL-2.0 rows are `node_modules/@axe-core/playwright`, `node_modules/axe-core`,
`node_modules/lightningcss`, and the 11 `node_modules/lightningcss-*` platform variants in the
bound lock. Every one has `dev: true`. The checker must reject an MPL npm row outside
development/build/test scope. That lock scope is not proof that a future emitted bundle excludes
covered bytes; an exact bundle scan remains separate.

## Artifact boundary

This review does not cover selected wheels or sdists, emitted web bundles, app/parser/base/OS image
layers, mirrored service-image layers, or model weights/manifests. Those scopes require exact
artifact digests, inventories, obligation review, and their own notices. External Compose references
do not make service layers part of this source snapshot.

Exact-version upstream references used for the two PDF replacements:

- pypdf 6.16.2 metadata: `https://pypi.org/pypi/pypdf/6.16.2/json`
- pypdf 6.16.2 license: `https://github.com/py-pdf/pypdf/blob/6.16.2/LICENSE`
- pypdfium2 5.13.0 metadata: `https://pypi.org/pypi/pypdfium2/5.13.0/json`
- pypdfium2 5.13.0 build-license sources:
  `https://github.com/pypdfium2-team/pypdfium2/tree/5.13.0/BUILD_LICENSES`

Any change to the two bound lock digests or to this reviewed evidence requires a new policy review;
it must not be silently waived by regenerating outputs.
