"""Document-local CURIE declarations and schema-aware identifier expansion.

Only identifiers and URI/reference slots are interpreted, never arbitrary prose,
commands, filenames, enum codes, or opaque alternate identifiers.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import re
from urllib.parse import urlsplit


# Explicit schemes avoid treating every misspelled CURIE prefix as a URI scheme.
# Other namespaces can be supplied through the document's prefixes mapping.
NETWORK_SCHEMES = {"http", "https", "ftp", "ftps", "s3", "gs", "drs", "ipfs", "ipns"}
URI_SCHEMES = NETWORK_SCHEMES | {"file", "urn", "mailto", "tag"}
PREFIX_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9._-]*\Z")


def is_absolute_uri(value: str) -> bool:
    if not isinstance(value, str) or not value or re.search(r'[\s<>"{}|\\^`]', value):
        return False
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return False
    try:
        uri = urlsplit(value)
        if uri.scheme in NETWORK_SCHEMES:
            return bool(uri.netloc and uri.hostname)
        if uri.scheme == "file":
            return value.startswith("file://") and uri.path.startswith("/")
        if uri.scheme == "urn":
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*:.+", uri.path))
        if uri.scheme in {"mailto", "tag"}:
            return bool(uri.path)
    except ValueError:
        pass
    return False


class PrefixResolver:
    def __init__(self, sv, document: dict):
        sv.imports_closure()
        self.prefixes = {name: str(base) for name, base in sv.namespaces().items()}
        self.errors: list[tuple[str, str]] = []
        if "prefixes" not in document:
            return
        declared = document["prefixes"]
        if not isinstance(declared, dict):
            self.errors.append(("prefixes", "expected a mapping of prefix names to absolute URI bases"))
            return
        for name, base in declared.items():
            where = f"prefixes.{name}"
            if not isinstance(name, str) or not PREFIX_NAME.fullmatch(name):
                self.errors.append((where, "invalid prefix name (write the name without a trailing colon)"))
            elif name.lower() in URI_SCHEMES:
                self.errors.append((where, "a document prefix cannot replace a URI scheme"))
            elif not is_absolute_uri(base):
                self.errors.append((where, "prefix base must be an absolute URI, not a path or another CURIE"))
            elif name in self.prefixes and self.prefixes[name] != base:
                self.errors.append((where, "cannot override a prefix declared by the model"))
            else:
                self.prefixes[name] = base

    def expand(self, value: str) -> str:
        prefix, colon, local = value.partition(":")
        if colon and prefix in self.prefixes:
            if not local:
                raise ValueError(f"empty local identifier in {value!r}")
            expanded = self.prefixes[prefix] + local
            if not is_absolute_uri(expanded):
                raise ValueError(f"{value!r} does not expand to a valid absolute URI")
            return expanded
        if is_absolute_uri(value):
            return value
        if colon and prefix.lower() not in URI_SCHEMES:
            raise ValueError(f"undeclared prefix {prefix!r} in {value!r}; declare it in prefixes or the schema")
        raise ValueError(f"{value!r} is not an absolute URI or a resolvable CURIE")


def transform_identifiers(document: dict, sv, groups: dict[str, str], *, compact_dapper=False):
    """Return a copy plus resolution errors, retaining invalid values for diagnostics.

    The linter uses a canonical representation for graph matching, but checks
    content digests against the original serialization. This handles mixed
    CURIE/URI references without silently changing the content being checked.
    """
    from identity.dapper_identity import compact_identifier

    resolver = PrefixResolver(sv, document)
    errors = list(resolver.errors)
    result = deepcopy(document)
    classes = sv.all_classes()

    @lru_cache(maxsize=None)
    def slots_for(class_name):
        return {slot.name: slot for slot in sv.class_induced_slots(class_name)}

    @lru_cache(maxsize=None)
    def uri_type(name):
        seen = set()
        while name and name not in seen:
            if name in {"uri", "uriorcurie"}:
                return True
            seen.add(name)
            type_def = sv.get_type(name)
            name = type_def.typeof if type_def else None
        return False

    def identifier(value, where):
        if isinstance(value, list):
            return [identifier(v, f"{where}[{i}]") for i, v in enumerate(value)]
        if not isinstance(value, str):
            return value  # Shape/type validation belongs to the schema validator.
        try:
            expanded = resolver.expand(value)
            return compact_identifier(expanded) if compact_dapper else expanded
        except ValueError as exc:
            errors.append((where, str(exc)))
            return value

    def record(node, class_name, where):
        if not isinstance(node, dict):
            return
        for key, value in list(node.items()):
            slot = slots_for(class_name).get(key)
            if slot is None or value is None:
                continue
            path = f"{where}.{key}"
            path_annotation = getattr(slot.annotations, "dapper:uri_or_path", None)
            if path_annotation and path_annotation.value:
                # Locations admit real filesystem paths as well as URI/CURIEs.
                # A colon denotes the latter except for Windows drive paths.
                if isinstance(value, str) and ":" in value and not re.match(r"^[A-Za-z]:[\\/]", value):
                    node[key] = identifier(value, path)
            elif slot.range in classes and (slot.inlined or slot.inlined_as_list):
                if isinstance(value, list):
                    for i, child in enumerate(value):
                        record(child, slot.range, f"{path}[{i}]")
                else:
                    record(value, slot.range, path)
            elif slot.identifier or uri_type(slot.range) or slot.range in classes:
                node[key] = identifier(value, path)

    for group, class_name in groups.items():
        values = result.get(group)
        if not isinstance(values, list) or class_name not in classes:
            continue
        for i, node in enumerate(values):
            record(node, class_name, f"{group}[{i}]")
    if isinstance(result.get("_illustrative"), list):
        result["_illustrative"] = identifier(result["_illustrative"], "_illustrative")
    return result, errors


def expand_document(document: dict, sv) -> dict:
    """Expand declared identifier/URI fields and re-mint existing DAPPER records.

    This is not a schema validator. Unresolvable identifiers and duplicate node
    IDs are errors; callers can additionally lint the returned document. The
    input is never mutated. External record identifiers are retained, while
    content-derived IDs and all references track any changed hashable values.
    """
    import yaml
    from identity.dapper_identity import DOC_GROUPS, assign_ids
    from lint.lint_provenance import PROFILES_PATH, Vocabulary

    if not isinstance(document, dict):
        raise ValueError("top level must be a mapping")
    vocab = Vocabulary.build(sv, yaml.safe_load(PROFILES_PATH.read_text()))
    expanded, errors = transform_identifiers(document, sv, {**vocab.node_groups, **vocab.edge_groups})
    if errors:
        raise ValueError("\n".join(f"{where}: {message}" for where, message in errors))
    ids = set()
    for group in DOC_GROUPS:
        nodes = expanded.get(group, [])
        if not isinstance(nodes, list):
            raise ValueError(f"{group}: expected a list")
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise ValueError(f"{group}: every node needs a string id")
            if node["id"] in ids:
                raise ValueError(f"duplicate node identifier after expansion: {node['id']}")
            ids.add(node["id"])
    expanded.pop("prefixes", None)
    assign_ids(expanded, sv, expanded=True, preserve_external=True)
    return expanded
