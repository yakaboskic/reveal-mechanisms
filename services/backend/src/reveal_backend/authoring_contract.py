"""Generate the shipped authoring schema from hash-locked upstream definitions."""
from pathlib import Path
from copy import deepcopy
import yaml
from .evidence_package import decode, canonical_json, require, sha256

CONTRACT_VERSION = 'reveal.authoring-contract/2'
SCHEMA_PATH = 'input/package-sections/authoring-schema-excerpt.yaml'
EXAMPLE_PATH = 'input/package-sections/authoring-examples.json'
WANTED = {'ScientificAccount','Proposition','Claim','EvidenceItem','ClaimScore','Mechanism','GeneSet','File','KnowledgeGap'}
DEPENDENCY_PIN = 'services/backend/agent-runtime/authoring-schema-dependencies.json'


def schema_excerpt(release_root, lock_path):
    release_root=Path(release_root); lock_raw=Path(lock_path).read_bytes(); lock=decode(lock_raw)
    definitions={k:{} for k in ('classes','slots','enums','types','prefixes')}
    sources={}
    dependencies=decode((Path(lock_path).parent/'authoring-schema-dependencies.json').read_bytes())
    for name, entry in dependencies['dependencies'].items():
        require(entry['path']=='linkml-types-1.11.1.yaml', 'Unexpected schema dependency path')
        raw=(Path(lock_path).parent/entry['path']).read_bytes()
        require(sha256(raw)==entry['sha256'], 'Authoring schema dependency checksum changed: '+name)
        part=yaml.safe_load(raw)
        for kind in definitions: definitions[kind].update(part.get(kind,{}) or {})
    defaults={}
    for relative in ('schema/dapper.yaml','schema/claims.yaml'):
        raw=(release_root/relative).read_bytes()
        require(sha256(raw)==lock['files'][relative], 'Authoring schema differs from locked release: '+relative)
        sources[relative]=sha256(raw)
        part=yaml.safe_load(raw)
        if relative=='schema/dapper.yaml':
            defaults={key:part[key] for key in ('default_prefix','default_range')}
        for kind in definitions: definitions[kind].update(part.get(kind,{}) or {})
    result={k:{} for k in ('classes','slots','enums','types')}
    def visit(name):
        for kind in result:
            if name in definitions[kind] and name not in result[kind]:
                value=deepcopy(definitions[kind][name]); result[kind][name]=value
                walk(value)
    def walk(value):
        if isinstance(value,dict):
            for key,child in value.items():
                if key in ('is_a','range','typeof') and isinstance(child,str): visit(child)
                elif key in ('mixins','slots') and isinstance(child,list):
                    for name in child: visit(name)
                elif key=='slot_usage' and isinstance(child,dict):
                    for name in child: visit(name)
                walk(child)
        elif isinstance(value,list):
            for child in value: walk(child)
    for name in sorted(WANTED): visit(name)
    visit(defaults['default_range'])
    require(WANTED<=set(result['classes']), 'Missing authoring classes')
    return yaml.safe_dump({'format':CONTRACT_VERSION,'note':'Exact transitive definitions from the locked release, including inherited classes, slots, mixins, ranges, enums and reference semantics. This excerpt is not an installed validator.',
        'release':{'tag':lock['tag'],'commit':lock['commit'],'lock_sha256':sha256(lock_raw),'source_sha256':sources},
        'external_dependencies':dependencies['dependencies'],**defaults,
        'prefixes':definitions['prefixes'],**result},sort_keys=True).encode()


def pinned_schema(project_root):
    root=Path(project_root)
    raw=(root/'services/backend/agent-runtime/authoring-schema-excerpt.yaml').read_bytes()
    value=yaml.safe_load(raw); lock_raw=(root/'services/backend/agent-runtime/dapper-release.json').read_bytes(); lock=decode(lock_raw)
    require(value['release']['lock_sha256']==sha256(lock_raw) and value['release']['commit']==lock['commit'], 'Shipped authoring excerpt release pin changed; regenerate it')
    require(all(lock['files'].get(k)==v for k,v in value['release']['source_sha256'].items()),'Shipped schema source hashes changed')
    dependencies=decode((root/DEPENDENCY_PIN).read_bytes())
    require(value.get('external_dependencies')==dependencies['dependencies'], 'Shipped schema dependency pins changed')
    return raw
