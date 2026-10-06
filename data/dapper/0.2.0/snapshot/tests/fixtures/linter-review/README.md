# PR #27 review examples

Eight self-contained, synthetic YAML documents reproduce the review of
[PR #27](https://github.com/broadinstitute/dapper/pull/27) at commit
`6f606bff1411e3e7ed8ff876ac565c128adf68d5`. Several are deliberately invalid;
these are review fixtures, not templates for authoring provenance.

Every case starts from the same control: one final Dataset, its generating
Activity, one raw input File, and one DrsObject exposing the result. The control
has no warnings. Existing node IDs have been minted and checked so stale
identifiers do not obscure the behavior under review.

## Regression expectations

The fixed linter accepts **00, 04, and 07** without warnings and rejects
**01, 02, 03, 05, and 06** with the appropriate shape, schema, endpoint, or
reachability findings. `test_review_regressions` in
[`test_lint_provenance.py`](../../test_lint_provenance.py) enforces these results.
Inline and reified generation relationships are both supported.

## Behavior before the fixes

- [00-valid-control.yaml](00-valid-control.yaml): should pass; originally passes
  with **0 errors, 0 warnings**.
- [01-file-without-id.yaml](01-file-without-id.yaml): adds a File without an ID
  and with an invented field. Should fail; originally passes with **0 errors,
  0 warnings** because the record is never indexed or validated.
- [02-node-group-is-mapping.yaml](02-node-group-is-mapping.yaml): makes `persons`
  a mapping instead of a list. Should fail shape validation; originally passes
  with **0 errors, 0 warnings**.
- [03-edge-is-scalar.yaml](03-edge-is-scalar.yaml): adds a string to `used_edges`.
  Should fail shape validation; originally passes with **0 errors, 0 warnings**.
- [04-upstream-dataset.yaml](04-upstream-dataset.yaml): replaces the raw input
  File with a Dataset consumed by the same Activity. There is still one final
  result. Should pass once the terminal is selected separately from its class;
  originally reports **2 errors, 1 warning**: two terminals, no generating
  Activity for the input, and no DRS object for the input.
- [05-dangling-unconfigured-edge.yaml](05-dangling-unconfigured-edge.yaml): adds
  a `Supersedes` edge whose endpoints are absent. Neither identifier has the
  DAPPER digest format, and this edge class has no configured endpoint types.
  Should fail reference validation; originally passes with **0 errors,
  0 warnings**.
- [06-literal-masquerades-as-link.yaml](06-literal-masquerades-as-link.yaml):
  adds a disconnected Person and puts its ID in the Dataset's literal
  `description`. Should fail reachability; originally passes with **0 errors,
  0 warnings** because literal text is treated as a graph connection.
- [07-inline-generation.yaml](07-inline-generation.yaml): replaces
  `was_generated_by_edges` with the schema-valid scalar `was_generated_by`.
  Originally reports **1 error, 0 warnings** (`required-edges`). Both representations are accepted after the fix.

## Reproduce

Run from the repository root:

```sh
uv run schema/lint/lint_provenance.py tests/fixtures/linter-review/00-valid-control.yaml
uv run schema/lint/lint_provenance.py tests/fixtures/linter-review/*.yaml
```

If the linter is in a separate checkout, substitute that checkout's path to
`schema/lint/lint_provenance.py`; keep the fixture paths relative to this repo.
The linter loads its own checkout's schema and profiles by default.

The batch command exits **1** because five fixtures deliberately contain errors;
its final summary is **3/8 document(s) clean**. Before the fixes the summary was
6/8, with the valid upstream-Dataset and inline-generation cases rejected.
Each YAML preserves the original observation and records the corrected result.
