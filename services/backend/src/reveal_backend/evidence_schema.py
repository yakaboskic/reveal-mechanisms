"""LinkML-derived structural validation for the evidence-package wire format.

This is separate from the frozen builder so adding a schema does not invalidate
the source-code pins or byte-for-byte replay of existing captures.
"""
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from linkml.generators.jsonschemagen import JsonSchemaGenerator
from linkml_runtime.utils.schemaview import SchemaView

from .evidence_package import EvidenceBuildError, canonical_json


def generate_schema(schema_path):
    """Compile LinkML with explicit, narrow wire-format compatibility rules.

    LinkML 1.11.1 does not emit extra_slots in JSON Schema. Materialize that
    standard LinkML declaration here, only on classes that explicitly opt in.
    Its Any mapping omits arrays; the annotated raw-list boundary handles that.
    All other classes remain closed, including imported DAPPER objects.
    """
    view = SchemaView(str(schema_path))
    result = json.loads(JsonSchemaGenerator(str(schema_path), top_class='EvidencePackage',
                        not_closed=False, include_null=True).serialize())
    definitions = result['$defs']
    for name, cls in view.all_classes().items():
        for definition_name in (name, name + '__identifier_optional'):
            definition = definitions.get(definition_name)
            if definition is None:
                continue
            if cls.extra_slots and cls.extra_slots.allowed:
                definition['additionalProperties'] = True
            json_type = cls.annotations.get('json_type')
            if json_type and json_type.value == 'array':
                definition.clear()
                definition.update(type='array', items={}, description=cls.description)
            elif json_type and json_type.value == 'any':
                definition.clear()
                definition.update(description=cls.description)
    validator_for(result).check_schema(result)
    return result


@lru_cache(maxsize=4)
def _validator(schema_json_bytes):
    schema = json.loads(schema_json_bytes)
    validator_class = validator_for(schema)
    validator_class.check_schema(schema)
    return validator_class(schema, format_checker=FormatChecker())


def validate_package_shape(package, schema):
    """Validate without coercion or mutation; source/identity checks are separate."""
    canonical_json(package)  # Also rejects NaN/Infinity, which JSON Schema alone allows.
    validator = _validator(canonical_json(schema))
    errors = sorted(validator.iter_errors(package), key=lambda error: '/'.join(map(str, error.absolute_path)))
    if errors:
        def describe(error):
            path = '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in error.absolute_path)
            return f'{path}: {error.message}'
        raise EvidenceBuildError('Evidence-package schema validation failed:\n' + '\n'.join(describe(e) for e in errors[:20]))


def load_generated_schema(path):
    return json.loads(Path(path).read_bytes())
