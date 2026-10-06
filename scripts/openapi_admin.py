"""Separate administrator read contract, using the existing scientific fixtures."""
from copy import deepcopy


SECURITY_SCHEME = {
    'type': 'http', 'scheme': 'bearer', 'bearerFormat': 'Admin read API key',
    'description': 'Dedicated rvl_admin_ credential with fixed science:read scope. '
        'Available only where explicitly configured; issued separately for QA. '
        'Authorizes only the four /v1/admin/accounts and /v1/admin/explorations GET operations. '
        'It grants cross-owner access to retained scientific records, including unpublished records. '
        'It does not authorize workspace sessions, MCP, jobs, artifact downloads, internal administration, '
        'editing, submission or publication. Ordinary workspace API keys and gateway/service credentials '
        'are not accepted here. Paste only the key; Swagger adds the Bearer prefix.'}


def extend(b, f, e):
    obj, ref, array, string = b.obj, b.ref, b.array, b.string
    digest = string(pattern='^[a-f0-9]{64}$')
    uuid = string(format='uuid')
    account_identity = {'record_id': digest, 'owner_user_id': uuid, 'summary': ref('AccountSummary')}
    exploration_identity = {'record_id': uuid, 'owner_user_id': uuid, 'summary': ref('AnalysisOutcomeSummary')}
    b.add('AdminAccountSummary', obj(account_identity, description=
        'One retained owner-specific account. record_id is the SHA-256 storage identity of '
        '[owner_user_id, account.id], not a DAPPER ID. Copies of one scientific account '
        'in different workspaces remain separate records. Publication can_manage is always false.'))
    b.add('AdminAccountDetail', obj({**account_identity, 'result': ref('AccountResult'),
        'paragraph': b.nullable(ref('ParagraphObjectResult'))}, description=
        'Current saved account envelope and its saved research statement, if available. '
        'Scientific IDs, schema pins, payload checksums, citations and coverage retain their stored values. '
        'An incomplete saved envelope stays explicitly incomplete; this endpoint does not expand or '
        're-query its sources. Artifact download URLs, expiry times and owner-scoped coverage.next_cursor are null. '
        'Publication can_manage is false; this is not an owner session or a frozen public snapshot.'))
    b.add('AdminExplorationSummary', obj(exploration_identity, description=
        'One retained insufficient-evidence analysis outcome, with its owner and UUID record identity. '
        'Explorations here are saved analysis outcomes, not knowledge-gap visit/bookmark records. '
        'Publication can_manage is always false.'))
    b.add('AdminExplorationDetail', obj({**exploration_identity, 'result': ref('AnalysisOutcome')}, description=
        'Current saved scoped exploration outcome with original attribution, scope and provenance. '
        'Scientific evidence hashes and source locators are preserved. Artifact and evidence-reference '
        'download URLs are null; internal storage records and credentials are not returned. '
        'Publication can_manage is false.'))
    for kind in ('Account', 'Exploration'):
        b.add('Admin' + kind + 'List', obj({'items': array(ref('Admin' + kind + 'Summary')),
            'page': ref('Page')}))

    def existing(path, example):
        return deepcopy(b.PATHS[path]['get']['responses']['200']['content']['application/json']['examples'][example]['value'])

    def readonly(value):
        """Apply only response access metadata changes, keeping scientific nodes exact."""
        if 'publication' in value:
            value['publication']['can_manage'] = False
        if 'coverage' in value:
            value['coverage']['next_cursor'] = None
        artifacts = value.get('artifacts', []) + value.get('provenance', {}).get('source_artifacts', [])
        for artifact in artifacts:
            artifact['download_url'] = None
            artifact['expires_at'] = None
        for evidence in value.get('provenance', {}).get('evidence_refs', []):
            evidence['download_url'] = None
        return value

    account_result = readonly(existing('/v1/accounts/{dapper_id}', 'document'))
    account_summary = readonly(existing('/v1/accounts', 'owned')['items'][0])
    account = {'record_id': b.sha([b.USER_ID, f['account']['id']]),
        'owner_user_id': b.USER_ID, 'summary': account_summary}
    account_detail = {**account, 'result': account_result,
        'paragraph': readonly(existing('/v1/paragraphs/{dapper_id}', 'document'))}
    outcome_result = readonly(existing('/v1/analysis-outcomes/{outcome_id}', 'private_owner'))
    outcome_summary = readonly(existing('/v1/analysis-outcomes', 'saved')['items'][0])
    exploration = {'record_id': outcome_result['id'], 'owner_user_id': b.USER_ID, 'summary': outcome_summary}
    exploration_detail = {**exploration, 'result': outcome_result}
    common = ('Requires the separately issued administrator read key with fixed science:read scope. '
        'Normal workspace API keys, gateway assertions, service tokens and anonymous requests are rejected. '
        'Reads retained scientific results across owners, including archived records and records of expired owners. '
        'No scientific mutation, publication, job execution, source query or artifact transfer occurs. '
        'Responses and authorization errors use Cache-Control: private, no-store and Vary: Authorization. ')
    list_description = ('visibility=private (default) selects currently unpublished records; public selects '
        'currently published records; all includes both. Public records with unpublished statement changes '
        'are found using all and publication.has_unpublished_changes, not the private filter. '
        'Returns current owner records, never substitutes frozen public snapshots. '
        'Results are ordered newest first with stable record identity tie-breaking. '
        'Cursors bind the administrator credential, resource, filter and collection revision; '
        'rotation, filter changes or changed records require restarting pagination. ')
    query = [b.parameter('visibility', 'query', b.enum('private', 'public', 'all', default='private'), 'private'),
        b.parameter('limit', 'query', {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 50}, 50),
        b.parameter('cursor', 'query', string(minLength=1), 'opaque-next-page', description=
            'Omit for the first page; reuse the returned next_cursor with the same credential and visibility filter.')]
    placeholder = 'Bearer rvl_admin_<private-admin-read-key>'

    def operation(path, name, summary, description, schema, examples, parameters, *, detail=False):
        op = b.operation(path, 'get', name, 'Administrator science read', summary, common + description,
            schema, examples, parameters=parameters,
            errors=('401', '404', '422', '503') if detail else ('401', '409', '422', '503'))
        op['security'] = [{'AdminReadBearer': []}]
        op['x-required-scope'] = 'science:read'
        errors = {
            '401': b.problem(401, 'INVALID_ADMIN_READ_API_KEY', 'An administrative scientific read key is required.'),
            '409': b.problem(409, 'CURSOR_EXPIRED', 'The collection, query scope or credential changed. Restart from the first page.'),
            '422': b.problem(422, 'INVALID_INPUT', 'The request does not satisfy the administrator read operation contract.'),
            '503': b.problem(503, 'ADMIN_READ_API_KEY_CONFIGURATION_INVALID', 'Administrator read key configuration is invalid.')}
        for status, response in op['responses'].items():
            response.setdefault('headers', {}).update({
                'Cache-Control': {'schema': string(), 'example': 'private, no-store'},
                'Vary': {'schema': string(), 'example': 'Authorization'}})
            if status in errors:
                value = errors[status]
                response.update(description=value['title'], content=b.content('Problem',
                    {value['code'].lower(): value}, 'application/problem+json'))
        for exchange in b.EXCHANGES:
            if exchange['operation_id'] == name:
                old = exchange['request']['headers']['Authorization']
                exchange['request']['headers']['Authorization'] = placeholder
                exchange['curl'] = exchange['curl'].replace(old, placeholder)
        for sample in op['x-codeSamples']:
            sample['source'] = sample['source'].replace('Bearer <api-key-or-gateway-assertion>', placeholder)

    for resource, kind, summary, detail, identity_schema in (
            ('accounts', 'Account', account, account_detail, digest),
            ('explorations', 'Exploration', exploration, exploration_detail, uuid)):
        path = '/v1/admin/' + resource
        operation(path, 'listAdmin' + kind + 's', 'List retained ' + resource + ' across owners',
            list_description, 'Admin' + kind + 'List',
            {'private_records': {'items': [summary], 'page': e['page']},
             'empty': {'items': [], 'page': e['page']}}, query)
        operation(path + '/{record_id}', 'getAdmin' + kind, 'Read a retained ' + kind.lower() + ' across owners',
            'Use the owner-specific record_id from its administrator list. Private and public records are readable. '
            'Returns 404 if this resource has no matching retained record. ', 'Admin' + kind + 'Detail',
            {'retained_record': detail}, [b.parameter('record_id', 'path', identity_schema, summary['record_id'], True)], detail=True)
