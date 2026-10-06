# Local and online research through MCP

Reveal exposes a stateless Streamable HTTP MCP endpoint at `/mcp`, alongside its existing API. Local and online research begin with a small immutable seed. Agents retrieve loaded factor, loading, gene, gene-set, membership, trait and imported-graph observations on demand. Existing eager captures remain readable.

## Try a local agent

1. Open Reveal, choose a knowledge gap and factor anchors, then open **Let’s close this gap** and select **Use my local agent**.
2. After preparation, choose **Codex** or **Claude Code** and **Download workspace**. The credential-free `reveal.local-setup/2` ZIP includes the exact frozen seed/source bytes, pinned authoring files, both project configurations and `start.py`. Downloading creates no connection grant or model run.
3. Unzip into its own folder and run `python3 start.py codex` or `python3 start.py claude` there. Python 3 and the installed, signed-in agent CLI are required. The launcher verifies the files and starts your agent with anonymous public Reveal tools. Reveal sign-in and a credential store are not required for this step. Your agent retains its normal model, login, trust and permission settings.
4. Explore loaded public data, the connected BiomarkerKG/Proto-OKN graphs, and currently published scientific accounts, Propositions and Claims. Keep exact capture IDs, payload selections, hashes, source identities and coverage. Download source bytes before each capture's stated expiry.
5. When ready to validate, upload or submit, ask the agent to call `connect_reveal`, or run `python3 start.py codex --login` / `python3 start.py claude --login`. Sign in through Reveal's Google/ORCID flow and explicitly approve the agent, scopes and research. If the run belongs to your anonymous browser workspace, explicitly move that workspace into the signed-in account first. Knowing a code or work ID does not grant ownership.
6. After approval, the helper obtains and securely stores Reveal credentials, attaches selected public captures under the frozen reference-generation or selected-graph policy, and supports server validation/submission. Return to the run page for validation reports and accepted/reused accounts. Local submissions stay private and do not generate hosted statements automatically.

There is no setup ticket or download countdown. Keep using the same folder on later visits. `--check-only` verifies files and tests anonymous MCP without a model; `--offline` keeps the local `read_evidence` tool available while disabling remote Reveal calls. Missing local artifacts fail explicitly; the reader never fetches them offline. `REVEAL_LOCAL_OFFLINE=1` applies the same restriction to every launcher command. Offline mode rejects sign-in, logout and downloads before accessing credentials or the network. Default launch does not require an online backend. `--login` and `--logout` are connection actions and do not start a model. Without offline mode, `--logout` disconnects this installation while preserving files and anonymous public access. If Reveal is unreachable during logout, local authenticated use is disabled immediately and server revocation can be retried when connected.

The downloaded Codex and Claude Code configurations both use `python3 start.py --mcp` as a local stdio helper. The helper connects to remote `/mcp`, supplies the frozen generation/work context, handles OAuth and transfers, and keeps raw credentials out of model-visible tool results. `connect_reveal` returns a browser link/code without blocking an agent session; `get_reveal_connection` checks the approval status; `disconnect_reveal` disconnects. Authenticated storage supports macOS Keychain and Linux Secret Service (`secret-tool`); unsupported storage fails explicitly, without a plaintext fallback. This storage requirement applies to sign-in, not anonymous work.

**Manual setup and connection controls** exposes these commands, the research prompt and revocation of existing connections. New UI no longer creates manual bearer credentials. Other clients can use the remote public `/mcp` directly and its standard OAuth authorization-code flow with S256 PKCE. They must handle their own file transfers and secure credential storage. See [workspaces and authentication](local-workspaces-and-authentication.md) for the full boundary and protocol.

Return to **Workspace → Research runs** for local and online runs together. Lifecycle/submission events update the list. The old `/local-runs` history URL redirects to `/workspace?tab=runs`; each local detail page remains `/local-runs/{id}`. Validation-only candidate accounts are not presented as saved findings.

