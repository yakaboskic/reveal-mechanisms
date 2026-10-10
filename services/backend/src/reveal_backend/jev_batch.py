"""Bounded, resumable TypeSafe SystemOne requests and transparent demo ranking."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import json
import math
from pathlib import Path
import random
import re
import threading
import time
import traceback
import uuid

import httpx

from .dismech_import import canonical, digest, require

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
ERROR_BODY_LIMIT = 8192
# Live Jev responses report probabilities in hundredths and can total 0.99.
# Keep a fixed bound (not one that grows with option count) and preserve raw values.
PROBABILITY_SUM_TOLERANCE = .01
DIAGNOSTIC_HEADERS = ('content-type', 'retry-after', 'x-request-id', 'request-id',
                      'x-correlation-id', 'traceparent', 'cf-ray')


def redact(value, secrets=()):
    """Sanitize provider diagnostics before writing them to any output."""
    if isinstance(value, dict):
        return {key: '[REDACTED]' if re.search(
            r'authorization|api[_-]?key|secret|password|access[_-]?token|refresh[_-]?token|cookie', str(key), re.I)
            else redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret: value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', value)
    return value


def client_secrets(client):
    authorization = client.headers.get('authorization', '')
    return (authorization, authorization.partition(' ')[2]) if authorization else ()


def response_diagnostics(response, secrets=(), *, body=False):
    details = {'http_status': response.status_code,
               'response_headers': redact({key: response.headers[key] for key in DIAGNOSTIC_HEADERS
                                           if key in response.headers}, secrets)}
    if body:
        try:
            value = redact(response.json(), secrets)
            text = json.dumps(value, ensure_ascii=False)
            if isinstance(value, dict) and isinstance(value.get('detail'), dict):
                error_type = value['detail'].get('error_type')
                if isinstance(error_type, str): details['provider_error_type'] = error_type
            if details.get('provider_error_type') == 'max_tokens_exceeded':
                details['action_required'] = ('Reprepare into a new output directory with fewer factors/genes/comparison gaps. '
                    'Retrying this unchanged payload will not reduce its token count. '
                    'Current preparation defaults are 20 factors, 20 genes and 12 comparison gaps.')
        except (ValueError, TypeError):
            text = redact(response.text, secrets)
        encoded = text.encode('utf-8')
        details.update(response_body=encoded[:ERROR_BODY_LIMIT].decode('utf-8', errors='replace'),
                       response_body_truncated=len(encoded) > ERROR_BODY_LIMIT)
    return details


class RunLog:
    """One append-only, immediately flushed log shared by this run's workers."""
    def __init__(self, output, secrets=()):
        self.path = (Path(output) / 'run.log').resolve()
        self.run_id = uuid.uuid4().hex[:12]
        self.secrets = secrets
        self.lock = threading.Lock()
        self.handle = self.path.open('a', encoding='utf-8')

    def record(self, event, **fields):
        payload = json.dumps(redact(fields, self.secrets), ensure_ascii=False, sort_keys=True)
        line = f'{datetime.now(timezone.utc).isoformat()} run={self.run_id} {event} {payload}\n'
        with self.lock:
            self.handle.write(line)
            self.handle.flush()

    def close(self):
        self.handle.close()


def atomic_json(path, value):
    # Keep the transport/report usable without importing the NumPy/SciPy preparer.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(canonical(value) + '\n', encoding='utf-8')
    temporary.replace(path)


