"""Allowed retained collection provenance, separate from canonical scientific nodes."""
from functools import lru_cache


PROVENANCE_NODES = {
    'activities': 'Activity', 'files': 'File', 'c2m2_files': 'C2M2File', 'datasets': 'Dataset',
    'sets': 'Set', 'organizations': 'Organization', 'persons': 'Person', 'awards': 'Award',
    'publications': 'Publication', 'licenses': 'License', 'lineage_steps': 'LineageStep',
    'recommended_citations': 'RecommendedCitation', 'drs_objects': 'DrsObject',
    'data_use_terms': 'DataUseTerm', 'ro_crate_packages': 'RoCratePackage',
    'bio_compute_objects': 'BioComputeObject', 'mirror_provenances': 'MirrorProvenance',
    'agentic_workspaces': 'AgenticWorkspace',
}
PROVENANCE_EDGES = {
    'has_creator_edges': 'HasCreator', 'has_contributor_edges': 'HasContributor',
    'funded_by_edges': 'FundedBy', 'is_described_by_edges': 'IsDescribedBy',
    'was_derived_from_edges': 'WasDerivedFrom', 'was_generated_by_edges': 'WasGeneratedBy',
    'used_edges': 'Used', 'was_attributed_to_edges': 'WasAttributedTo',
    'has_lineage_step_edges': 'HasLineageStep', 'has_recommended_citation_edges': 'HasRecommendedCitation',
    'has_mirror_provenance_edges': 'HasMirrorProvenance', 'has_data_use_term_edges': 'HasDataUseTerm',
    'has_file_edges': 'HasFile', 'has_drs_object_edges': 'HasDrsObject', 'drs_representation_edges': 'DrsRepresentation',
    'packaged_as_edges': 'PackagedAs', 'has_workflow_provenance_edges': 'HasWorkflowProvenance',
    'has_agentic_workspace_edges': 'HasAgenticWorkspace',
}
PROVENANCE_GROUPS = ('prefixes', *PROVENANCE_NODES, *PROVENANCE_EDGES)


@lru_cache(maxsize=1)
def _edge_runtime():
    from .evidence_package import DapperRuntime
    from .runtime_config import CURRENT_DAPPER_SNAPSHOT
    return DapperRuntime(CURRENT_DAPPER_SNAPSHOT)


def validate_provenance_edges(graph):
    """Validate only preserved edge records, without requiring all GeneSet nodes in memory."""
    from .evidence_package import require
    present = {group: graph[group] for group in PROVENANCE_EDGES if graph.get(group)}
    if not present: return
    runtime = _edge_runtime()
    for group, rows in present.items():
        cls = PROVENANCE_EDGES[group]
        for edge in rows:
            result = runtime.validator.validate(edge, cls)
            require(not result.results, f'Invalid retained {cls}: {[r.message for r in result.results]}')
            expected = str(runtime.schema.induced_slot('predicate', cls).ifabsent or '')
            require(not expected or edge.get('predicate') == expected[7:-1], f'Invalid retained {cls} predicate')
