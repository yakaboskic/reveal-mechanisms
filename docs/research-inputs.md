# Saved drafts and private research inputs

An editor opened from a Knowledge Gap uses a `temporary` draft with a 24-hour expiry. The browser holds edits until the researcher explicitly saves or submits. Temporary drafts do not appear in the Saved Drafts list. Saving supplies a name and promotes the draft to `saved`; saved drafts have no expiry. A missing lifecycle on historical records means saved for compatibility.

Submitting freezes the composer and `reveal.user-inputs/1` into the research request. The frozen record pins every original upload and extraction by SHA-256, size and immutable storage reference. The worker reads these references rather than looking up the current draft. Deleting a draft, editing a saved version or abandoning an editor cannot rewrite a submitted run. A temporary clone can record its originating saved draft and loaded version without changing that saved draft. Send `source_draft_id` and `source_draft_version` together to preserve the editor's loaded revision even when another session saved a newer version. Positive historical versions up to the current owned saved revision are accepted; an omitted version means current for compatibility.

## Upload protocol

1. `POST /v1/uploads` with an idempotency key, owned `draft_id`, plain filename, media type, exact byte size and SHA-256. The response contains upload metadata and a transfer ticket. Replaying initiation returns current metadata and a fresh ticket, with ownership checked again.
2. In S3 mode, upload bytes directly using the ticket's POST fields and multipart form. Policies enforce exact size, content type and AES256 encryption and expire after 15 minutes. Keys are confined to the configured prefix's `uploads/staging/` directory. The browser does not receive storage credentials.
3. `POST /v1/uploads/{id}/complete` verifies the exact uploaded bytes, performs bounded extraction, retains immutable original/extraction objects and returns `ready` metadata. Completion is idempotent. A retry should first read `GET /v1/uploads/{id}` and skip transfer when already ready.
4. Include ready `upload_ids` when saving or submitting. Owned ready uploads may be reused by a temporary clone even when their original `draft_id` differs.

The filesystem test/development store uses a JSON base64 content endpoint instead of multipart, because the application gateway forwards JSON text. That endpoint is disabled when S3 is configured. Owner-authorized download streams the checksum-verified original bytes with an attachment disposition and private, non-cacheable response headers.

Supported formats are UTF-8 text, Markdown, CSV, TSV, JSON, YAML, text-bearing PDF and DOCX. Scanned PDFs require OCR before upload. Limits are five selected files, 8 MB per file, 16 MB selected original bytes, 500 KB extracted text per file, and 1 MB aggregate extracted JSON including locators. PDF extraction rejects encrypted files and documents exceeding 100 pages. DOCX expanded archive bytes, PDF page streams and segment counts are bounded. Parsing runs in a separate process without inherited credentials, with CPU and wall-clock limits; Linux additionally enforces a 1 GiB address-space limit.

Pending transfers have a separate five-file/16 MB allowance so a researcher can replace an attachment while the prior saved version still references it. A workspace may initiate up to 50 files/100 MB per rolling 24-hour period. Selection limits are enforced again when saving or submitting. Removed and failed attempts count toward the rolling allowance.

## Evidence and disclosure

The evidence package includes researcher direction, context, hypotheses, upload metadata, exact original and extraction DAPPER File IDs, and extracted segments with page, paragraph or line locators. The agent's file reader receives this content, and final validation checks returned output against the frozen sources. Original and extraction bytes are bundled and checksum verified. Recovery rejects changes to the submitted text or pinned upload descriptors.

Researcher hypotheses are unverified proposals. Supplied observations can support auxiliary evidence only when the exact content supports the claim; required CFDE ancestry is unchanged. Citations use the extraction File and an exact JSON Pointer such as `/segments/0`, together with its human-readable locator. The deterministic source checker verifies segment resolution and verbatim snippets, including when an account cites the original PDF or DOCX File.

Inputs and resulting accounts remain private. Publishing an account or exploration that used nonempty private text or attachments currently returns `PRIVATE_RESEARCH_INPUTS`; a future deliberate disclosure workflow must be designed before enabling that publication. Upload storage locations remain owner-scoped metadata.

## Retention and local rollout

Expired temporary drafts and unreferenced upload metadata are cleaned by managed workflow reconciliation (every scheduled tick, one to five minutes apart; listing drafts is a pure read that already hides temporary editors, and an expired editor answers `EDITOR_EXPIRED` until it is removed). An expired upload that is still referenced is checked again a day later rather than on every pass. Removing an upload referenced by any live saved draft or frozen request returns `UPLOAD_IN_USE`. Cleanup never deletes shared content-addressed original/extraction objects. S3 staging expiry is configured separately and applies only to `local/uploads/staging/`, including noncurrent versions.

The current local deployment uses `.runtime/workflow/backend.env`, `reveal_workflow_local_*` application tables and the `local/` S3 prefix. It may use remote development backing services. The legacy local README is not the deployment authority; see [Durable workflow runtime](durable-workflow-runtime.md).

`scripts/migrate_research_inputs.py` defaults to a dry run. An explicit `--apply` preserves historical drafts as saved and adds empty composer fields, taking a private backup before one optimistic transaction. It does not rewrite requests, accounts, outcomes or scientific identities. Restore requires reviewing the backup against any later edits; blindly replaying the backup would overwrite newer revisions.

`scripts/configure_upload_storage.py` also defaults to a dry run. Explicit `--apply` adds the local browser origin and local staging expiry while preserving unrelated bucket rules, and saves the prior configuration. Both scripts reject a configuration outside the isolated local namespace. Neither script configures QA or production.

Restarting the local API can resume deliveries from the existing workflow scheduler. Inspect existing jobs and scheduler state before restarting; a pending real analysis may start paid execution independently of a UI smoke test. The test suite uses isolated application storage, mocked S3 and fixture collection and does not require a model call.
