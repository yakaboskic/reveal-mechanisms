# DIG service platform deployment

The administrator directed REVEAL to `broadinstitute/dig-service-platform` instead of a separately provisioned EC2 host. The previous EC2/IAM setup is on hold. Keep the personal Vercel frontend project and preserve the original RDS tables and S3 artifacts.

## Access required

The active GitHub account is `yakaboskic`, with `repo` scope. Both `gh repo view broadinstitute/dig-service-platform` and `git ls-remote git@github.com:broadinstitute/dig-service-platform.git HEAD` returned repository-not-found errors. The URL cannot currently be inspected. The administrator needs to grant repository access or confirm the link before we can validate its configuration or prepare a platform PR.

Message to the administrator:

> Thanks, let's use the platform. Could you give my GitHub account `yakaboskic` access to `broadinstitute/dig-service-platform`? Both the GitHub API and Git currently return “Repository not found.” REVEAL needs an API, two background workers, a dispatcher, and private Redis; we'll check the platform template for the supported way to run those together.

## Requirements to map against the actual repository

The following integration details come from the administrator's supplied instructions; they have not yet been checked against repository files.

- Proposed service name: `reveal`; proposed route prefix: `/api/reveal`. The listener priority must be selected from the actual registry, not guessed.
- Adapt every API route, including authenticated internal gateway operations, to `SERVICE_PATH_PREFIX`. Expose `<prefix>/health`; retain the current local URLs when the prefix is unset. Preserve query validation, authorization, cache-control rules, SSE, and S3 redirects under the prefix.
- Keep the existing Python image and scientific reference assets reproducible. The deployment image already exposes port 8000 and includes curl. Confirm how the platform build obtains the application source and pinned assets without committing credentials, private evidence, or local runtime files.
- Run the dispatcher and two workers as supported background processes/tasks, with a shared private Redis endpoint. An API-only template does not establish that these are supported. Inspect the service schema and deployment module before choosing sidecars, separate tasks, or another layout.
- Preserve bounded RAM-backed temporary work and S3 for durable artifacts. Confirm the platform launch type supports the required temporary filesystem configuration; do not silently replace it with persistent or ephemeral disk storage.
- Retain the original database/schema and `reveal_*` tables, existing OAuth client IDs, and existing account/publication identities. Cloud writes use `prod/`; existing `local/` artifact versions remain readable.
- Inspect QA/production workflow behavior before enabling consumers. They must not inadvertently launch two independent application stacks against the same primary records and queue. The user requested one deployed application for now, without a staging label in the public frontend URL.
- Keep the frontend in personal Vercel project `reveal-mechanisms`, domain `reveal-mechanisms.vercel.app`. Its server-side backend URL will include the platform's actual host and `/api/reveal` prefix. Register the production OAuth callbacks after that configuration is confirmed.

## Work after access is available

Read the platform's repository instructions, template, service configuration schema, registry, workflow, tests, and deployment modules. Prepare the `reveal/` integration with its supported worker/Redis arrangement; run platform validation and tests plus local prefix/auth/SSE checks. Review the concrete change before deploying against the shared database. Drain the current local pool only when the replacement is ready.

No platform service files, registry entries, CI jobs, pull requests, or deployments have been created yet. The local application remains the active instance.