## Canonical URLs and online execution

The local Workflow deployment publishes the API at `http://127.0.0.1:18001`; its MCP URL is `http://127.0.0.1:18001/mcp`. `REVEAL_PUBLIC_API_URL` overrides the canonical base, including an optional deployment path prefix. If unset, the base derives from `REVEAL_WORKFLOW_URL`, otherwise falls back to `http://127.0.0.1:18000`. Downloaded workspace return links use `REVEAL_PUBLIC_WEB_URL`, then `REVEAL_CANONICAL_URL`, then `NEXTAUTH_URL`, in that order. These are trusted server settings; request Host/Origin headers never select the browser destination. A public API tunnel may use a loopback browser URL such as `http://localhost:3100`. Deployment generators set the browser URL explicitly to the chosen frontend origin and port. With no browser setting, only a loopback API retains the legacy `http://localhost:3000` default; a remote API fails configuration. HTTPS is required except for loopback browser URLs. Credentials, query strings, fragments and malformed URLs are rejected, including invalid higher-priority settings rather than silently falling back.

Hosted Boxes must reach that backend over public HTTPS. To run a hosted agent against a laptop backend, configure an HTTPS tunnel before creating research work. Loopback works for a local agent but cannot be reached by a remote Box. Online execution still uses the existing job/Workflow/Box flow, shared progressive data/reuse services, execution-fenced credentials and trusted output collector. Hosted credentials cannot directly upload or accept account results. New online accounts retain the existing paragraph policy; reuse-only outcomes do not generate duplicate paragraphs.

## Evidence and reuse

Public loaded-data operations require an exact retained `reference_generation_id`; the downloaded helper supplies it. They never fall back to a newer import or an external reference service. Authenticated queries use the work's pinned generation. Only `get_pigean_gene_phenotype` and `get_pigean_gene_set_phenotype` may use BioIndex, fixed to `bioindex.hugeamp.org`, model `small`, sigma `2`, and the `phenotype,2,small` signature. Other model/index/host overrides are rejected.

Those two external operations remain unavailable until deployment access and index signatures are verified. The original development check returned HTTP 403. Do not set `REVEAL_SMALL_PHENOTYPE_VERIFIED=true` merely to bypass that result. Unavailable responses cannot support claims, and source verification is not triggered by an agent query.

Anonymous reference results include portable, exact server-retained captures with an explicit `expires_at`, by default seven days. `get_public_capture` replays them without another scientific query. Its artifact URLs serve only verified public reference bytes, never private seed inputs, user uploads or reuse closures. After sign-in, `attach_public_captures` verifies and attaches one to ten selected captures to a ready work item with the **same frozen generation**. It preserves the original source bytes and metadata, including separately identified small-model observations, without re-querying a source. Already attached evidence remains retained after public capture expiry. Unattached expired captures fail explicitly; downloaded bytes or caller-supplied hashes do not bypass that rule.

### Connected knowledge graphs

Local agents can also read the same connected knowledge graphs as online agents. `list_knowledge_graphs` discovers the fixed `biomarkerkg` and `prokn` connections; `describe_kg` explains a graph's scope and `get_schema` inspects its schema. These use the server-fixed upstream `https://apps.okn.us/okn-mcp-dev/mcp` and fixed named-graph IRIs. They are distinct from the imported graph/context records in CFDE/PIGEAN/EAGGL reference generations.

Use `query_graph` with `graph` and bounded typed fields `subject`, `predicate`, `object`, `literal`, `contains`, `limit`. The current online-parity query builder accepts absolute entity/predicate IRIs and limits from 1 to 100 (default 25); begin with a small relevant query. For exact label/symbol discovery, use a schema-derived predicate plus a case-sensitive `literal`. `contains` needs a bound subject or predicate, and cannot scan the entire graph. Callers cannot replace the upstream endpoint or pass an arbitrary SPARQL string through this bounded tool.

