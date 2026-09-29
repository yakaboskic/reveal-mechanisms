# Colleague implementation handoff

Updated September 30, 2026. The application exists; this is a handoff to run and extend it. The former implementation checklist is retained in Git history and the dated design plans.

1. Clone the `main` branch and follow [README.local.md](../README.local.md) with the encrypted environment and separately delivered password. Setup is agent-friendly and uses shared development credentials; existing handoff-branch clones can switch to `main` using the README's update instructions.
2. Confirm `http://localhost:3000` and API readiness, then follow the [API walkthrough](api-quickstart.md). Read-only examples do not submit research.
3. Read [implementation status](implementation-status.md), [local deployment](local-deployment.md) and the scientific [linting](scientific-account-linting.md)/[review](scientific-review.md) boundaries before changing workers.
4. Use the [documentation index](README.md) for specialized contracts and historical decisions.

## Preserve these boundaries

- The original RDS `reveal_*` records contain real users, drafts, scientific accounts and publications. Never reset or replace them for setup. The colleague queue namespace separates dispatch and drain controls, not scientific data or ownership.
- API, dispatcher and workers use the same Docker backend image. Authoring and final acceptance share source-validation/lint code. Final trusted assembly hydrates only schema-declared references, never an identifier merely mentioned in prose.
- Keep provenance, scientific digests, artifact versions and checksums intact. No cached fixture is a production scientific result.
- Next.js owns sessions; FastAPI independently checks assertions and ownership. Secrets remain server-side. A colleague's login is their own identity, not Chase's session.
- RDS owns durable dispatch and lease state; Redis delivery is reconstructible. Durable files belong in S3; ephemeral work uses container tmpfs.
- A retry after scientific failure must follow the explicit workflow. Never mark a failed job accepted to make the UI appear successful.

## Current next deployment step

The administrator proposed Broad's DIG service platform. Repository access for `yakaboskic` is pending. Once available, inspect its actual service template and support for the API, dispatcher, Redis and worker pool before implementing the platform service. Keep the Vercel frontend in the selected personal workspace. The separate EC2 IAM request is on hold; do not apply it as part of colleague setup.

See [platform deployment](platform-deployment.md) and [validation report](validation-report.md). Local validation does not certify cloud networking, IAM, provider callbacks at new domains, or a new scientific run.
