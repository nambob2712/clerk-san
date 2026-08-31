# Clerk-san source dependency SBOM

`source-dependencies.spdx.json` is a deterministic SPDX 2.3 inventory generated from
`requirements.lock`, `web/package-lock.json`, and the reviewed source-reference policy in
`license-policy.json`. The policy's self-contained public evidence is
`source-license-review.md`:

```bash
SOURCE_DATE_EPOCH=0 scripts/generate-sbom.sh
python3 scripts/generate-third-party-notices.py
python3 scripts/check-license-policy.py
```

The canonical policy and the machine block in `source-license-review.md` bind both lock SHA-256
digests, both complete composite-identity sets, and the machine policy projection. Every installable
Python lock block must be an exact hash-locked `name==version` reference; unsupported forms such as
direct URLs fail closed. The policy explicitly enumerates all 81 Python package URLs plus normalized
marker identities and derives all 183 npm package URLs, exact `packages` paths, declared licenses,
and lock scopes from the digest-bound package lock. Waivers, when present, require canonical
dedicated evidence at `sbom/waivers/<waiver-id>.json`; that file and its digest bind the exact waiver
identity, violation, expression, owner, reason, and expiry. Evidence locators cannot escape the
repository or traverse symlinks. The checker fails on lock/policy/evidence/SBOM/notice drift,
unresolved or incompatible licenses, invalid waivers, and MPL npm rows outside
development/build/test scope. SPDX download locations are deterministic
exact-version PyPI metadata URLs for Python and the exact safe HTTPS `resolved` URLs from the npm
lock; missing or unsafe npm locations fail closed. `THIRD_PARTY_NOTICES.md` is the deterministic
informational index for these source-lock references.

These files are deliberately source-reference evidence. They do not prove or clear selected Python
wheels or sdists, the emitted web bundle, application/parser/base/OS images, external service-image
layers, or model weights/manifests. Those artifacts need separate exact inventories, license review,
and notices. In particular, `pypdfium2` is classified here as `Apache-2.0 OR BSD-3-Clause` at source
scope; each selected platform wheel still requires review of its bundled dependency licenses.
