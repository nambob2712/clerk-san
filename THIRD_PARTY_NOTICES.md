# Third-party source-lock reference index

This deterministic file is an informational source-lock index for the exact dependency references in `requirements.lock` and `web/package-lock.json`. It records reviewed source license classifications; it is not legal advice and does not say that dependency bytes are included in the source snapshot.

## Scope boundary

Binary wheels and sdists, the emitted `web/dist` bundle, application/parser/base/OS images, external service-image layers, and model weights/manifests require separate exact artifact inventories, review, and notices. No clearance for those scopes is inherited from this file.
The npm `production-lock-closure` label is a lock-graph scope only, not a bundle scan.

`pypdfium2==5.13.0` is classified here as `Apache-2.0 OR BSD-3-Clause` for its source project. This is a source-project classification only. Every selected pypdfium2 wheel or other binary artifact requires platform-specific review of its bundled dependency licenses and separate artifact notices before redistribution.

## Python lock references (81)

| Package URL | Normalized marker identity | Declared license | Concluded license |
| --- | --- | --- | --- |
| `pkg:pypi/aiosqlite@0.22.1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/altair@6.2.2` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/annotated-doc@0.0.5` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/annotated-types@0.8.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/anyio@4.14.2` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/asyncpg@0.31.0` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/attrs@26.1.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/blinker@1.9.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/certifi@2026.7.22` | `unconditional` | `MPL-2.0` | `MPL-2.0` |
| `pkg:pypi/charset-normalizer@3.5.1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/click@8.4.2` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/cobble@0.1.4` | `unconditional` | `BSD-2-Clause` | `BSD-2-Clause` |
| `pkg:pypi/colorama@0.4.6` | `sys_platform == "win32"` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/et-xmlfile@2.0.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/fastapi@0.141.1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/greenlet@3.5.5` | `unconditional` | `MIT AND PSF-2.0` | `MIT AND PSF-2.0` |
| `pkg:pypi/h11@0.16.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/httpcore@1.0.9` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/httptools@0.8.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/httpx@0.28.1` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/idna@3.19` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/imagehash@4.3.2` | `unconditional` | `BSD-2-Clause` | `BSD-2-Clause` |
| `pkg:pypi/iniconfig@2.3.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/itsdangerous@2.2.0` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/jinja2@3.1.6` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/jsonschema-specifications@2025.9.1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/jsonschema@4.26.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/lxml@6.1.2` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/mammoth@1.12.1` | `unconditional` | `BSD-2-Clause` | `BSD-2-Clause` |
| `pkg:pypi/markupsafe@3.0.3` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/narwhals@2.25.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/numpy@2.4.6` | `python_full_version < "3.12"` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| `pkg:pypi/numpy@2.5.2` | `python_full_version >= "3.12"` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` | `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| `pkg:pypi/openpyxl@3.1.5` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/packaging@26.3` | `unconditional` | `Apache-2.0 OR BSD-2-Clause` | `Apache-2.0 OR BSD-2-Clause` |
| `pkg:pypi/pandas@2.3.3` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/pgvector@0.5.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/pillow@12.3.0` | `unconditional` | `MIT-CMU` | `MIT-CMU` |
| `pkg:pypi/pluggy@1.6.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/protobuf@7.36.0` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/psutil@7.2.2` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/pyarrow@25.0.1` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/pydantic-core@2.46.4` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/pydantic-settings@2.15.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/pydantic@2.13.4` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/pydeck@0.9.3` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/pygments@2.21.0` | `unconditional` | `BSD-2-Clause` | `BSD-2-Clause` |
| `pkg:pypi/pypdf@6.16.2` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/pypdfium2@5.13.0` | `unconditional` | `Apache-2.0 OR BSD-3-Clause` | `Apache-2.0 OR BSD-3-Clause` |
| `pkg:pypi/pytest-asyncio@1.4.0` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/pytest@9.1.1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/python-dateutil@2.9.0.post0` | `unconditional` | `BSD-3-Clause OR Apache-2.0` | `BSD-3-Clause OR Apache-2.0` |
| `pkg:pypi/python-docx@1.2.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/python-dotenv@1.2.3` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/python-magic@0.4.27` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/python-multipart@0.0.32` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/pytz@2026.3.post1` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/pywavelets@1.9.0` | `unconditional` | `MIT AND BSD-3-Clause` | `MIT AND BSD-3-Clause` |
| `pkg:pypi/pyyaml@6.0.3` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/rapidfuzz@3.14.5` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/referencing@0.37.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/requests@2.34.2` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/respx@0.23.1` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/rpds-py@2026.6.3` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/ruff@0.16.4` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/scipy@1.17.1` | `python_full_version < "3.12"` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/scipy@1.18.1` | `python_full_version >= "3.12"` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/six@1.17.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/sqlalchemy@2.0.52` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/starlette@1.6.0` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/streamlit@1.62.0` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/toml@0.10.2` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/typing-extensions@4.16.0` | `unconditional` | `PSF-2.0` | `PSF-2.0` |
| `pkg:pypi/typing-inspection@0.4.4` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/tzdata@2026.3` | `unconditional` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/urllib3@2.7.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/uvicorn@0.52.4` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |
| `pkg:pypi/uvloop@0.22.1` | `platform_python_implementation != "PyPy" and sys_platform != "cygwin" and sys_platform != "win32"` | `Apache-2.0 OR MIT` | `Apache-2.0 OR MIT` |
| `pkg:pypi/watchdog@6.0.0` | `sys_platform != "darwin"` | `Apache-2.0` | `Apache-2.0` |
| `pkg:pypi/watchfiles@1.2.0` | `unconditional` | `MIT` | `MIT` |
| `pkg:pypi/websockets@16.1.1` | `unconditional` | `BSD-3-Clause` | `BSD-3-Clause` |

## npm lock references (183)

| package-lock path | Package URL | Declared/concluded license | Lock scope |
| --- | --- | --- | --- |
| `node_modules/@adobe/css-tools` | `pkg:npm/%40adobe/css-tools@4.5.0` | `MIT` | `development-build-test` |
| `node_modules/@asamuzakjp/css-color` | `pkg:npm/%40asamuzakjp/css-color@6.0.7` | `MIT` | `development-build-test` |
| `node_modules/@asamuzakjp/dom-selector` | `pkg:npm/%40asamuzakjp/dom-selector@8.3.2` | `MIT` | `development-build-test` |
| `node_modules/@axe-core/playwright` | `pkg:npm/%40axe-core/playwright@4.13.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/@babel/code-frame` | `pkg:npm/%40babel/code-frame@7.29.7` | `MIT` | `development-build-test` |
| `node_modules/@babel/helper-validator-identifier` | `pkg:npm/%40babel/helper-validator-identifier@7.29.7` | `MIT` | `development-build-test` |
| `node_modules/@babel/runtime` | `pkg:npm/%40babel/runtime@7.29.7` | `MIT` | `development-build-test` |
| `node_modules/@bramus/specificity` | `pkg:npm/%40bramus/specificity@2.4.2` | `MIT` | `development-build-test` |
| `node_modules/@csstools/color-helpers` | `pkg:npm/%40csstools/color-helpers@6.1.1` | `MIT-0` | `development-build-test` |
| `node_modules/@csstools/css-calc` | `pkg:npm/%40csstools/css-calc@3.3.0` | `MIT` | `development-build-test` |
| `node_modules/@csstools/css-color-parser` | `pkg:npm/%40csstools/css-color-parser@4.2.0` | `MIT` | `development-build-test` |
| `node_modules/@csstools/css-parser-algorithms` | `pkg:npm/%40csstools/css-parser-algorithms@4.0.0` | `MIT` | `development-build-test` |
| `node_modules/@csstools/css-syntax-patches-for-csstree` | `pkg:npm/%40csstools/css-syntax-patches-for-csstree@1.1.8` | `MIT-0` | `development-build-test` |
| `node_modules/@csstools/css-tokenizer` | `pkg:npm/%40csstools/css-tokenizer@4.0.0` | `MIT` | `development-build-test` |
| `node_modules/@exodus/bytes` | `pkg:npm/%40exodus/bytes@1.15.1` | `MIT` | `development-build-test` |
| `node_modules/@jridgewell/sourcemap-codec` | `pkg:npm/%40jridgewell/sourcemap-codec@1.5.5` | `MIT` | `development-build-test` |
| `node_modules/@oxc-project/types` | `pkg:npm/%40oxc-project/types@0.144.0` | `MIT` | `development-build-test` |
| `node_modules/@playwright/test` | `pkg:npm/%40playwright/test@1.62.1` | `Apache-2.0` | `development-build-test` |
| `node_modules/@radix-ui/primitive` | `pkg:npm/%40radix-ui/primitive@1.1.7` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-collection` | `pkg:npm/%40radix-ui/react-collection@1.1.15` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-compose-refs` | `pkg:npm/%40radix-ui/react-compose-refs@1.1.5` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-context` | `pkg:npm/%40radix-ui/react-context@1.2.2` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-direction` | `pkg:npm/%40radix-ui/react-direction@1.1.4` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-id` | `pkg:npm/%40radix-ui/react-id@1.1.4` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-presence` | `pkg:npm/%40radix-ui/react-presence@1.1.10` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-primitive` | `pkg:npm/%40radix-ui/react-primitive@2.1.10` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-roving-focus` | `pkg:npm/%40radix-ui/react-roving-focus@1.1.19` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-slot` | `pkg:npm/%40radix-ui/react-slot@1.3.3` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-tabs` | `pkg:npm/%40radix-ui/react-tabs@1.1.21` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-use-callback-ref` | `pkg:npm/%40radix-ui/react-use-callback-ref@1.1.4` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-use-controllable-state` | `pkg:npm/%40radix-ui/react-use-controllable-state@1.2.6` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-use-effect-event` | `pkg:npm/%40radix-ui/react-use-effect-event@0.0.5` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-use-is-hydrated` | `pkg:npm/%40radix-ui/react-use-is-hydrated@0.1.3` | `MIT` | `production-lock-closure` |
| `node_modules/@radix-ui/react-use-layout-effect` | `pkg:npm/%40radix-ui/react-use-layout-effect@1.1.4` | `MIT` | `production-lock-closure` |
| `node_modules/@rolldown/binding-android-arm64` | `pkg:npm/%40rolldown/binding-android-arm64@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-darwin-arm64` | `pkg:npm/%40rolldown/binding-darwin-arm64@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-darwin-x64` | `pkg:npm/%40rolldown/binding-darwin-x64@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-freebsd-x64` | `pkg:npm/%40rolldown/binding-freebsd-x64@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-arm-gnueabihf` | `pkg:npm/%40rolldown/binding-linux-arm-gnueabihf@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-arm64-gnu` | `pkg:npm/%40rolldown/binding-linux-arm64-gnu@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-arm64-musl` | `pkg:npm/%40rolldown/binding-linux-arm64-musl@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-ppc64-gnu` | `pkg:npm/%40rolldown/binding-linux-ppc64-gnu@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-s390x-gnu` | `pkg:npm/%40rolldown/binding-linux-s390x-gnu@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-x64-gnu` | `pkg:npm/%40rolldown/binding-linux-x64-gnu@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-linux-x64-musl` | `pkg:npm/%40rolldown/binding-linux-x64-musl@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-openharmony-arm64` | `pkg:npm/%40rolldown/binding-openharmony-arm64@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-win32-arm64-msvc` | `pkg:npm/%40rolldown/binding-win32-arm64-msvc@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/binding-win32-x64-msvc` | `pkg:npm/%40rolldown/binding-win32-x64-msvc@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/@rolldown/pluginutils` | `pkg:npm/%40rolldown/pluginutils@1.0.1` | `MIT` | `development-build-test` |
| `node_modules/@standard-schema/spec` | `pkg:npm/%40standard-schema/spec@1.1.0` | `MIT` | `development-build-test` |
| `node_modules/@tabler/icons-react` | `pkg:npm/%40tabler/icons-react@3.46.0` | `MIT` | `production-lock-closure` |
| `node_modules/@tabler/icons` | `pkg:npm/%40tabler/icons@3.46.0` | `MIT` | `production-lock-closure` |
| `node_modules/@testing-library/dom` | `pkg:npm/%40testing-library/dom@10.4.1` | `MIT` | `development-build-test` |
| `node_modules/@testing-library/jest-dom/node_modules/dom-accessibility-api` | `pkg:npm/dom-accessibility-api@0.6.3` | `MIT` | `development-build-test` |
| `node_modules/@testing-library/jest-dom` | `pkg:npm/%40testing-library/jest-dom@7.0.1` | `MIT` | `development-build-test` |
| `node_modules/@testing-library/react` | `pkg:npm/%40testing-library/react@16.3.2` | `MIT` | `development-build-test` |
| `node_modules/@testing-library/user-event` | `pkg:npm/%40testing-library/user-event@14.6.4` | `MIT` | `development-build-test` |
| `node_modules/@types/aria-query` | `pkg:npm/%40types/aria-query@5.0.4` | `MIT` | `development-build-test` |
| `node_modules/@types/chai` | `pkg:npm/%40types/chai@5.2.3` | `MIT` | `development-build-test` |
| `node_modules/@types/deep-eql` | `pkg:npm/%40types/deep-eql@4.0.2` | `MIT` | `development-build-test` |
| `node_modules/@types/estree` | `pkg:npm/%40types/estree@1.0.9` | `MIT` | `development-build-test` |
| `node_modules/@types/node` | `pkg:npm/%40types/node@24.10.1` | `MIT` | `development-build-test` |
| `node_modules/@types/react-dom` | `pkg:npm/%40types/react-dom@19.2.4` | `MIT` | `production-lock-closure` |
| `node_modules/@types/react` | `pkg:npm/%40types/react@19.2.18` | `MIT` | `production-lock-closure` |
| `node_modules/@typescript/typescript-aix-ppc64` | `pkg:npm/%40typescript/typescript-aix-ppc64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-darwin-arm64` | `pkg:npm/%40typescript/typescript-darwin-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-darwin-x64` | `pkg:npm/%40typescript/typescript-darwin-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-freebsd-arm64` | `pkg:npm/%40typescript/typescript-freebsd-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-freebsd-x64` | `pkg:npm/%40typescript/typescript-freebsd-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-arm64` | `pkg:npm/%40typescript/typescript-linux-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-arm` | `pkg:npm/%40typescript/typescript-linux-arm@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-loong64` | `pkg:npm/%40typescript/typescript-linux-loong64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-mips64el` | `pkg:npm/%40typescript/typescript-linux-mips64el@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-ppc64` | `pkg:npm/%40typescript/typescript-linux-ppc64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-riscv64` | `pkg:npm/%40typescript/typescript-linux-riscv64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-s390x` | `pkg:npm/%40typescript/typescript-linux-s390x@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-linux-x64` | `pkg:npm/%40typescript/typescript-linux-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-netbsd-arm64` | `pkg:npm/%40typescript/typescript-netbsd-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-netbsd-x64` | `pkg:npm/%40typescript/typescript-netbsd-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-openbsd-arm64` | `pkg:npm/%40typescript/typescript-openbsd-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-openbsd-x64` | `pkg:npm/%40typescript/typescript-openbsd-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-sunos-x64` | `pkg:npm/%40typescript/typescript-sunos-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-win32-arm64` | `pkg:npm/%40typescript/typescript-win32-arm64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@typescript/typescript-win32-x64` | `pkg:npm/%40typescript/typescript-win32-x64@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/@vitejs/plugin-react` | `pkg:npm/%40vitejs/plugin-react@6.0.5` | `MIT` | `development-build-test` |
| `node_modules/@vitest/expect` | `pkg:npm/%40vitest/expect@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/mocker` | `pkg:npm/%40vitest/mocker@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/pretty-format` | `pkg:npm/%40vitest/pretty-format@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/runner` | `pkg:npm/%40vitest/runner@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/snapshot` | `pkg:npm/%40vitest/snapshot@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/spy` | `pkg:npm/%40vitest/spy@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/@vitest/utils` | `pkg:npm/%40vitest/utils@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/ansi-regex` | `pkg:npm/ansi-regex@5.0.1` | `MIT` | `development-build-test` |
| `node_modules/ansi-styles` | `pkg:npm/ansi-styles@5.2.0` | `MIT` | `development-build-test` |
| `node_modules/aria-query` | `pkg:npm/aria-query@5.3.0` | `Apache-2.0` | `development-build-test` |
| `node_modules/assertion-error` | `pkg:npm/assertion-error@2.0.1` | `MIT` | `development-build-test` |
| `node_modules/axe-core` | `pkg:npm/axe-core@4.13.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/bidi-js` | `pkg:npm/bidi-js@1.0.3` | `MIT` | `development-build-test` |
| `node_modules/chai` | `pkg:npm/chai@6.2.2` | `MIT` | `development-build-test` |
| `node_modules/convert-source-map` | `pkg:npm/convert-source-map@2.0.0` | `MIT` | `development-build-test` |
| `node_modules/css-tree` | `pkg:npm/css-tree@3.2.1` | `MIT` | `development-build-test` |
| `node_modules/css.escape` | `pkg:npm/css.escape@1.5.1` | `MIT` | `development-build-test` |
| `node_modules/csstype` | `pkg:npm/csstype@3.2.3` | `MIT` | `production-lock-closure` |
| `node_modules/data-urls/node_modules/whatwg-url` | `pkg:npm/whatwg-url@16.0.1` | `MIT` | `development-build-test` |
| `node_modules/data-urls` | `pkg:npm/data-urls@7.0.0` | `MIT` | `development-build-test` |
| `node_modules/decimal.js` | `pkg:npm/decimal.js@10.6.0` | `MIT` | `development-build-test` |
| `node_modules/dequal` | `pkg:npm/dequal@2.0.3` | `MIT` | `development-build-test` |
| `node_modules/detect-libc` | `pkg:npm/detect-libc@2.1.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/dom-accessibility-api` | `pkg:npm/dom-accessibility-api@0.5.16` | `MIT` | `development-build-test` |
| `node_modules/entities` | `pkg:npm/entities@8.0.0` | `BSD-2-Clause` | `development-build-test` |
| `node_modules/es-module-lexer` | `pkg:npm/es-module-lexer@2.3.2` | `MIT` | `development-build-test` |
| `node_modules/estree-walker` | `pkg:npm/estree-walker@3.0.3` | `MIT` | `development-build-test` |
| `node_modules/expect-type` | `pkg:npm/expect-type@1.4.0` | `Apache-2.0` | `development-build-test` |
| `node_modules/fdir` | `pkg:npm/fdir@6.5.0` | `MIT` | `development-build-test` |
| `node_modules/fsevents` | `pkg:npm/fsevents@2.3.3` | `MIT` | `development-build-test` |
| `node_modules/html-encoding-sniffer` | `pkg:npm/html-encoding-sniffer@6.0.0` | `MIT` | `development-build-test` |
| `node_modules/indent-string` | `pkg:npm/indent-string@4.0.0` | `MIT` | `development-build-test` |
| `node_modules/is-potential-custom-element-name` | `pkg:npm/is-potential-custom-element-name@1.0.1` | `MIT` | `development-build-test` |
| `node_modules/js-tokens` | `pkg:npm/js-tokens@4.0.0` | `MIT` | `development-build-test` |
| `node_modules/jsdom` | `pkg:npm/jsdom@30.0.1` | `MIT` | `development-build-test` |
| `node_modules/lightningcss-android-arm64` | `pkg:npm/lightningcss-android-arm64@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-darwin-arm64` | `pkg:npm/lightningcss-darwin-arm64@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-darwin-x64` | `pkg:npm/lightningcss-darwin-x64@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-freebsd-x64` | `pkg:npm/lightningcss-freebsd-x64@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-linux-arm-gnueabihf` | `pkg:npm/lightningcss-linux-arm-gnueabihf@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-linux-arm64-gnu` | `pkg:npm/lightningcss-linux-arm64-gnu@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-linux-arm64-musl` | `pkg:npm/lightningcss-linux-arm64-musl@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-linux-x64-gnu` | `pkg:npm/lightningcss-linux-x64-gnu@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-linux-x64-musl` | `pkg:npm/lightningcss-linux-x64-musl@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-win32-arm64-msvc` | `pkg:npm/lightningcss-win32-arm64-msvc@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss-win32-x64-msvc` | `pkg:npm/lightningcss-win32-x64-msvc@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lightningcss` | `pkg:npm/lightningcss@1.33.0` | `MPL-2.0` | `development-build-test` |
| `node_modules/lru-cache` | `pkg:npm/lru-cache@11.5.2` | `BlueOak-1.0.0` | `development-build-test` |
| `node_modules/lz-string` | `pkg:npm/lz-string@1.5.0` | `MIT` | `development-build-test` |
| `node_modules/magic-string` | `pkg:npm/magic-string@0.30.21` | `MIT` | `development-build-test` |
| `node_modules/mdn-data` | `pkg:npm/mdn-data@2.27.1` | `CC0-1.0` | `development-build-test` |
| `node_modules/min-indent` | `pkg:npm/min-indent@1.0.1` | `MIT` | `development-build-test` |
| `node_modules/nanoid` | `pkg:npm/nanoid@3.3.18` | `MIT` | `development-build-test` |
| `node_modules/obug` | `pkg:npm/obug@2.1.4` | `MIT` | `development-build-test` |
| `node_modules/parse5` | `pkg:npm/parse5@8.0.1` | `MIT` | `development-build-test` |
| `node_modules/pathe` | `pkg:npm/pathe@2.0.3` | `MIT` | `development-build-test` |
| `node_modules/picocolors` | `pkg:npm/picocolors@1.1.1` | `ISC` | `development-build-test` |
| `node_modules/picomatch` | `pkg:npm/picomatch@4.0.5` | `MIT` | `development-build-test` |
| `node_modules/playwright-core` | `pkg:npm/playwright-core@1.62.1` | `Apache-2.0` | `development-build-test` |
| `node_modules/playwright/node_modules/fsevents` | `pkg:npm/fsevents@2.3.2` | `MIT` | `development-build-test` |
| `node_modules/playwright` | `pkg:npm/playwright@1.62.1` | `Apache-2.0` | `development-build-test` |
| `node_modules/postcss` | `pkg:npm/postcss@8.5.26` | `MIT` | `development-build-test` |
| `node_modules/pretty-format` | `pkg:npm/pretty-format@27.5.1` | `MIT` | `development-build-test` |
| `node_modules/punycode` | `pkg:npm/punycode@2.3.1` | `MIT` | `development-build-test` |
| `node_modules/react-dom` | `pkg:npm/react-dom@19.2.8` | `MIT` | `production-lock-closure` |
| `node_modules/react-is` | `pkg:npm/react-is@17.0.2` | `MIT` | `development-build-test` |
| `node_modules/react` | `pkg:npm/react@19.2.8` | `MIT` | `production-lock-closure` |
| `node_modules/redent` | `pkg:npm/redent@3.0.0` | `MIT` | `development-build-test` |
| `node_modules/require-from-string` | `pkg:npm/require-from-string@2.0.2` | `MIT` | `development-build-test` |
| `node_modules/rolldown` | `pkg:npm/rolldown@1.2.4` | `MIT` | `development-build-test` |
| `node_modules/saxes` | `pkg:npm/saxes@6.0.0` | `ISC` | `development-build-test` |
| `node_modules/scheduler` | `pkg:npm/scheduler@0.27.0` | `MIT` | `production-lock-closure` |
| `node_modules/siginfo` | `pkg:npm/siginfo@2.0.0` | `ISC` | `development-build-test` |
| `node_modules/source-map-js` | `pkg:npm/source-map-js@1.2.1` | `BSD-3-Clause` | `development-build-test` |
| `node_modules/stackback` | `pkg:npm/stackback@0.0.2` | `MIT` | `development-build-test` |
| `node_modules/std-env` | `pkg:npm/std-env@4.2.0` | `MIT` | `development-build-test` |
| `node_modules/strip-indent` | `pkg:npm/strip-indent@3.0.0` | `MIT` | `development-build-test` |
| `node_modules/symbol-tree` | `pkg:npm/symbol-tree@3.2.4` | `MIT` | `development-build-test` |
| `node_modules/tinybench` | `pkg:npm/tinybench@2.9.0` | `MIT` | `development-build-test` |
| `node_modules/tinyexec` | `pkg:npm/tinyexec@1.3.0` | `MIT` | `development-build-test` |
| `node_modules/tinyglobby` | `pkg:npm/tinyglobby@0.2.17` | `MIT` | `development-build-test` |
| `node_modules/tinyrainbow` | `pkg:npm/tinyrainbow@3.1.1` | `MIT` | `development-build-test` |
| `node_modules/tldts-core` | `pkg:npm/tldts-core@7.4.10` | `MIT` | `development-build-test` |
| `node_modules/tldts` | `pkg:npm/tldts@7.4.10` | `MIT` | `development-build-test` |
| `node_modules/tough-cookie` | `pkg:npm/tough-cookie@6.0.2` | `BSD-3-Clause` | `development-build-test` |
| `node_modules/tr46` | `pkg:npm/tr46@6.0.0` | `MIT` | `development-build-test` |
| `node_modules/typescript` | `pkg:npm/typescript@7.0.2` | `Apache-2.0` | `development-build-test` |
| `node_modules/undici-types` | `pkg:npm/undici-types@7.16.0` | `MIT` | `development-build-test` |
| `node_modules/undici` | `pkg:npm/undici@8.10.0` | `MIT` | `development-build-test` |
| `node_modules/vitest` | `pkg:npm/vitest@4.1.10` | `MIT` | `development-build-test` |
| `node_modules/vite` | `pkg:npm/vite@8.2.1` | `MIT` | `development-build-test` |
| `node_modules/w3c-xmlserializer` | `pkg:npm/w3c-xmlserializer@5.0.0` | `MIT` | `development-build-test` |
| `node_modules/webidl-conversions` | `pkg:npm/webidl-conversions@8.0.1` | `BSD-2-Clause` | `development-build-test` |
| `node_modules/whatwg-mimetype` | `pkg:npm/whatwg-mimetype@5.0.0` | `MIT` | `development-build-test` |
| `node_modules/whatwg-url` | `pkg:npm/whatwg-url@17.1.0` | `MIT` | `development-build-test` |
| `node_modules/why-is-node-running` | `pkg:npm/why-is-node-running@2.3.0` | `MIT` | `development-build-test` |
| `node_modules/xml-name-validator` | `pkg:npm/xml-name-validator@5.0.0` | `Apache-2.0` | `development-build-test` |
| `node_modules/xmlchars` | `pkg:npm/xmlchars@2.2.0` | `MIT` | `development-build-test` |

## Exact input bindings

- `requirements.lock`: `sha256:114917732b509616ac1a6c23b2f9b5faed4b55f39f4f5cf5a1a94271b2c540f6`
- `web/package-lock.json`: `sha256:30c583df20c2a1663a310cbc8dc67d6b4b2b828062fb1b3873093d9add7b1a39`
- `sbom/license-policy.json`: `sha256:81a577bfccb34bba7215e467817d782aca78614faf84b6bb9dad68c19c400d65`
