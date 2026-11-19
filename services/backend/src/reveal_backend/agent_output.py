"""Structured output of an answer agent job: the JSON Schema a requester sends, and the gate its result passes.

A request may carry output_schema, a JSON Schema the final answer must satisfy. Upstash Box offers structured
output through its own agent API (response_schema), which drives Claude Code's --json-schema; REVEAL does not use
that API, because it runs Claude Code itself inside the Box with its own tools, tool ledger and key proxy. So the
runner passes the frozen schema to Claude Code as --json-schema, the same mechanism, and keeps everything else.

Two checks happen here, in the backend, where the JSON Schema library is:

  - check_schema, when the job is submitted: the schema is valid (Draft 2020-12 unless it declares another draft) and
    names no remote reference. box_agent.output_schema has already checked its size and that it describes an object.
  - check_result, after the run: the object Claude Code returned satisfies that schema and is within the size cap.
    Claude Code validated it too; this is REVEAL's own check of the bytes it stores and serves.
"""
from jsonschema import exceptions, validators

from .box_agent import MAX_STRUCTURED_OUTPUT_BYTES, AgentInputError, canonical_json
from .evidence_package import EvidenceBuildError


def validator(schema):
    cls = validators.validator_for(schema, default=validators.Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema)


def check_schema(schema):
    """Raise AgentInputError, with where the schema is wrong, when output_schema is not a valid JSON Schema."""
    try:
        validator(schema)
    except exceptions.SchemaError as exc:
        where = '/'.join(str(part) for part in exc.absolute_path) or 'the top level'
        raise AgentInputError(f'output_schema is not a valid JSON Schema at {where}: {exc.message}'[:400]) from None


def check_result(record, schema):
    """The structured output in a trusted response record, checked against the job's output_schema. Raises
    EvidenceBuildError (the job fails as VALIDATION_FAILED) when it is missing, too large or does not satisfy it."""
    if 'structured_output' not in record or record['structured_output'] is None:
        raise EvidenceBuildError('The agent finished without structured output, although the job asked for output_schema')
    value = record['structured_output']
    size = len(canonical_json(value).encode())
    if size > MAX_STRUCTURED_OUTPUT_BYTES:
        raise EvidenceBuildError(f'The structured output is {size:,} bytes; a job result holds at most {MAX_STRUCTURED_OUTPUT_BYTES:,}. '
                                 'Ask for a smaller object, and have the agent write anything larger to output/.')
    error = exceptions.best_match(validator(schema).iter_errors(value))
    if error is not None:
        where = '/'.join(str(part) for part in error.absolute_path) or 'the top level'
        # The message names the schema rule that failed, not the value, which may be private.
        raise EvidenceBuildError(f'The structured output does not satisfy output_schema at {where}: '
                                 f'it fails the "{error.validator}" rule')
    return value