def validate_questions(questions):
    require(isinstance(questions, dict) and bool(questions), 'questions must be a nonempty object')
    for key, question in questions.items():
        require(isinstance(key, str) and isinstance(question, dict), 'Invalid question')
        kind = question.get('type')
        require(kind in ('choice', 'score', 'noul') and bool(question.get('instructions')), 'Invalid question type/instructions')
        criteria = question.get('criteria')
        if kind == 'choice':
            require(isinstance(criteria, dict) and 1 <= len(criteria) <= 255, 'Choice requires 1–255 options')
        elif kind == 'score':
            require(isinstance(criteria, list) and 2 <= len(criteria) <= 10, 'Score requires 2–10 ordered levels')
        elif criteria is not None:
            require(isinstance(criteria, dict) and set(criteria) <= {'true', 'false'}, 'Invalid Noul criteria')


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_response(response, questions):
    warnings = []
    require(isinstance(response, dict) and isinstance(response.get('model'), str) and bool(response['model']), 'Missing response model')
    answers = response.get('answers')
    require(isinstance(answers, dict) and set(questions) <= set(answers), 'Missing question answers')
    for key, question in questions.items():
        answer = answers[key]; kind = question['type']
        require(isinstance(answer, dict) and answer.get('type') == kind, f'Wrong answer type for {key}')
        if kind == 'noul':
            require(number(answer.get('noul'), 0, 1), f'Invalid Noul for {key}')
            continue
        require(number(answer.get('confidence'), 0, 1), f'Invalid confidence for {key}')
        if kind == 'choice':
            options = set(question['criteria'])
            require(answer.get('choice') in options, f'Unknown choice for {key}')
        else:
            options = {str(i) for i in range(len(question['criteria']))}
            require(number(answer.get('score'), 0, len(options) - 1), f'Invalid score for {key}')
        probabilities = answer.get('probabilities')
        # Older official examples omit Score probabilities; retain those responses.
        require(kind == 'score' or probabilities is not None, f'Missing probabilities for {key}')
        if probabilities is not None:
            require(isinstance(probabilities, dict) and set(probabilities) == options and
                    all(number(p, 0, 1) for p in probabilities.values()), f'Invalid probability distribution for {key}')
            total = math.fsum(probabilities.values())
            require(abs(total - 1) <= PROBABILITY_SUM_TOLERANCE + 1e-9,
                    f'Invalid probability distribution for {key}: sum={total:.12g}, '
                    f'expected 1 ± {PROBABILITY_SUM_TOLERANCE}')
            if abs(total - 1) > 1e-9:
                warnings.append({'question': key, 'kind': 'probability_sum_drift',
                                 'probability_sum': total, 'tolerance': PROBABILITY_SUM_TOLERANCE})
    usage = response.get('usage', {})
    require(isinstance(usage, dict), 'Invalid usage')
    for key in ('input_tokens', 'output_tokens'):
        if key in usage:
            require(type(usage[key]) is int and usage[key] >= 0, 'Invalid token usage')
    return warnings


