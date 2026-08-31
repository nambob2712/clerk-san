# Security policy

Clerk-san is a source-only, local-first developer preview. We accept private reports about the current
default branch, but do not promise production support, a versioned security-maintenance window, or a
response-time service level.

## Report a vulnerability privately

GitHub private vulnerability reporting is this project's only advertised security-reporting route:

1. Open this repository on GitHub and select the **Security** tab.
2. Select **Report a vulnerability**.
3. Submit the report through the private advisory form.

Do not disclose a vulnerability in a pull request, commit, public discussion, or issue. If the private
reporting button is unavailable, do not post the details publicly; private vulnerability reporting must
be enabled by the repository owner before this preview is published. No fallback security email is
advertised.

## Keep evidence synthetic and private

Never submit a real receipt, invoice, personal document, customer record, database, runtime storage,
local log, credential, token, or model input/output containing private information. Do not include the
contents of an exposed secret. Reproduce the problem with the smallest synthetic example possible and
remove identifying metadata from screenshots and traces.

A useful report includes:

- the affected commit and component;
- the prerequisites and minimal reproduction steps;
- the expected and observed security boundary;
- the potential impact and whether the issue is already being exploited; and
- a synthetic proof of concept or sanitized diagnostic output, when safe.

## Security boundary

The advertised runtime is loopback-only and has no authentication for public network exposure.
Binding it to a public interface, treating the developer preview as production-ready, or relying on it
as a legal or regulatory compliance guarantee is unsupported.

Third-party dependencies, model runtimes and weights, base images, and operating-system packages are
maintained under their own projects and licenses. Report their upstream flaws to the appropriate
maintainer; also report privately here when Clerk-san's integration or configuration creates a distinct
exposure.

Please allow the maintainer to triage and coordinate a fix before public disclosure. The maintainer may
ask for additional synthetic evidence, agree on a disclosure date, or determine that the report belongs
upstream. Private reporting does not itself establish production readiness, certification, or a
compliance guarantee.
