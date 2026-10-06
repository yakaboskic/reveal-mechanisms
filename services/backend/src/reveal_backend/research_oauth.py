"""Public-client OAuth for scoped local research, using Reveal browser identity.

No provider tokens are accepted here. Consent requires the existing gateway's
registered principal assertion; a work identifier or device code confers no
workspace ownership. Opaque authorization, device, access and refresh secrets
are retained only as hashes. Rotation/replay fences commit with the response.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .auth import Problem, owned, principal
from .repository import digest, now, uid
from .research_work import deadline, public_base, valid_principal
from .runtime_config import setting

DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
SCOPES = ('research:read', 'research:write')
ACCESS_TTL = 900
REQUEST_TTL = 600
REFRESH_TTL = 30 * 86400
LAUNCHER_CLIENT = 'reveal-local-launcher'
KINDS = ('research_oauth_client', 'research_oauth_request', 'research_oauth_code',
    'research_oauth_device', 'research_oauth_user_code', 'research_oauth_family',
    'research_oauth_refresh', 'research_oauth_rate')


class OAuthError(Exception):
    def __init__(self, error, description, status=400):
        self.error, self.description, self.status = error, description, status


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def resource():
    return public_base()+'/mcp'


def consent_url():
    from .research_setup import urls
    # Share canonical web URL validation, without issuing a setup ticket.
    return urls('placeholder')['return_url'].rsplit('/local-runs/', 1)[0]+'/research/connect'


def metadata():
    base = public_base()
    return {'issuer': base, 'authorization_endpoint': base+'/oauth/authorize',
        'token_endpoint': base+'/oauth/token', 'registration_endpoint': base+'/oauth/register',
        'device_authorization_endpoint': base+'/oauth/device_authorization', 'revocation_endpoint': base+'/oauth/revoke',
        'response_types_supported': ['code'], 'grant_types_supported': ['authorization_code', 'refresh_token', DEVICE_GRANT],
        'token_endpoint_auth_methods_supported': ['none'], 'revocation_endpoint_auth_methods_supported': ['none'],
        'code_challenge_methods_supported': ['S256'], 'scopes_supported': list(SCOPES)}


def resource_metadata():
    return {'resource': resource(), 'authorization_servers': [public_base()],
        'scopes_supported': list(SCOPES), 'bearer_methods_supported': ['header'], 'resource_name': 'Reveal research'}


def challenge(*, write=False, error=None):
    scopes = ' '.join(SCOPES if write else SCOPES[:1])
    value = 'Bearer resource_metadata="'+public_base()+'/.well-known/oauth-protected-resource/mcp", scope="'+scopes+'"'
    if error in ('invalid_token', 'insufficient_scope'): value += ', error="'+error+'"'
    return value


def _text(value, name, minimum=1, maximum=2048):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise OAuthError('invalid_request', 'Invalid '+name+'.')
    return value


def _resource(value):
    if value != resource(): raise OAuthError('invalid_target', 'Request the exact Reveal MCP resource.')
    return value


def _scopes(value=None, permitted=SCOPES):
    value = ' '.join(permitted) if value is None else _text(value, 'scope', maximum=200)
    result = value.split()
    if (not result or len(set(result)) != len(result) or not set(result) <= set(permitted)
            or 'research:read' not in result):
        raise OAuthError('invalid_scope', 'Request research:read, optionally with research:write.')
    return sorted(result)


def _redirect(value):
    _text(value, 'redirect_uri')
    try:
        parsed = urlsplit(value); port = parsed.port
    except ValueError: raise OAuthError('invalid_redirect_uri', 'Register an exact valid callback URL.') from None
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or '\\' in value or any(char.isspace() for char in value)
            or (port is not None and port < 1)
            or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))
            or any(key in ('code', 'state', 'error', 'iss') for key, _ in parse_qsl(parsed.query))):
        raise OAuthError('invalid_redirect_uri', 'Use HTTPS or an exact loopback callback, without fragments or credentials.')
    return value


def _client(tx, identity):
    _text(identity, 'client_id', maximum=200)
    if identity == LAUNCHER_CLIENT:
        return {'client_id': identity, 'client_name': 'Reveal local launcher', 'redirect_uris': [],
            'grant_types': [DEVICE_GRANT, 'refresh_token'], 'scope': ' '.join(SCOPES),
            'token_endpoint_auth_method': 'none'}
    row = tx.get('research_oauth_client', identity)
    if not row or row['data'].get('revoked_at'):
        raise OAuthError('invalid_client', 'Unknown public OAuth client.', 401)
    return row['data']


def _rate(tx, label, subject, limit, period):
    identity = digest(['oauth-rate', label, subject]); current = int(time.time()); window = current//period
    old = tx.get('research_oauth_rate', identity)
    count = old['data']['count'] if old and old['data']['window'] == window else 0
    if count >= limit: raise OAuthError('temporarily_unavailable', 'Too many requests; retry later.', 429)
    tx.put('research_oauth_rate', identity, 'oauth', {'window': window, 'count': count+1})


def _registered(tx, authorization):
    if not isinstance(authorization, str) or not authorization.startswith('Bearer ') or authorization[7:].startswith('rvl'):
        raise Problem(401, 'BROWSER_SESSION_REQUIRED', 'Sign in to Reveal to review this authorization request.')
    identity = principal(tx, authorization)
    if identity['principal_kind'] != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in with Google or ORCID before authorizing local research.')
    return identity


def require_registered_local(authority):
    grant = authority['grant']
    if grant['kind'] != 'local': return
    if authority.get('principal_kind') != 'registered':
        raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in to Reveal before contributing local research.')
    if not grant.get('oauth_family_id') and grant.get('issued_principal_kind') != 'registered':
        raise Problem(403, 'REGISTERED_CONSENT_REQUIRED', 'Authorize this local connection again while signed in to Reveal.')


def _work(tx, owner, work_id):
    me = valid_principal(tx, owner)
    if me['principal_kind'] != 'registered': raise Problem(403, 'SIGN_IN_REQUIRED', 'Sign in to authorize local research.')
    work = owned(tx, 'local_work', _text(work_id, 'local_work_id', maximum=100), owner)['data']
    if work.get('job_id'): raise Problem(404, 'NOT_FOUND', 'Local research unavailable.')
    if work['state'] != 'ready' or not work.get('package_id') or work['expires_at'] <= now():
        raise Problem(409, 'WORK_UNAVAILABLE', 'Choose ready, open local research with an active lifetime.')
    return work


def check_grant_scope(tx, owner, grant, *, write=False):
    """Recheck OAuth family, principal and resource on every authenticated use."""
    family_id = grant.get('oauth_family_id')
    if not family_id: return
    me = valid_principal(tx, owner)
    row = tx.get('research_oauth_family', family_id)
    if (me['principal_kind'] != 'registered' or not row or row['owner'] != owner
            or row['data'].get('issued_owner_user_id') != owner or row['data'].get('revoked_at')
            or row['data']['expires_at'] <= now()):
        raise Problem(401, 'MCP_GRANT_EXPIRED', 'The OAuth authorization is no longer active; sign in again.')
    family = row['data']
    if (grant.get('resource') != resource() or family['resource'] != resource()
            or family['client_id'] != grant.get('client_id') or family['local_work_id'] != grant['local_work_id']
            or family['research_request_id'] != grant['research_request_id']):
        raise Problem(403, 'RESEARCH_SCOPE_MISMATCH', 'The OAuth authorization belongs to a different resource.')
    required = 'research:write' if write else 'research:read'
    if required not in grant.get('scopes', []):
        raise Problem(403, 'INSUFFICIENT_SCOPE', 'Authorize '+required+' for this operation.')


def register_client(tx, body):
    if not isinstance(body, dict): raise OAuthError('invalid_client_metadata', 'Supply a JSON client registration.')
    if body.get('token_endpoint_auth_method', 'none') != 'none':
        raise OAuthError('invalid_client_metadata', 'Only public clients with token_endpoint_auth_method none are supported.')
    redirects = body.get('redirect_uris', [])
    if not isinstance(redirects, list) or len(redirects) > 10 or len(set(map(str, redirects))) != len(redirects):
        raise OAuthError('invalid_client_metadata', 'Supply at most ten distinct callback URLs.')
    redirects = [_redirect(uri) for uri in redirects]
    grants = body.get('grant_types', ['authorization_code'])
    if (not isinstance(grants, list) or not grants or not all(isinstance(g, str) for g in grants)
            or not set(grants) <= {'authorization_code', 'refresh_token', DEVICE_GRANT}
            or ('authorization_code' in grants and not redirects)
            or not set(grants) & {'authorization_code', DEVICE_GRANT}):
        raise OAuthError('invalid_client_metadata', 'Register a supported public-client grant and callback.')
    responses = body.get('response_types', ['code'] if 'authorization_code' in grants else [])
    if responses != (['code'] if 'authorization_code' in grants else []):
        raise OAuthError('invalid_client_metadata', 'Only the code response type is supported.')
    value = {'client_id': 'rvlc_'+secrets.token_urlsafe(24), 'client_name': _text(body.get('client_name', 'Local research client'), 'client_name', maximum=120),
        'client_id_issued_at': int(time.time()), 'redirect_uris': redirects, 'grant_types': sorted(set(grants)),
        'response_types': responses, 'token_endpoint_auth_method': 'none', 'scope': ' '.join(_scopes(body.get('scope')))}
    tx.put('research_oauth_client', value['client_id'], 'oauth', value)
    return value


def authorize(tx, params):
    client = _client(tx, params.get('client_id'))
    if 'authorization_code' not in client['grant_types']:
        raise OAuthError('unauthorized_client', 'This client cannot use authorization codes.')
    redirect = params.get('redirect_uri')
    if redirect not in client['redirect_uris']:
        raise OAuthError('invalid_request', 'The callback URL does not exactly match a registered redirect.')
    if params.get('response_type') != 'code': raise OAuthError('unsupported_response_type', 'Request response_type code.')
    challenge_value = params.get('code_challenge')
    if (params.get('code_challenge_method') != 'S256' or not isinstance(challenge_value, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge_value)):
        raise OAuthError('invalid_request', 'Supply an S256 PKCE challenge.')
    state = params.get('state')
    if state is not None: _text(state, 'state', maximum=1024)
    value = {'id': uid(), 'kind': 'authorization_code', 'client_id': client['client_id'],
        'redirect_uri': redirect, 'state': state, 'code_challenge': challenge_value,
        'scopes': _scopes(params.get('scope'), client['scope'].split()), 'resource': _resource(params.get('resource')),
        'requested_local_work_id': None, 'status': 'pending', 'created_at': now(), 'expires_at': deadline(REQUEST_TTL)}
    tx.put('research_oauth_request', value['id'], 'oauth', value)
    return consent_url()+'?'+urlencode({'request_id': value['id']})


def device_authorization(tx, params):
    client = _client(tx, params.get('client_id'))
    if DEVICE_GRANT not in client['grant_types']: raise OAuthError('unauthorized_client', 'This client cannot use device authorization.')
    hint = params.get('local_work_id')
    if hint is not None: _text(hint, 'local_work_id', maximum=100)
    code = 'rvld_'+secrets.token_urlsafe(32)
    user_code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
    normalized = sha(user_code)
    if tx.get('research_oauth_user_code', normalized):
        raise OAuthError('temporarily_unavailable', 'Retry the device authorization request.', 503)
    value = {'id': uid(), 'kind': 'device', 'client_id': client['client_id'],
        'scopes': _scopes(params.get('scope'), client['scope'].split()), 'resource': _resource(params.get('resource')),
        'requested_local_work_id': hint, 'status': 'pending', 'created_at': now(), 'expires_at': deadline(REQUEST_TTL),
        'device_sha256': sha(code)}
    tx.put('research_oauth_request', value['id'], 'oauth', value)
    tx.put('research_oauth_device', sha(code), 'oauth', {'request_id': value['id'], 'client_id': client['client_id'],
        'resource': value['resource'], 'expires_at': value['expires_at'], 'interval': 5, 'last_poll': None})
    tx.put('research_oauth_user_code', normalized, 'oauth', {'request_id': value['id'], 'expires_at': value['expires_at']})
    display = user_code[:4]+'-'+user_code[4:]
    return {'device_code': code, 'user_code': display, 'verification_uri': consent_url(),
        'verification_uri_complete': consent_url()+'?'+urlencode({'user_code': display}), 'expires_in': REQUEST_TTL, 'interval': 5}


def _request(tx, selector):
    if not isinstance(selector, dict) or bool(selector.get('request_id')) == bool(selector.get('user_code')):
        raise Problem(422, 'INVALID_REQUEST', 'Supply a request_id or user_code.')
    identity = selector.get('request_id')
    if identity is None:
        code = selector.get('user_code')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z2-9]{4}-?[A-Za-z2-9]{4}', code):
            raise Problem(404, 'OAUTH_REQUEST_UNAVAILABLE', 'This authorization request is unavailable.')
        row = tx.get('research_oauth_user_code', sha(code.replace('-', '').upper()))
        if not row or row['data']['expires_at'] <= now():
            raise Problem(404, 'OAUTH_REQUEST_UNAVAILABLE', 'This authorization request is unavailable.')
        identity = row['data']['request_id']
    row = tx.get('research_oauth_request', _text(identity, 'request_id', maximum=100))
    if not row or row['data'].get('revoked_at') or row['data']['expires_at'] <= now():
        raise Problem(404, 'OAUTH_REQUEST_UNAVAILABLE', 'This authorization request is unavailable or expired.')
    return row


def consent_info(tx, authorization, selector):
    me = _registered(tx, authorization)
    _rate(tx, 'consent-lookup', me['user_id'], 30, 60)
    row = _request(tx, selector); value = row['data']
    if row['owner'] not in ('oauth', me['user_id']):
        raise Problem(404, 'OAUTH_REQUEST_UNAVAILABLE', 'This authorization request is unavailable.')
    client = _client(tx, value['client_id'])
    return {'request_id': value['id'], 'kind': value['kind'], 'client_id': value['client_id'],
        'client_name': client['client_name'], 'scopes': value['scopes'], 'resource': value['resource'],
        'requested_local_work_id': value.get('requested_local_work_id'), 'redirect_uri': value.get('redirect_uri'),
        'expires_at': value['expires_at'], 'status': value['status'], 'requires_registered': True}


def _callback(request, **fields):
    parsed = urlsplit(request['redirect_uri'])
    query = parse_qsl(parsed.query, keep_blank_values=True)+list(fields.items())
    if request.get('state') is not None: query.append(('state', request['state']))
    query.append(('iss', public_base()))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))


def consent(tx, authorization, body):
    me = _registered(tx, authorization); owner = me['user_id']
    if (not isinstance(body, dict) or type(body.get('approve')) is not bool
            or not set(body) <= {'request_id', 'user_code', 'local_work_id', 'approve'}):
        raise Problem(422, 'INVALID_REQUEST', 'Supply a consent decision and the selected local work.')
    _rate(tx, 'consent-decision', owner, 30, 60)
    row = _request(tx, body); value = row['data']
    if row['owner'] not in ('oauth', owner): raise Problem(404, 'OAUTH_REQUEST_UNAVAILABLE', 'This request is unavailable.')
    if value['status'] != 'pending': raise Problem(409, 'OAUTH_ALREADY_DECIDED', 'This authorization request was already answered.')
    _client(tx, value['client_id']); _resource(value['resource'])
    if body['approve']:
        work = _work(tx, owner, body.get('local_work_id'))
        if value.get('requested_local_work_id') and work['id'] != value['requested_local_work_id']:
            raise Problem(403, 'RESEARCH_SCOPE_MISMATCH', 'This client requested a different local research run.')
        value.update(status='approved', owner_user_id=owner, local_work_id=work['id'],
            research_request_id=work['research_request_id'], package_sha256=work['package_sha256'])
        if value['kind'] == 'authorization_code':
            code = 'rvlac_'+secrets.token_urlsafe(32)
            tx.put('research_oauth_code', sha(code), owner, {**value, 'expires_at': deadline(60), 'attempts': 0, 'used_at': None})
            result = {'redirect_url': _callback(value, code=code)}
        else: result = {'approved': True, 'local_work_id': work['id']}
    else:
        value.update(status='denied', owner_user_id=owner)
        result = {'redirect_url': _callback(value, error='access_denied')} if value['kind'] == 'authorization_code' else {'approved': False}
    value['decided_at'] = now(); tx.put('research_oauth_request', value['id'], owner, value)
    if value['kind'] == 'device':
        device = tx.get('research_oauth_device', value['device_sha256'])['data']
        tx.put('research_oauth_device', value['device_sha256'], owner, device)
    return result


def revoke_family(tx, row, reason):
    value = row['data']
    value.update(revoked_at=value.get('revoked_at') or now(), revocation_reason=reason)
    tx.put('research_oauth_family', value['id'], row['owner'], value)
    for grant in tx.list('research_access', row['owner']):
        if grant['data'].get('oauth_family_id') == value['id']:
            grant['data']['revoked_at'] = grant['data'].get('revoked_at') or now()
            tx.put('research_access', grant['id'], row['owner'], grant['data'])


def revoke_oauth_grant(tx, owner, grant_id):
    family = tx.get('research_oauth_family', grant_id)
    if family and family['owner'] == owner:
        revoke_family(tx, family, 'owner_revoked')


def _active_family(tx, family_id):
    row = tx.get('research_oauth_family', family_id)
    if (not row or row['data'].get('revoked_at') or row['owner'] != row['data']['issued_owner_user_id']
            or row['data']['expires_at'] <= now()):
        raise OAuthError('invalid_grant', 'The authorization expired or was revoked.')
    value = row['data']
    try: work = _work(tx, row['owner'], value['local_work_id'])
    except Problem: raise OAuthError('invalid_grant', 'The research owner or work is no longer authorized.') from None
    if work['research_request_id'] != value['research_request_id'] or work['package_sha256'] != value['package_sha256']:
        raise OAuthError('invalid_grant', 'The frozen research scope changed.')
    return row, work


def _tokens(tx, family, work, scopes, *, refresh=True):
    owner = family['issued_owner_user_id']; token = 'rvlm_'+secrets.token_urlsafe(32)
    expiry = min(deadline(ACCESS_TTL), family['expires_at'], work['expires_at'])
    grant = {'grant_id': family['id'], 'oauth_family_id': family['id'], 'client_id': family['client_id'],
        'resource': family['resource'], 'scopes': scopes, 'local_work_id': work['id'],
        'research_request_id': work['research_request_id'], 'kind': 'local', 'execution_id': None,
        'created_at': now(), 'expires_at': expiry, 'revoked_at': None}
    tx.put('research_access', sha(token), owner, grant)
    from datetime import datetime, timezone
    expires_in = max(0, int((datetime.fromisoformat(expiry.replace('Z', '+00:00'))-datetime.now(timezone.utc)).total_seconds()))
    result = {'access_token': token, 'token_type': 'Bearer', 'expires_in': expires_in, 'scope': ' '.join(scopes),
        'resource': family['resource'], 'local_work_id': work['id'], 'package_sha256': work['package_sha256'],
        'mcp_url': resource(), 'grant_id': family['id']}
    if refresh:
        secret = 'rvlrt_'+secrets.token_urlsafe(32)
        tx.put('research_oauth_refresh', sha(secret), owner, {'family_id': family['id'], 'client_id': family['client_id'],
            'resource': family['resource'], 'scopes': scopes, 'expires_at': family['expires_at'], 'used_at': None})
        result['refresh_token'] = secret
    return result


def _initial_tokens(tx, consented, client):
    owner = consented['owner_user_id']
    try: work = _work(tx, owner, consented['local_work_id'])
    except Problem: raise OAuthError('invalid_grant', 'The research owner or work is no longer authorized.') from None
    if work['research_request_id'] != consented['research_request_id'] or work['package_sha256'] != consented['package_sha256']:
        raise OAuthError('invalid_grant', 'The frozen research scope changed.')
    active = {row['data']['grant_id'] for row in tx.list('research_access', owner)
        if row['data']['local_work_id'] == work['id'] and not row['data'].get('revoked_at') and row['data']['expires_at'] > now()}
    families = {row['id'] for row in tx.list('research_oauth_family', owner)
        if row['data']['local_work_id'] == work['id'] and not row['data'].get('revoked_at') and row['data']['expires_at'] > now()}
    if len(active | families) >= 5: raise OAuthError('access_denied', 'Revoke an old connection before adding another.')
    refresh = 'refresh_token' in client['grant_types']
    family = {'id': uid(), 'issued_owner_user_id': owner, 'client_id': client['client_id'], 'resource': consented['resource'],
        'scopes': consented['scopes'], 'local_work_id': work['id'], 'research_request_id': work['research_request_id'],
        'package_sha256': work['package_sha256'], 'created_at': now(), 'expires_at': min(
            deadline(REFRESH_TTL if refresh else ACCESS_TTL), work['expires_at']), 'revoked_at': None}
    tx.put('research_oauth_family', family['id'], owner, family)
    return _tokens(tx, family, work, consented['scopes'], refresh=refresh), family['id']


def token(tx, params):
    client = _client(tx, params.get('client_id')); target = _resource(params.get('resource'))
    grant_type = params.get('grant_type')
    if grant_type not in client['grant_types']: raise OAuthError('unauthorized_client', 'This client did not register this grant type.')
    if params.get('client_secret') is not None: raise OAuthError('invalid_client', 'This endpoint accepts public clients only.', 401)
    if grant_type == 'authorization_code':
        code = _text(params.get('code'), 'code', maximum=200)
        row = tx.get('research_oauth_code', sha(code))
        if not row or row['data'].get('revoked_at'): raise OAuthError('invalid_grant', 'The authorization code is unavailable.')
        value = row['data']
        if (value['client_id'] != client['client_id'] or value['resource'] != target
                or params.get('redirect_uri') != value['redirect_uri']):
            raise OAuthError('invalid_grant', 'The authorization code does not match this client and callback.')
        if row['owner'] != value['owner_user_id'] or value['expires_at'] <= now() or value['attempts'] >= 5:
            raise OAuthError('invalid_grant', 'The authorization code expired or is unavailable.')
        verifier = params.get('code_verifier')
        if not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
            raise OAuthError('invalid_grant', 'A valid PKCE verifier is required.')
        actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if not hmac.compare_digest(value['code_challenge'], actual):
            value['attempts'] += 1; tx.put('research_oauth_code', sha(code), row['owner'], value)
            raise OAuthError('invalid_grant', 'The PKCE verifier does not match.')
        if value.get('used_at'):
            family = tx.get('research_oauth_family', value.get('family_id'))
            if family: revoke_family(tx, family, 'authorization_code_replayed')
            raise OAuthError('invalid_grant', 'The authorization code was already consumed.')
        result, family_id = _initial_tokens(tx, value, client)
        value.update(used_at=now(), family_id=family_id); tx.put('research_oauth_code', sha(code), row['owner'], value)
        return result
    if grant_type == DEVICE_GRANT:
        code = _text(params.get('device_code'), 'device_code', maximum=200)
        row = tx.get('research_oauth_device', sha(code))
        if not row or row['data'].get('revoked_at'): raise OAuthError('invalid_grant', 'The device code is unavailable.')
        value = row['data']
        if value['client_id'] != client['client_id'] or value['resource'] != target:
            raise OAuthError('invalid_grant', 'The device code belongs to a different client.')
        if value['expires_at'] <= now(): raise OAuthError('expired_token', 'The device authorization expired.')
        if value.get('used_at'): raise OAuthError('invalid_grant', 'The device authorization was already consumed.')
        timestamp = time.time(); previous = value.get('last_poll'); value['last_poll'] = timestamp
        too_fast = previous is not None and timestamp-previous < value['interval']
        if too_fast: value['interval'] += 5
        tx.put('research_oauth_device', sha(code), row['owner'], value)
        if too_fast: raise OAuthError('slow_down', 'Increase the polling interval by five seconds.')
        request = tx.get('research_oauth_request', value['request_id'])
        if not request or request['data'].get('revoked_at'): raise OAuthError('access_denied', 'The request is no longer authorized.')
        selected = request['data']
        if selected['status'] == 'pending': raise OAuthError('authorization_pending', 'Waiting for the user to approve in Reveal.')
        if selected['status'] != 'approved': raise OAuthError('access_denied', 'The user denied this authorization.')
        if request['owner'] != selected['owner_user_id'] or row['owner'] != selected['owner_user_id']:
            raise OAuthError('access_denied', 'The approving workspace changed.')
        result, family_id = _initial_tokens(tx, selected, client)
        value.update(used_at=now(), family_id=family_id); tx.put('research_oauth_device', sha(code), row['owner'], value)
        return result
    if grant_type == 'refresh_token':
        secret = _text(params.get('refresh_token'), 'refresh_token', maximum=200)
        row = tx.get('research_oauth_refresh', sha(secret))
        if not row: raise OAuthError('invalid_grant', 'The refresh token is unavailable.')
        value = row['data']
        if value['client_id'] != client['client_id'] or value['resource'] != target:
            raise OAuthError('invalid_grant', 'The refresh token belongs to a different client or resource.')
        family, work = _active_family(tx, value['family_id'])
        if row['owner'] != family['owner'] or value['expires_at'] <= now():
            raise OAuthError('invalid_grant', 'The refresh token expired or changed owners.')
        if value.get('used_at'):
            revoke_family(tx, family, 'refresh_token_replayed')
            raise OAuthError('invalid_grant', 'Refresh-token reuse revoked this connection; authorize again.')
        scopes = _scopes(params.get('scope'), value['scopes'])
        result = _tokens(tx, family['data'], work, scopes)
        value['used_at'] = now(); tx.put('research_oauth_refresh', sha(secret), row['owner'], value)
        return result
    raise OAuthError('unsupported_grant_type', 'This grant type is not supported.')


def revoke(tx, params):
    client = _client(tx, params.get('client_id')); secret = _text(params.get('token'), 'token', maximum=200)
    row = tx.get('research_oauth_refresh', sha(secret)) or tx.get('research_access', sha(secret))
    if not row or row['data'].get('client_id') != client['client_id']: return
    family_id = row['data'].get('family_id') or row['data'].get('oauth_family_id')
    family = tx.get('research_oauth_family', family_id) if family_id else None
    if family and family['data']['client_id'] == client['client_id']: revoke_family(tx, family, 'client_revoked')


def _perform(repository, request, label, action, *, public=True, limit=120):
    """Commit replay revocations and polling/rate counters even on protocol errors."""
    with repository.transaction() as tx:
        try:
            # Never trust caller-supplied forwarding headers for a rate identity.
            peer = request.client.host if request.client else 'unknown'
            _rate(tx, label, peer, limit, 60)
            result = action(tx)
        except (OAuthError, Problem) as error: result = error
    headers = {'Cache-Control': 'private, no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'no-referrer'}
    if public: headers['Access-Control-Allow-Origin'] = '*'
    if isinstance(result, OAuthError):
        return JSONResponse({'error': result.error, 'error_description': result.description}, status_code=result.status, headers=headers)
    if isinstance(result, Problem):
        if public:
            return JSONResponse({'error': 'access_denied', 'error_description': result.detail}, status_code=400, headers=headers)
        return JSONResponse({'code': result.code, 'detail': result.detail, **result.extra}, status_code=result.status, headers=headers)
    if isinstance(result, Response):
        result.headers.update(headers); return result
    return JSONResponse(result, headers=headers)


async def _body(request, *, form=False):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 16_384: raise OAuthError('invalid_request', 'The OAuth request is too large.', 413)
    try:
        if form:
            if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                raise OAuthError('invalid_request', 'Use application/x-www-form-urlencoded.')
            pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=True, max_num_fields=30)
            if len({key for key, _ in pairs}) != len(pairs): raise ValueError()
            return dict(pairs)
        value = json.loads(raw)
        if not isinstance(value, dict): raise ValueError()
        return value
    except (ValueError, UnicodeError): raise OAuthError('invalid_request', 'Supply an unambiguous OAuth request body.') from None


def register(app, repository):
    @app.get('/.well-known/oauth-authorization-server')
    def authorization_metadata(): return JSONResponse(metadata(), headers={'Access-Control-Allow-Origin': '*'})

    @app.get('/.well-known/oauth-protected-resource')
    @app.get('/.well-known/oauth-protected-resource/mcp')
    def protected_metadata(): return JSONResponse(resource_metadata(), headers={'Access-Control-Allow-Origin': '*'})

    @app.options('/oauth/{path:path}')
    def preflight(path: str):
        return Response(status_code=204, headers={'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, POST, OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type, Authorization'})

    async def parsed(request, label, action, *, form=False, public=True, limit=120):
        try: body = await _body(request, form=form)
        except OAuthError as error:
            return JSONResponse({'error': error.error, 'error_description': error.description}, status_code=error.status,
                headers={'Cache-Control': 'no-store'})
        return await asyncio.to_thread(_perform, repository(), request, label, lambda tx: action(tx, body), public=public, limit=limit)

    @app.post('/oauth/register', status_code=201)
    async def client_registration(request: Request):
        def action(tx, body):
            _rate(tx, 'registration-global', 'all', 1000, 86400)
            return JSONResponse(register_client(tx, body), status_code=201)
        return await parsed(request, 'registration', action, limit=10)

    @app.get('/oauth/authorize')
    def authorization(request: Request):
        def action(tx):
            pairs = list(request.query_params.multi_items())
            if len(pairs) > 20 or len({key for key, _ in pairs}) != len(pairs):
                raise OAuthError('invalid_request', 'Supply each OAuth query parameter once.')
            return RedirectResponse(authorize(tx, dict(pairs)), status_code=303)
        return _perform(repository(), request, 'authorization', action, limit=30)

    @app.post('/oauth/device_authorization')
    async def device(request: Request):
        return await parsed(request, 'device-authorization', device_authorization, form=True, limit=20)

    @app.post('/oauth/token')
    async def exchange(request: Request):
        return await parsed(request, 'token', token, form=True)

    @app.post('/oauth/revoke')
    async def revocation(request: Request):
        def action(tx, body):
            revoke(tx, body); return Response(status_code=200)
        return await parsed(request, 'revocation', action, form=True)

    @app.get('/v1/research-oauth/consent')
    def get_consent(request: Request):
        return _perform(repository(), request, 'browser-consent', lambda tx:
            consent_info(tx, request.headers.get('authorization'), dict(request.query_params)), public=False)

    @app.post('/v1/research-oauth/consent')
    async def decide(request: Request):
        return await parsed(request, 'browser-decision', lambda tx, body:
            consent(tx, request.headers.get('authorization'), body), public=False)
