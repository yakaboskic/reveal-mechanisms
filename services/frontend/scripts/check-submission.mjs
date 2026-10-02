#!/usr/bin/env node
/** Compatibility entrypoint for submission lifecycle regression.
 * The stateful harness covers the current explicit Save / temporary editor /
 * frozen run model, including bounded deadlines and pending OAuth recovery.
 * See scripts/README.md for the migration from the old autosave assumptions.
 */
process.env.DRAFT_LIFECYCLE_BASE_URL ||= process.env.SUBMISSION_BASE_URL || 'http://127.0.0.1:3000';
if (process.env.SUBMISSION_AUDIT_DIR) process.env.DRAFT_LIFECYCLE_AUDIT_DIR ||= process.env.SUBMISSION_AUDIT_DIR;
if (process.env.SUBMISSION_SCENARIO_FILTER) process.env.DRAFT_LIFECYCLE_SCENARIO_FILTER ||= process.env.SUBMISSION_SCENARIO_FILTER;
await import('./check-draft-lifecycle.mjs');
