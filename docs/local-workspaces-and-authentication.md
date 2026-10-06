# Local workspaces and authentication

A downloaded Reveal workspace is a research folder, not an access credential. It contains a frozen question and selected context, source files, pinned authoring instructions, project configuration for Codex and Claude Code, a Python launcher and an output directory. Sharing a folder shares its scientific inputs, but does not grant access to the owner's Reveal account.

## Three connection states

| State | Available work | Sign-in and storage |
| --- | --- | --- |
| Offline | Read exact downloaded evidence through local `read_evidence`, and author local files. Missing artifacts remain unavailable. The selected CLI may still contact its own model provider. | No remote Reveal calls or credential store. Start with `--offline`. |
| Anonymous connected | Query the explicitly selected loaded data generation or connected public knowledge graphs, retain public captures, and inspect currently published scientific objects. | Default launch; no Reveal account, bearer or OS credential store required. |
| Authenticated | Read the authorized private work/context, attach public captures, capture/import evidence, upload files, validate and submit within the approved run. | Explicit registered browser consent; scoped Reveal credentials stored securely by the local helper. |

Reveal sign-in is separate from your agent provider's login. Default launch does not change the agent's model, approval, trust or permission settings. It can launch while Reveal is offline; unavailable remote tools report that limitation. The downloaded authoring kit includes lint source, but is not a complete installed offline validator. Server acceptance always uses the pinned backend runtime.

## Download and launch

Choose **Let’s close this gap → Use my local agent**, then choose either client and **Download workspace**. New archives use `reveal.local-setup/2`. They contain neither a setup ticket nor any access/refresh/device secret, and there is no launch countdown. The authenticated browser download may use an anonymous workspace session; this is distinct from requiring a registered account for contribution.

Unzip into its own folder. Run one command there:

```sh
python3 start.py codex
# or
python3 start.py claude
```

Use Python 3 and an installed, signed-in CLI. Configuration is project/session scoped: `.codex/config.toml` and `.mcp.json` invoke `python3 start.py --mcp`. No global config is rewritten and no bearer is supplied through config, command arguments or a copied prompt. `setup-manifest.json` fixes the work/request/generation/package, canonical endpoints and file checksums. The launcher rejects unsafe paths, symlinks and changed generated files. Restore damaged generated inputs from the original download or use a fresh folder while preserving authored outputs.

| Command option | Effect |
| --- | --- |
| `--offline` | Start with local `read_evidence` enabled and remote Reveal tools disabled. Sign-in, logout and downloads fail before credential or network access. `REVEAL_LOCAL_OFFLINE=1` applies the same mode. |
| `--check-only` | Verify the folder and anonymously check MCP without starting a model. |
| `--login` | Begin browser device sign-in and approval; no model run. |
| `--logout` | Disable this installation's authenticated access and revoke its connection; keep files and public access. |
| `--download-public-capture CAPTURE_ID` | Save exact anonymous capture bytes under `evidence/public/` before expiry; no model run or sign-in. |
| `--materialize-evidence output/evidence-selection.json` | Queue/resume authenticated export and download a verified complete selected closure under `evidence/closures/`; no model run. |

For example, `python3 start.py claude --login` connects an existing folder. Reconnection does not require downloading a new ticket or replacing files.

## Browser approval and anonymous ownership

The helper exposes `connect_reveal`, `get_reveal_connection` and `disconnect_reveal`. `connect_reveal` starts a device request and returns a browser link and visible user code. It never asks the researcher to paste a token. After the researcher approves, `get_reveal_connection` respects the server polling interval and obtains the resulting connection. The raw device secret remains outside the agent's tool result.

The browser opens `/research/connect?user_code=...`, or the researcher can enter the code at `/research/connect`. A native OAuth client opens the same page with `request_id` after the authorization endpoint validates its registered callback and PKCE challenge. These are lookup handles, not ownership proofs.

The page requires registered Google/ORCID sign-in before reading/approving the request. Signing in is not consent. It displays the client name and ID, resource, scopes, callback when applicable, and the research selection. Client names from public registration are self-reported, so inspect the request you started. Missing configured providers are shown as unavailable; there is no anonymous approval path.

If research was downloaded from an anonymous browser workspace, select **Move my anonymous research** after sign-in. That explicit action uses the existing session claim flow, fresh verified login and the browser's anonymous session. It preserves scientific identity and attribution. A code, copied archive or work ID cannot claim another person's workspace. Approval is disabled until the selected work is actually owned, ready and unexpired. A device request naming a particular work cannot be switched to another run. A generic PKCE client requires a work selection.

Only **Approve connection** authorizes the agent. Loading, refreshing, choosing an agent or checking status creates no grant. **Decline** refuses access. For authorization-code clients, the UI navigates only the backend-returned registered callback, preserving code/error, state and issuer; it never accepts a callback supplied in the page query. Provider tokens stay in the registered identity flow and are never forwarded to the agent or research token endpoint.