For a custom read, use local `sparql_query` with `graph`, `query` and optional `limit` (1–100, default 25). Only read-only SELECT is supported, and every graph pattern must be scoped to the chosen fixed literal named graph: `<https://purl.org/okn/frink/kg/biomarkerkg>` or `<https://purl.org/okn/frink/kg/prokn>`. PREFIX declarations are allowed. The server rejects SERVICE, FROM/FROM NAMED, unscoped graph patterns, graph variables, updates, custom extension functions and endpoint overrides. Public calls use these flat arguments; authenticated calls also supply `research_request_id` and `idempotency_key`, then poll the returned operation with `get_operation` before using its result.

For example, first inspect the graph schema, then send a bounded read using the [SPARQL named-graph syntax](https://www.w3.org/TR/sparql11-query/#namedGraphs):

```json
{
  "graph": "prokn",
  "query": "SELECT ?s ?p ?o WHERE { GRAPH <https://purl.org/okn/frink/kg/prokn> { ?s ?p ?o } }",
  "limit": 5
}
```

This example is a discovery sample, not evidence for a particular biological mechanism. The server wraps the original SELECT as a bounded subquery and retains both query texts.

Anonymous graph reads retain exact upstream result envelopes, the generated or supplied query, graph identity, observation time, hashes and source locators as public captures. Keep those bytes and capture IDs; a later graph change must not replace the observed result. Before using an anonymous graph capture in a submitted account, sign in and call `attach_public_captures`. The source graph must be in the frozen `request.composer.selected_kgs`. Browsing a graph anonymously does not select it for an existing run; to change the selection, prepare new research. Authenticated graph queries enforce the same selection, and the existing online graph proxy and its budgets remain unchanged.

Connected graph observations use `source_mode=external_kg`, with `source.graph_id`, `named_graph`, upstream request/server/origin and observation time; they do not claim a CFDE import generation or a known upstream release. Graph attachment checks the selected graph rather than requiring a reference-generation match. `query_graph` yields complete triple rows; `sparql_query` preserves the returned SELECT variables and typed RDF bindings, including supported projections or aggregates. A projected binding or aggregate supports only its captured query semantics: do not invent omitted triples, count semantics or qualifiers. Schema/description metadata, errors and empty reads are not biological findings or evidence of biological absence. Verify entity identity and biological scope before citing a row; graph membership alone establishes no CFDE ancestry or causal direction. The same explicit public expiry/retention rule applies, and already authenticated attachments retain their exact bytes.

Anonymous scientific search and exact-object reads see only current public publication snapshots. Authenticated research may also inspect its authorized owner-visible science. Search uses structured/lexical matching, not an embedding similarity index. Reading public science creates no permanent reuse right. `reuse_scientific_objects` rechecks the exact payload, source observation, dependency closure, citation metadata and current authority. Withdrawal blocks further borrowed reads/reuse/publication; retained hashes do not override access. Reuse-only results preserve original identities/authorship and do not create another statement.

Independent evidence uploads require authenticated write scope and retain original bytes and extracted records. Locally derived imports identify retained inputs and a declared method; that declaration does not prove Reveal executed the process. Every Claim needs eligible scientific support. Missing CFDE ancestry remains an account-level advisory even under strict lint.

## Export and offline replay

Begin with `input/evidence-index.json`. `read_evidence` accepts an artifact ID and frozen SHA-256 plus an exact JSON Pointer or bounded text range. Its `content_json` string preserves original numeric tokens. The original source hash, locator, paging and truncation accompany each response. Reading retained bytes consumes inspection limits, not scientific-query budget, and creates no new observation. No recursive `evidence-records/` tree is generated. Use `mode=text` for lines or `mode=text_range` for Unicode character ranges, including a JSON string selected by `pointer`. Character offsets are zero-based Unicode code points with an exclusive end; returned locators distinguish decoded string positions from original UTF-8 byte ranges. `complete_selection=true` means the entire selection was returned in this response. A terminal page with `truncated=false` can still be a suffix; it does not establish complete scientific-query coverage.

Before anonymous captures expire, use local `download_public_capture` or `python3 start.py --download-public-capture CAPTURE_ID`. Exact originals and their manifest are saved under `evidence/public/<capture-id>/`. Downloading does not attach a capture, grant reuse permission or validate an account. Protected contribution still requires sign-in and `attach_public_captures` while the server capture remains available.

`export_evidence_context` now immediately returns an `operation_id`. Poll `get_operation` until `succeeded`, or inspect the terminal error. Repeating the same selection and idempotency key resumes the same operation across process restarts; changed input needs a new key. The successful result is `reveal.validation-context-export/2`: seed and full-package/context hashes, the selected receipt/import/reuse IDs, a package artifact and a compact manifest. Original source artifacts are reused, with owner and dependency authority rechecked before result commit and later downloads. Neither export nor replay re-queries a scientific source.

For the local helper, select **all** cited receipts, imports and reuse receipts in `output/evidence-selection.json`:

```json
{
  "receipt_ids": ["RECEIPT_ID"],
  "import_ids": [],
  "reuse_receipt_ids": [],
  "idempotency_key": "evidence-delivery-1"
}
```

Run `python3 start.py --materialize-evidence output/evidence-selection.json`, or call `materialize_evidence_context` with those fields. If the operation is pending, repeat the same command/key. Once complete, the helper atomically writes `evidence/closures/<package-sha256>/evidence-package.json`, original source artifacts and `manifest.json`. Every required artifact is verified, including existing local bytes; only missing verified checksums are transferred. Changed local bytes fail rather than being silently trusted. The local reader registers these artifacts for `python3 start.py codex --offline` or `python3 start.py claude --offline`.

The result distinguishes `selected_evidence_present_locally` from each `accounts[]` entry's `cited_evidence_present_locally`. Account entries inspect `output/account*.json`, report its draft hash, and traverse Claims, EvidenceItems and reused source Claims to cited Files. Missing files or unresolved dependencies remain explicit. A complete selected closure does not imply that an account selected every cited source. Include this per-account status in delivery; a File descriptor alone is not offline evidence.

Hosted draft writing and linting consume the same complete closure and verified source inventory. Frozen v1 exports remain readable by the updated consumer. Existing downloaded workspaces and their manifests remain immutable: obtain a newly generated kit to use this reader/launcher contract.

Hosted research queries return durable operation IDs promptly; use `get_operation` to observe them. Operation observations use a separate bounded inspection allowance. If an acknowledgement is unavailable, repeat the identical tool arguments: the harness preserves its deterministic retry key. Observed IDs are retained in a scoped execution journal, and responses arriving after the caller's deadline cannot add evidence receipts. Hosted materialization shares a deadline across export, polls and downloads, reuses verified original seed files, and retains completed downloads for the same draft/lint retry. Errors retain the observed export ID. Hosted lint has a 55-second overall budget below its 60-second caller deadline, including materialization.

`validate_submission` retains private validation results and normalized artifacts; it does not accept accounts or publish. `submit_accounts` is the separate protected acceptance action. The downloaded schema and lint source are not an installed offline validator. A rehearsal can stop after validation and repair without submitting.

## Runtime and compatibility

`POST /v1/local-work/{id}/setup-kit` accepts `{"client":"codex"}` or `{"client":"claude_code"}` through the owner-authenticated browser gateway, including an anonymous workspace session. It returns `application/zip` with `Cache-Control: private, no-store`. The process caches bounded credential-free bytes and reads retained artifacts concurrently; ownership and package binding are rechecked after preparation. New archives contain no `setup.json`, ticket, access or refresh token. Private scientific inputs in an archive still need appropriate handling when shared.

OAuth discovery/registration/authorize/device/token/revoke routes live directly on the canonical backend. `/v1/research-oauth/consent` uses the registered browser gateway. Access credentials last at most 15 minutes; refresh families last at most 30 days and are bounded by the work lifetime. Every protected use rechecks owner, resource, work, scope, family expiry/revocation and frozen package binding. Access/refresh/device/code secrets are stored as hashes server-side; provider tokens are not accepted as research credentials. Legacy manual issuance and setup-ticket exchange/revocation remain deprecated compatibility paths. New manual issuance requires a registered principal (`403 SIGN_IN_REQUIRED` otherwise). Old anonymous-issued or unmarked manual grants cannot authorize protected access after identity promotion (`403 REGISTERED_CONSENT_REQUIRED`). Legacy tickets are redeemable only when their persisted issuance records prove `issued_principal_kind=registered`; anonymous-issued and unmarked tickets return `403 SIGN_IN_REQUIRED` even after promotion. Use a current credential-free workspace and fresh OAuth consent instead. New downloads never issue tickets.

Local records, artifacts, captures, receipts and operations are durable repository state; MCP connections retain no server session. Short operations use database leases and commit fences. Status reads resume pending operations and API processes perform recovery scans. Closed work rejects new mutations; already-authorized bounded work may finish, while explicit revocation blocks its uncommitted result. Reference retention pins survive until pending operations settle.

Protected uploads/downloads use bounded raw HTTP with the same scoped Reveal bearer, no shared filesystem or credentials in URLs. The local helper handles transfers without exposing the bearer to the agent. Evidence export is a durable polled operation; local materialization verifies the complete selected package and original sources before reporting readiness. Bundled authoring/lint source is not a complete installed offline validator; final validation still uses Reveal's pinned runtime.

## OAuth and provenance checks

Run offline/local test suites from the repository root; these commands do not authorize a live model run:

```sh
PYTHONPATH=services/backend/src:services/backend/tests .venv/bin/python -m unittest test_research_oauth test_research_public test_research_anonymous test_research_graphs test_research_sparql test_research_setup test_local_launcher
.venv/bin/python scripts/build_openapi.py
.venv/bin/python scripts/validate_openapi.py
.venv/bin/python scripts/build_api_viewer.py
npm --prefix services/frontend run generate
npm --prefix services/frontend run fixtures
npm --prefix services/frontend run typecheck
npm --prefix services/frontend test
npm --prefix reveal-client run typecheck
npm --prefix reveal-client test
```

The OAuth suite covers registered consent, PKCE/resource/client bindings, device polling, refresh rotation/replay and revocation. Public-research tests cover exact captures, explicit expiry, reference-generation attachment, retained context and public-only science. Connected-graph checks additionally cover the fixed upstream/graphs, bounded query construction, exact upstream bytes, selected-graph attachment, and rejection of schema/error/empty records as claim evidence; consult the test results before treating any check as executed. Setup/launcher suites cover credential-free archive paths/hashes, anonymous/offline launch, explicit sign-in, credential-store isolation and transfers. See their test results for what was actually executed; this list is a verification procedure, not a claim of passing live provider login or CLI research.

`services/frontend/scripts/check-local-work.mjs` and `check-research-consent.mjs` exercise UI behavior with intercepted API responses. The consent regression checks missing providers, sign-in callback preservation, explicit workspace promotion, no grant on reads/refresh, device approval, expiry, work selection and exact server callback handling. It does not contact Google, ORCID or a model provider. Run against a current local build using `LOCAL_WORK_FRONTENDS` / `RESEARCH_CONSENT_FRONTEND`.

Existing backend suites for HTTP/data/retention/execution/hosted/reuse/progressive acceptance/Box remain relevant. Actual Google/ORCID login, user-run Codex/Claude investigations, native storage smoke and a paid hosted investigation are separate checks. These automated procedures do not establish research quality or speedup. Semantic reuse search, a full offline validator installer, desktop auto-launch, Windows authenticated credential storage remain follow-on work.

The [implementation design](local-agent-mcp-design.md) records the scientific and execution rationale.