class JevCallError(Exception):
    """A bounded live SystemOne failure. kind is timeout, too_large, unavailable or invalid; it never
    carries provider text, echoed input or credentials."""
    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def post_systemone(content, *, api_key, deadline, max_response_bytes=64_000):
    """One bounded HTTPS attempt for a product request: parsed JSON, or JevCallError.

    content is the caller's exact serialized body. There are no retries or redirects, and
    only the documented max_tokens_exceeded sizing error is recognized; other provider
    diagnostics are never read beyond that check.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise JevCallError('timeout')
    try:
        with httpx.Client(timeout=httpx.Timeout(remaining, connect=min(5, remaining)), follow_redirects=False,
                headers={'Authorization': 'Bearer '+api_key, 'Accept': 'application/json'}) as client:
            with client.stream('POST', ENDPOINT, content=content, headers={'Content-Type': 'application/json'}) as response:
                if response.status_code != 200:
                    if response.status_code in (400, 413, 422):
                        raw_error = bytearray()
                        for chunk in response.iter_bytes():
                            raw_error.extend(chunk)
                            if len(raw_error) > 4096 or time.monotonic() >= deadline: break
                        try: provider_error = json.loads(raw_error).get('detail', {})
                        except (ValueError, TypeError, AttributeError): provider_error = {}
                        if isinstance(provider_error, dict) and provider_error.get('error_type') == 'max_tokens_exceeded':
                            raise JevCallError('too_large')
                    raise JevCallError('unavailable')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline: raise JevCallError('timeout')
                    raw.extend(chunk)
                    if len(raw) > max_response_bytes: raise JevCallError('invalid')
    except httpx.TimeoutException:
        raise JevCallError('timeout') from None
    except httpx.HTTPError:
        raise JevCallError('unavailable') from None
    try: return json.loads(raw)
    except ValueError: raise JevCallError('invalid') from None


@contextmanager
def run_lock(output):
    with (Path(output) / '.run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another run/report is using this preparation') from error
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)


def read_preparation(output):
    output = Path(output)
    manifest = json.loads((output / 'manifest.json').read_text())
    require(manifest.get('format') == 'reveal.demo-prioritization/1', 'Unsupported preparation format')
    require(digest((output / 'gap-catalog.json').read_bytes()) == manifest['gap_catalog_sha256'], 'Gap catalog changed')
    seen = set()
    requests = []
    for entry in manifest['requests']:
        checksum = entry['request_sha256']
        require(len(checksum) == 64 and set(checksum) <= set('0123456789abcdef'), 'Invalid request checksum')
        require(checksum not in seen and entry['path'] == f'requests/{checksum}.json', 'Duplicate or invalid request path')
        seen.add(checksum)
        requests.append(entry)
    return manifest, ((entry, read_request(output, entry, manifest['model'])) for entry in requests)


def read_request(output, entry, model):
    body = json.loads((Path(output) / entry['path']).read_text())
    require(digest(canonical(body)) == entry['request_sha256'], 'Prepared request changed; create a new preparation to change inputs')
    require(body['state']['focal_gap']['source_id'] == entry['source_id'] and body['model'] == model, 'Request identity mismatch')
    validate_questions(body['questions'])
    return body


def read_result(output, entry, body):
    path = Path(output) / 'results' / (entry['request_sha256'] + '.json')
    if not path.exists(): return None
    result = json.loads(path.read_text())
    require(result.get('request_sha256') == entry['request_sha256'] and
            digest(canonical(result['response'])) == result.get('response_sha256'), 'Cached result checksum mismatch')
    validate_response(result['response'], body['questions'])
    return result


def retry_delay(header, attempt):
    try:
        value = float(header)
    except (TypeError, ValueError):
        try:
            value = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            value = 2 ** attempt + random.random()
    return max(0, value) if math.isfinite(value) else 2 ** attempt


def send_request(client, body, *, retries=2, sleep=time.sleep, log=lambda *args, **kwargs: None):
    start = time.monotonic()
    secrets = client_secrets(client)
    for attempt in range(retries + 1):
        retry_after = None
        attempt_start = time.monotonic()
        details = {}
        log('attempt_start', attempt=attempt + 1, maximum_attempts=retries + 1, model=body['model'])
        try:
            response = client.post(ENDPOINT, json=body)
            details = response_diagnostics(response, secrets)
            details['attempt_seconds'] = round(time.monotonic() - attempt_start, 3)
            if response.status_code == 200:
                value = None
                try:
                    value = response.json()
                    details['validation_warnings'] = validate_response(value, body['questions'])
                except (ValueError, TypeError, KeyError) as exc:
                    details.update(response_diagnostics(response, secrets, body=True))
                    details['validation_error'] = redact(f'{type(exc).__name__}: {exc}', secrets)
                    log('attempt_failed', attempt=attempt + 1, error='Invalid successful API response', **details)
                    return {'ok': False, 'error': 'Invalid successful API response', 'attempts': attempt + 1,
                            'seconds': round(time.monotonic() - start, 3), **details}
                log('attempt_succeeded', attempt=attempt + 1, response_model=value['model'], usage=value.get('usage'), **details)
                return {'ok': True, 'response': value, 'attempts': attempt + 1,
                        'seconds': round(time.monotonic() - start, 3), **details}
            error = f'HTTP {response.status_code}'
            details.update(response_diagnostics(response, secrets, body=True))
            log('attempt_failed', attempt=attempt + 1, error=error, **details)
            if response.status_code not in (429, 500, 502, 503, 504):
                return {'ok': False, 'error': error, 'attempts': attempt + 1, 'fatal_auth': response.status_code in (401, 403),
                        'seconds': round(time.monotonic() - start, 3), **details}
            retry_after = response.headers.get('retry-after')
        except httpx.TransportError as exc:
            error = type(exc).__name__
            details = {'exception_message': redact(str(exc), secrets),
                       'attempt_seconds': round(time.monotonic() - attempt_start, 3)}
            log('attempt_failed', attempt=attempt + 1, error=error, **details)
        if attempt < retries:
            delay = retry_delay(retry_after, attempt)
            if delay > 60:
                log('retry_deferred', attempt=attempt + 1, retry_after_seconds=delay)
                return {'ok': False, 'error': error, 'attempts': attempt + 1, 'retry_after_seconds': delay,
                        'seconds': round(time.monotonic() - start, 3), **details}
            log('retry_scheduled', next_attempt=attempt + 2, delay_seconds=delay)
            sleep(delay)
    return {'ok': False, 'error': error, 'attempts': retries + 1,
            'seconds': round(time.monotonic() - start, 3), **details}


def recover(output):
    """Revalidate complete saved HTTP 200 errors locally; never make API calls."""
    output = Path(output)
    journal = RunLog(output)
    print(f'Log: {journal.path}', flush=True)
    try:
        with run_lock(output):
            manifest, requests = read_preparation(output)
            summary = {'prepared': len(manifest['requests']), 'cached': 0,
                       'recovered_now': 0, 'unrecoverable_saved_errors': 0, 'http_attempts': 0}
            journal.record('recovery_start')
            for entry, body in requests:
                if read_result(output, entry, body) is not None:
                    summary['cached'] += 1
                    continue
                checksum = entry['request_sha256']
                path = output / 'errors' / f'{checksum}.json'
                if not path.exists(): continue
                try:
                    error = json.loads(path.read_text())
                    require(isinstance(error, dict) and error.get('request_sha256') == checksum and
                            error.get('source_id') == entry['source_id'] and
                            error.get('requested_model') == body['model'], 'Saved error identity mismatch')
                    require(error.get('http_status') == 200 and error.get('error') == 'Invalid successful API response',
                            'No saved successful HTTP response')
                    require(error.get('response_body_truncated') is False, 'Saved response body is truncated or unverified')
                    response = json.loads(error['response_body'])
                    warnings = validate_response(response, body['questions'])
                except (ValueError, TypeError, KeyError) as exc:
                    summary['unrecoverable_saved_errors'] += 1
                    journal.record('recovery_skipped', request_sha256=checksum,
                                   source_id=entry['source_id'], reason=str(exc))
                    continue
                # Retain the original error and completion timestamp for audit.
                result = {key: error[key] for key in ('request_sha256', 'source_id', 'requested_model',
                    'completed_at', 'attempts', 'seconds', 'http_status', 'response_headers', 'attempt_seconds') if key in error}
                result.update(ok=True, response=response, response_sha256=digest(canonical(response)),
                              validation_warnings=warnings, recovered_from=f'errors/{checksum}.json',
                              recovered_at=datetime.now(timezone.utc).isoformat())
                atomic_json(output / 'results' / f'{checksum}.json', result)
                event = {'event': 'response_recovered', **{key: value for key, value in result.items() if key != 'response'}}
                with (output / 'events.jsonl').open('a') as event_log:
                    event_log.write(canonical(event) + '\n')
                journal.record('response_recovered', **{key: value for key, value in event.items() if key != 'event'})
                summary['recovered_now'] += 1
            summary.update(pending_after=summary['prepared'] - summary['cached'] - summary['recovered_now'],
                           log_file=str(journal.path), run_id=journal.run_id)
            journal.record('recovery_finished', **summary)
            atomic_json(output / 'last-recovery.json', summary)
            return summary
    except BaseException as error:
        journal.record('recovery_aborted', error_type=type(error).__name__, error=str(error))
        raise
    finally:
        journal.close()


def run(output, *, api_key=None, send=False, batch_size=25, workers=4, limit=None, retries=2, timeout=120,
        client=None):
    secrets = (api_key,) + (client_secrets(client) if client is not None else ())
    journal = RunLog(output, secrets)
    print(f'Log: {journal.path}', flush=True)
    try:
        journal.record('run_start', send=send, batch_size=batch_size, workers=workers, limit=limit,
                       retries=retries, timeout_seconds=timeout, endpoint=ENDPOINT)
        result = _run(output, api_key=api_key, send=send, batch_size=batch_size, workers=workers,
                      limit=limit, retries=retries, timeout=timeout, client=client, log=journal.record)
        result.update(log_file=str(journal.path), run_id=journal.run_id)
        journal.record('run_finished', **result)
        return result
    except BaseException as error:
        journal.record('run_aborted', error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        journal.close()


def _run(output, *, api_key=None, send=False, batch_size=25, workers=4, limit=None, retries=2, timeout=120,
         client=None, log=lambda *args, **kwargs: None):
    require(batch_size >= 1 and 1 <= workers <= 16 and 0 <= retries <= 10 and timeout > 0, 'Invalid batch settings')
    require(limit is None or limit > 0, '--limit must be positive')
    output = Path(output)
    with run_lock(output):
        manifest, requests = read_preparation(output)
        pending = [entry for entry, body in requests if read_result(output, entry, body) is None]
        selected = pending[:limit] if limit else pending
        prepared_count = len(manifest['requests'])
        summary = {'send': send, 'prepared': prepared_count, 'blocked_requests': len(manifest.get('blocked', [])),
            'cached': prepared_count - len(pending),
            'pending': len(pending), 'selected': len(selected), 'maximum_http_attempts': len(selected) * (retries + 1),
            'estimated_input_tokens_one_attempt': sum(entry['estimated_input_tokens'] for entry in selected),
            'estimate_note': manifest['token_estimate_method'], 'succeeded_now': 0, 'failed_now': 0}
        log('run_plan', **summary)
        if not send or not selected: return summary
        require(bool(api_key) or client is not None, 'Set TYPESAFE_API_KEY in the root .env or environment')
        owned_client = client is None
        client = client or httpx.Client(headers={'Authorization': f'Bearer {api_key}'},
                                        timeout=timeout, follow_redirects=False)
        stop = False
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for start in range(0, len(selected), batch_size):
                    futures = {}
                    log('batch_start', batch_number=start // batch_size + 1,
                        requests=min(batch_size, len(selected) - start))
                    for entry in selected[start:start + batch_size]:
                        body = read_request(output, entry, manifest['model'])
                        # Bind each entry now: worker threads must not capture the loop's final entry.
                        def request_log(event, entry=entry, **details):
                            log(event, request_sha256=entry['request_sha256'], source_id=entry['source_id'], **details)
                        futures[pool.submit(send_request, client, body, retries=retries, log=request_log)] = (entry, body)
                    for future in as_completed(futures):
                        entry, body = futures[future]
                        result = future.result()
                        checksum = entry['request_sha256']
                        result.update(request_sha256=checksum, source_id=entry['source_id'],
                                      requested_model=body['model'], completed_at=datetime.now(timezone.utc).isoformat())
                        if result['ok']:
                            result['response_sha256'] = digest(canonical(result['response']))
                            atomic_json(output / 'results' / f'{checksum}.json', result)
                            summary['succeeded_now'] += 1
                        else:
                            atomic_json(output / 'errors' / f'{checksum}.json', result)
                            summary['failed_now'] += 1
                            stop |= result.get('fatal_auth', False) or result.get('retry_after_seconds', 0) > 60
                            detail = result.get('action_required') or result.get('validation_error') or result.get('response_body') or result.get('exception_message', '')
                            detail = json.dumps(detail, ensure_ascii=False)[1:-1][:300]
                            print(f"Failed {checksum[:12]}: {result['error']} — {detail}", flush=True)
                        with (output / 'events.jsonl').open('a') as event_log:
                            event_log.write(canonical({key: value for key, value in result.items() if key != 'response'}) + '\n')
                        completed = summary['succeeded_now'] + summary['failed_now']
                        log('request_finished', **{key: value for key, value in result.items() if key != 'response'})
                        print(f"Completed {completed}/{len(selected)}: {summary['succeeded_now']} succeeded, {summary['failed_now']} failed", flush=True)
                    if stop: break
        finally:
            if owned_client: client.close()
        summary['pending_after'] = len(pending) - summary['succeeded_now']
        atomic_json(output / 'last-run.json', summary)
        return summary


WEIGHTS = {'evidence_fit': .4, 'answerability': .3, 'demo_clarity': .2, 'distinctiveness': .1}


def diverse_candidates(rows, size, max_per_disease):
    selected, diseases = [], defaultdict(int)
    for row in rows:
        if len(selected) >= size: break
        if diseases[row['disease']] >= max_per_disease: continue
        candidate = set(row['retrieved_top5_anchors'])
        if any(len(candidate & set(other['retrieved_top5_anchors'])) /
               max(1, len(candidate | set(other['retrieved_top5_anchors']))) > .6 for other in selected):
            continue
        selected.append(row)
        diseases[row['disease']] += 1
    return selected


def shortlist_diagnostics(ranked):
    # Explain the conjunction of gates; counts per flag overlap, the funnel does not.
    gates = ('weak_evidence_or_answerability', 'no_defensible_anchor',
             'low_prioritization_probability', 'overclaim_risk', 'low_model_confidence')
    remaining = ranked
    funnel = []
    for gate in gates:
        before = len(remaining)
        remaining = [row for row in remaining if gate not in row['review_flags']]
        funnel.append({'gate': gate, 'excluded_at_step': before - len(remaining), 'remaining': len(remaining)})
    return {'ranked': len(ranked), 'exclusion_counts_overlap': True,
            'exclusion_counts': {gate: sum(gate in row['review_flags'] for row in ranked) for gate in gates},
            'main_blocker_counts': dict(Counter(row['main_blocker'] for row in ranked)),
            'gate_funnel': funnel, 'eligible_before_diversity': len(remaining),
            'confidence_only_exclusions': sum(row['review_flags'] == ['low_model_confidence'] for row in ranked)}


def write_review_candidates(output, candidates, min_confidence):
    lines = ['# Demo candidates for manual review', '',
        'These candidates pass the evidence-fit, answerability, prioritization, overclaim-risk, '
        'and anchor checks. They fail only the score-confidence gate and remain outside the automatic shortlist.', '',
        f'The confidence gate requires all four rubric confidences to be at least {min_confidence:g}. '
        'Confidence describes how concentrated the model response is; it is not a calibrated probability that a demo is good.', '',
        'Scores come from the saved Jev responses. Validate the selected factor, genes, and current REVEAL evidence before choosing a demo.', '',
        f'Candidates after the same disease and factor-overlap limits: {len(candidates)}.', '']
    for i, row in enumerate(candidates, 1):
        lines.extend([f"## {i}. {row['disease']}", '', row['question'].strip(), '',
            f"Overall rank: {row['rank']}. Demo score: {row['demo_score_100']:g}/100. "
            f"Prioritization output: {row['prioritize_probability']:g}.", '',
            f"Evidence fit: {row['evidence_fit']:g}/4. Answerability: {row['answerability']:g}/4. "
            f"Overclaim-risk output: {row['overclaim_probability']:g}.", '',
            'Rubric confidences: ' + ', '.join(f'{key}={value:g}' for key, value in row['rubric_confidences'].items()) + '.', '',
            f"Anchor: {row['best_anchor_label']} (`{row['best_anchor']}`). "
            f"Main blocker: `{row['main_blocker']}`.", '',
            f"Review flag: `low_model_confidence`. Source: `{row['source_id']}`.", '',
            f"[Prepared request]({row['request_path']})", ''])
    (output / 'review-candidates.md').write_text('\n'.join(lines), encoding='utf-8')


def report(output, *, shortlist_size=10, max_per_disease=2, min_confidence=.5):
    require(shortlist_size > 0 and max_per_disease > 0 and number(min_confidence, 0, 1), 'Invalid shortlist settings')
    output = Path(output)
    with run_lock(output):
        manifest, requests = read_preparation(output)
        ranked, raw_answers, models = [], [], set()
        usage = {'input_tokens': 0, 'output_tokens': 0, 'responses_missing_usage': 0}
        for entry, body in requests:
            result = read_result(output, entry, body)
            if result is None: continue
            response = result['response']; answers = response['answers']; models.add(response['model'])
            raw_answers.append({'source_id': entry['source_id'], 'response': response})
            token_usage = response.get('usage', {})
            for key in ('input_tokens', 'output_tokens'): usage[key] += token_usage.get(key, 0)
            if not all(key in token_usage for key in ('input_tokens', 'output_tokens')): usage['responses_missing_usage'] += 1
            if manifest['rubric_version'] != 'demo-grounded-story-v1': continue
            scores = {key: answers[key]['score'] for key in WEIGHTS}
            confidences = {key: answers[key]['confidence'] for key in WEIGHTS}
            confidence = min(confidences.values())
            best = answers['best_anchor']['choice']
            factors = body['state']['eaggl']['factors']
            anchor = next((row for row in factors if row['ref'] == best), None)
            review = []
            if answers['prioritize']['noul'] < .5: review.append('low_prioritization_probability')
            if confidence < min_confidence: review.append('low_model_confidence')
            if answers['overclaim_risk']['noul'] >= .5: review.append('overclaim_risk')
            if scores['evidence_fit'] < 2 or scores['answerability'] < 2: review.append('weak_evidence_or_answerability')
            if anchor is None: review.append('no_defensible_anchor')
            ranked.append({'source_id': entry['source_id'], 'disease': entry['disease'], 'question': entry['question'],
                'demo_score_100': round(25 * sum(scores[key] * weight for key, weight in WEIGHTS.items()), 3),
                'prioritize_probability': answers['prioritize']['noul'], **scores,
                'minimum_rubric_confidence': confidence, 'rubric_confidences': confidences,
                'overclaim_probability': answers['overclaim_risk']['noul'],
                'main_blocker': answers['main_blocker']['choice'], 'best_anchor': anchor['cfde_node_id'] if anchor else None,
                'best_anchor_label': anchor['label'] if anchor else None, 'review_flags': review,
                'retrieved_top5_anchors': [row['cfde_node_id'] for row in factors[:5]],
                'response_model': response['model'], 'request_path': entry['path']})
        ranked.sort(key=lambda row: (-row['demo_score_100'], -row['prioritize_probability'], row['source_id']))
        for rank, row in enumerate(ranked, 1):
            row['rank'] = rank
        shortlist = diverse_candidates([row for row in ranked if not row['review_flags']], shortlist_size, max_per_disease)
        review_candidates = diverse_candidates([row for row in ranked if row['review_flags'] == ['low_model_confidence']],
                                               shortlist_size, max_per_disease)
        result = {'prepared': len(manifest['requests']), 'completed': len(raw_answers), 'pending': len(manifest['requests']) - len(raw_answers),
            'blocked_requests': len(manifest.get('blocked', [])),
            'rubric_version': manifest['rubric_version'], 'models_returned': sorted(models), 'mixed_models': len(models) > 1,
            'usage_successful_responses': usage, 'score_formula': '25 * (0.4 evidence_fit + 0.3 answerability + 0.2 demo_clarity + 0.1 distinctiveness)',
            'shortlist_policy': {'size': shortlist_size, 'max_per_disease': max_per_disease, 'min_confidence': min_confidence,
                'max_top5_anchor_jaccard': .6, 'exclude_review_flags': True},
            'shortlist_diagnostics': shortlist_diagnostics(ranked),
            'review_candidate_policy': {'required_review_flags': ['low_model_confidence'],
                'all_other_gates_required': True, 'size': shortlist_size,
                'max_per_disease': max_per_disease, 'max_top5_anchor_jaccard': .6,
                'requires_manual_review': True},
            'limitations': ['Triage scores and model confidence are not scientific probabilities.',
                'Cross-atlas bindings and live evidence availability still need validation.',
                'Token totals exclude unreported failed/retried requests; not a billing total.',
                'Review candidates fail the confidence gate and require manual validation; they are not automatically approved demos.',
                'Scores use only each request\'s comparison subset; shortlist diversity is a deterministic heuristic.'],
            'shortlist': shortlist, 'review_candidates': review_candidates, 'ranked': ranked}
        atomic_json(output / 'report.json', result)
        atomic_json(output / 'answers.json', raw_answers)
        write_review_candidates(output, review_candidates, min_confidence)
        if ranked:
            columns = [key for key in ranked[0] if key != 'retrieved_top5_anchors']
            with (output / 'ranking.csv').open('w', newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
                writer.writeheader()
                for row in ranked:
                    writer.writerow({**row, 'review_flags': ';'.join(row['review_flags']),
                                     'rubric_confidences': canonical(row['rubric_confidences'])})
        return {key: value for key, value in result.items() if key not in ('ranked', 'shortlist', 'review_candidates')} | {
            'shortlist_count': len(shortlist), 'review_candidate_count': len(review_candidates)}
