"""Safe public failure details derived only from trusted runtime diagnostics."""
import math


def amount(value):
    return value if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


def review_failure(exc):
    audit = exc.audit
    blocked = audit.get('blocked_call') or {}
    limit, spent = amount(audit.get('configured_max_usd')), amount(audit.get('actual_cost_usd'))
    if limit is not None and spent is not None and (
            blocked.get('reason') == 'budget' or
            str(exc) == 'Scientific review remaining budget is insufficient'):
        return {'code': 'REVIEW_BUDGET_EXCEEDED', 'retryable': True,
            'message': f'Independent scientific review stopped to stay within its ${limit:.2f} budget. '
                f'Recorded review spend: ${spent:.4f}. The next call could exceed the remaining budget. '
                'No scientific verdict was reached. Generated output is retained; you can retry review without rerunning research.',
            'budget': {'scope': 'review', 'limit_usd': limit, 'spent_usd': spent,
                       'next_call_max_usd': amount(blocked.get('reserved_max_usd'))}}
    resources = {'request_bytes': 'context size in bytes', 'turns': 'model turns', 'input_tokens': 'input tokens'}
    resource = resources.get(blocked.get('reason'))
    if resource and amount(blocked.get('limit')) is not None and amount(blocked.get('observed')) is not None:
        return {'code': 'REVIEW_UNAVAILABLE', 'retryable': True,
            'message': f'Independent scientific review reached its configured limit for {resource}: '
                       f'{blocked["observed"]:,} requested or used; limit {blocked["limit"]:,}. '
                       'No scientific verdict was reached. Generated output is retained; you can retry review without rerunning research.'}
    return {'code': 'REVIEW_UNAVAILABLE', 'retryable': True,
            'message': 'The independent scientific review could not complete; no scientific verdict was reached. '
                       'Generated output is retained; you can retry review without rerunning research.'}


def authoring_failure(result, request):
    from .evidence_package import decode
    try:
        runtime = decode(result.runtime_manifest_path.read_bytes()) if result.runtime_manifest_path else {}
        completion = runtime.get('completion', {})
        if completion.get('provider_result_subtype') == 'error_max_budget_usd':
            limit = amount(completion.get('max_budget_usd')) or request.max_budget_usd
            spent = amount(completion.get('cost_usd'))
            cost = f'Recorded authoring spend: ${spent:.4f}.' if spent is not None else 'Final authoring spend was not reported.'
            message = (f'The statement writer stopped at its ${limit:.2f} budget. {cost} '
                       'No research statement was saved; your scientific account is unchanged.'
                       if getattr(request, 'kind', 'research') == 'paragraph' else
                       f'The research agent stopped at its ${limit:.2f} authoring budget. {cost} '
                       'No scientific result was accepted. Your draft and activity are saved.')
            return {'code': 'AUTHORING_BUDGET_EXCEEDED', 'retryable': True, 'message': message,
                'budget': {'scope': 'authoring', 'limit_usd': limit, 'spent_usd': spent, 'next_call_max_usd': None}}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {'code': 'AGENT_EXECUTION_FAILED', 'message': result.reason or 'The execution did not complete.', 'retryable': True}