## OAuth and credentials

The canonical backend publishes:

- `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource[/mcp]`.
- `/oauth/register` for public-client registration, `/oauth/authorize` for authorization code with S256 PKCE, and `/oauth/device_authorization` for device requests.
- `/oauth/token` for initial tokens and rotating refresh, and `/oauth/revoke` for connection-family revocation.
- `/v1/research-oauth/consent` for registered-browser lookup and an explicit approval/denial decision.

Registration is JSON; device/token/revoke use `application/x-www-form-urlencoded`. Public-client token authentication is `none`, with mandatory exact `client_id` and MCP `resource` binding. Authorization-code callbacks are exact registered HTTPS or HTTP loopback URLs (`localhost`, `127.0.0.1`, `::1`); custom schemes and implicit grants are unsupported. PKCE must use S256. Clients must register `refresh_token` explicitly if they need it; the fixed `reveal-local-launcher` client supports device plus refresh.

The available scopes are `research:read` and `research:write`. Read permits this run's private inputs, retained evidence and authorized scientific context. Write adds evidence capture/import, validation and submission and requires read. Neither allows publishing, changing ownership, running hosted jobs or inspecting unrelated private operational records.

Requests/device codes expire after ten minutes; approved authorization codes after one minute. Access tokens last at most fifteen minutes. Refresh families last at most thirty days, bounded by the work lifetime. Refresh tokens rotate on each use; reuse of a consumed refresh token revokes the family. Do not blindly retry a consumed token after an ambiguous response. Reauthorize safely when a token exchange cannot be recovered. Closing/expiry, revocation, ownership changes or principal invalidation can end access sooner. Invalid supplied credentials never silently downgrade to anonymous.

The local helper stores secrets in macOS Keychain or Linux Secret Service (`secret-tool`), with no plaintext fallback. Anonymous access does not initialize that store. Project-local installation state is metadata, not a token file; raw credentials stay out of process arguments, project configuration, chat, outputs and logs. Unsupported authenticated storage fails explicitly. New connections do not revoke unrelated installations. Disconnect disables local authenticated use before a network request; if server revocation fails offline, retry disconnect later. Disconnecting does not erase local files or accepted science.

Direct remote MCP is also available to native clients: anonymous public tools require no helper, and protected operations advertise the OAuth resource challenge. Native PKCE clients handle their own secure tokens, renewal and file transfers. The bundled stdio helper provides equivalent authentication/transfer behavior for Codex and Claude Code without depending on differences in their native remote OAuth support.

## Public evidence becomes scoped evidence only by attachment

Anonymous data queries name the exact retained reference generation. The helper fills that value from the frozen manifest. Data never silently changes to the active/newest generation. Public science lookup returns current public publication snapshots only; private drafts, uploads, research inputs, accounts and borrowed closures are not anonymous data.

Public reference and connected-graph queries can return `reveal.public-reference-capture/1` with a content-addressed capture ID, raw response hash, source/operation/arguments, materialized context, result/coverage and artifact descriptors. The default public retention is **seven days**, configurable by the operator; every response supplies its actual `expires_at`. `get_public_capture` replays retained bytes without another source query. Artifact downloads are anonymous at `/v1/public-research/captures/{capture_id}/artifacts/{artifact_sha256}`, with explicit size/hash checks. They are limited to verified public reference captures and never serve private work inputs or independent user uploads.

Before the stated expiry, authenticate and call `attach_public_captures` with one to ten selected capture IDs, the work ID and an idempotency key. The backend verifies its own retained manifest, exact bytes, capture source and owner, then checks the source-specific authorization before creating work-scoped evidence receipts: reference captures must match the **frozen reference generation**; connected-graph captures must name a graph in the frozen **`request.composer.selected_kgs`**. Caller-supplied checksums do not substitute for server retention. Attachment can use exact retained historical bytes after source tables become unavailable; it does not issue a fresh source query or reinterpret them as current data. Existing authenticated attachments continue to work after public capture expiry. Unattached expiry returns `PUBLIC_CAPTURE_EXPIRED`; generation mismatch returns `CAPTURE_GENERATION_MISMATCH`.

Connected graph discovery uses `list_knowledge_graphs`; `describe_kg` and `get_schema` take a graph ID. The supported graphs are `biomarkerkg` and `prokn`, served through the fixed Proto-OKN upstream. `query_graph` offers the existing online bounded triple-query fields, with absolute IRIs, exact predicate/literal lookup or subject/predicate-bound text discovery. Local `sparql_query` accepts a read-only SELECT string and a result limit from 1 to 100 (default 25), confined to the chosen literal named graph `<https://purl.org/okn/frink/kg/biomarkerkg>` or `<https://purl.org/okn/frink/kg/prokn>`. PREFIX declarations are allowed; SERVICE, FROM/FROM NAMED, unscoped patterns, graph variables, updates and custom extension functions are rejected. Neither tool accepts an endpoint override. Anonymous browsing requires no Reveal login, but capture attachment and citation in a submitted account require the graph to have been selected when that work was frozen. Anonymous browsing cannot broaden an existing run's graph policy. Authenticated queries include `research_request_id` and `idempotency_key`; poll their operation to completion before citing the captured result. Existing hosted graph tools and query budgets are unchanged.

Graph captures preserve the original upstream envelope and generated or supplied query, exact result rows, graph/source identity, observation time and byte hashes. Their `source_mode` is `external_kg`; their scientific source is generation-independent, distinct from CFDE imported factors and graph/context records, with an explicitly unknown upstream release. `query_graph` preserves complete triples; custom SELECT preserves its variable names and typed RDF bindings, including supported projections or aggregates. Interpret those bindings only under the captured query semantics; missing columns do not imply unseen triples or qualifiers. Matching a label or receiving a KG result establishes neither CFDE ancestry nor entity equivalence. Schema and description metadata are discovery, not scientific evidence; only relevant captured observations with verified identity and scope may support a claim. No rows, an upstream error or a timeout is not evidence of biological absence. Preserve exact captures and attach them before their explicit expiry; replay or attachment never performs a fresh graph query.

Public scientific objects use a different path: choose exact payload/source observations and call authenticated `reuse_scientific_objects`. Public read access is not a durable reuse grant. The service rechecks current authority, citation metadata and required dependency closure at selection, acceptance and later borrowed reads/publication. Withdrawal can remove future authorization despite retained audit bytes. Preserve original authorship and do not treat repeated use of one source as independent corroboration.

The reference-data boundary is unchanged: loaded CFDE/PIGEAN/EAGGL records plus exactly two opt-in BioIndex phenotype operations, fixed to model `small`, sigma `2` and the configured allowlisted deployment/indexes. They stay gated until deployment compatibility is verified. Connected BiomarkerKG/Proto-OKN reads are a separate existing graph-evidence path with their own frozen selection policy. Anonymous access does not enable arbitrary URLs, SQL, other BioIndex indexes, external identifier lookup, or an automatic reference-data fallback.

## Durable evidence delivery

Public downloads preserve evidence for offline inspection; server attachment and reuse remain separate authorization decisions. Export cannot recover an expired capture whose original retained bytes are gone. Never reconstruct missing original captures or represent a newly issued query as their replay.

Authenticated `export_evidence_context` queues a durable operation and returns immediately. Poll `get_operation`; its successful `reveal.validation-context-export/2` result identifies the exact seed, selected receipts/imports/reuse, full context and authorized immutable artifacts. Retry the same selection/key after interruption to resume one logical export. Authorization is rechecked at commit and download, including inherited reuse dependencies. Validation reconstructs the full selected closure even when only missing artifacts need transfer.

The local `materialize_evidence_context` tool and `--materialize-evidence` command verify and atomically save the package, every required original source and a manifest under `evidence/closures/<package-sha256>/`. Repeating a pending command resumes its operation. Existing files are checked, not assumed valid. Originals remain readable using `read_evidence` offline without network calls or new scientific observations. See the [selection-file example and exact replay commands](local-agent-mcp.md#export-and-offline-replay).

Materialization reports complete selected evidence separately from per-account citation completeness for `output/account*.json`. Each account report includes its draft hash and missing File/dependency IDs. Preserve that status with the account delivery: selected descriptors or uploaded drafts alone do not establish offline reproducibility.

`validate_submission` is a protected write that retains private validation results and normalized artifacts. It neither accepts accounts nor publishes. `submit_accounts` separately accepts private results; publication remains separate. Downloaded schema/lint source does not install an offline validator, and offline mode cannot perform server validation or submission.

## Compatibility and verification

Legacy manual issuance and setup-ticket exchange/revocation remain deprecated compatibility paths. New manual issuance requires a registered principal (`403 SIGN_IN_REQUIRED` otherwise). Old anonymous-issued or unmarked manual grants cannot authorize protected access after identity promotion (`403 REGISTERED_CONSENT_REQUIRED`). Legacy tickets are redeemable only when their persisted issuance records prove `issued_principal_kind=registered`; anonymous-issued and unmarked tickets return `403 SIGN_IN_REQUIRED` even after promotion. Use a current credential-free workspace and fresh OAuth consent instead. New v2 workspaces and UI do not mint tickets or manual bearer connections. Online agents retain their separate execution-fenced path and shared scientific validation; they do not use browser device consent. Local research remains in **Workspace → Research runs**, with private accepted/reused results and explicit optional hosted statements.

See [local operation and test commands](local-agent-mcp.md#oauth-and-provenance-checks), the generated [OpenAPI contract](../api/openapi.yaml), and focused backend/frontend suites for recorded verification. Mocked browser checks do not establish real provider-login integration, actual CLI research quality, or production deployment readiness.
