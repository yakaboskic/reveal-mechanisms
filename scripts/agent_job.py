#!/usr/bin/env python3
"""Run a REVEAL general agent job: submit a prompt, watch its progress, print the final answer.

Standard library only. Credentials come from a handoff JSON (--handoff, with api_url and api_key) or the
REVEAL_API_URL and REVEAL_API_KEY environment variables.

  python3 agent_job.py --handoff my-key.json --prompt "Summarize ..." \\
      --tool Read --tool Write --tool Bash --allow-host eutils.ncbi.nlm.nih.gov

To get a DAPPER Scientific Account built from papers the agent reads, give it a question (and any claims to assess):

  python3 agent_job.py --handoff my-key.json --prompt "Prefer primary studies." \\
      --question "Does loss of LEPR signalling cause hepatic insulin resistance?" \\
      --claim "Hepatocyte LEPR loss reduces insulin-stimulated AKT phosphorylation." --account-out account.json

Ctrl-C stops watching; the job keeps running. Resume with --job <id>, or stop it with --cancel <id>.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

TERMINAL = {'succeeded', 'failed', 'cancelled', 'insufficient_evidence'}


class Client:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip('/'), key

    def call(self, method, path, body=None, idempotency=None):
        headers = {'Authorization': 'Bearer ' + self.key, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode(); headers['Content-Type'] = 'application/json'
        if idempotency: headers['Idempotency-Key'] = idempotency
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read() or b'null')
            except urllib.error.HTTPError as error:
                problem = error.read().decode(errors='replace')
                if error.code in (429, 502, 503, 504) and attempt < 4:
                    time.sleep(2 * (attempt + 1)); continue
                try: problem = json.loads(problem); detail = f"{problem.get('code')}: {problem.get('detail')}"
                except ValueError: detail = problem[:500]
                sys.exit(f'{method} {path} failed with HTTP {error.code}. {detail}')
            except urllib.error.URLError as error:
                if attempt < 4: time.sleep(2 * (attempt + 1)); continue
                sys.exit(f'Cannot reach {self.url}: {error.reason}')


def credentials(args):
    if args.handoff:
        handoff = json.loads(Path(args.handoff).read_text())
        return handoff['api_url'], handoff['api_key']
    url, key = os.environ.get('REVEAL_API_URL'), os.environ.get('REVEAL_API_KEY')
    if not url or not key: sys.exit('Pass --handoff <file.json> or set REVEAL_API_URL and REVEAL_API_KEY.')
    return url, key


def request_body(args):
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else args.prompt
    if not prompt: sys.exit('Pass --prompt or --prompt-file.')
    body = {'kind': 'agent', 'prompt': prompt}
    system = Path(args.system_file).read_text() if args.system_file else args.system
    if system: body['system_prompt'] = system
    if args.tool: body['tools'] = args.tool
    if args.allow_host: body['allowed_domains'] = args.allow_host
    if args.graph: body['graphs'] = args.graph
    if args.literature: body['literature'] = True
    if args.question:
        body.update(deliverable='scientific_account', question=args.question)
        if args.claim: body['claims'] = args.claim
        if args.attribute_requester: body['attribute_requester'] = True
    elif args.claim or args.attribute_requester:
        sys.exit('--claim and --attribute-requester belong to a Scientific Account job: pass --question as well.')
    if args.claims_repository is not None: body['claims_repository'] = args.claims_repository
    for name in ('network', 'effort', 'model'):
        if getattr(args, name): body[name] = getattr(args, name)
    limits = {key: value for key, value in (('timeout_seconds', args.timeout), ('max_turns', args.turns),
                                            ('max_budget_usd', args.budget), ('tool_calls', args.tool_calls),
                                            ('literature_searches', args.searches), ('literature_search_results', args.search_results),
                                            ('literature_papers', args.papers), ('literature_characters', args.paper_characters))
              if value is not None}
    if limits: body['limits'] = limits
    return body


def show(event, state):
    """One readable line per event; the agent's streamed text is printed as it arrives."""
    detail = event.get('detail') or {}
    kind = detail.get('kind')
    if kind == 'agent_message':
        text = event.get('message') or ''
        if not state['in_text']: sys.stdout.write('\n  │ '); state['in_text'] = True
        sys.stdout.write(text.replace('\n', '\n  │ ')); sys.stdout.flush()
        if not detail.get('message_delta'): sys.stdout.write('\n'); state['in_text'] = False
        return
    if state['in_text']: sys.stdout.write('\n'); state['in_text'] = False
    if kind == 'tool_call':
        arguments = detail.get('display_arguments') or ''
        print(f"  → {detail.get('tool_name')} {arguments[:300]}")
    elif kind == 'tool_result':
        status = '✗' if detail.get('state') == 'failed' else '✓'
        excerpt = (detail.get('output_excerpt') or '').strip().replace('\n', ' ')
        print(f"    {status} {excerpt[:200]}")
    elif event.get('stage') != state['stage'] or event.get('event_type') in ('status', 'warning', 'failure', 'result'):
        print(f"[{event.get('stage')}] {event.get('message') or ''}".rstrip())
    state['stage'] = event.get('stage')


def watch(client, job_id, poll):
    state, after = {'stage': None, 'in_text': False}, '0'
    while True:
        page = client.call('GET', f'/v1/jobs/{job_id}/events?after={after}&limit=100')
        for event in page['items']: show(event, state)
        after = page['next_after']
        if page['terminal']: break
        if not page['items']: time.sleep(poll)
    if state['in_text']: print()
    return client.call('GET', f'/v1/jobs/{job_id}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--handoff', help='Key handoff JSON with api_url and api_key')
    parser.add_argument('--prompt'); parser.add_argument('--prompt-file')
    parser.add_argument('--system', help='Appended to the agent system prompt'); parser.add_argument('--system-file')
    parser.add_argument('--tool', action='append', help='Read, Write, Edit, Glob, Grep, Bash, WebSearch, WebFetch (repeat)')
    parser.add_argument('--allow-host', action='append', help='Extra host the restricted network may reach (repeat)')
    parser.add_argument('--graph', action='append', help='OKN knowledge graph for the graph tools (repeat)')
    parser.add_argument('--literature', action='store_true', help='Europe PMC search_papers / read_paper tools')
    parser.add_argument('--question', help='Return a DAPPER Scientific Account that addresses this question, built from papers '
                                           'the agent reads (sets deliverable scientific_account)')
    parser.add_argument('--claim', action='append', help='With --question: a statement to assess from the literature (repeat)')
    parser.add_argument('--attribute-requester', action='store_true',
                        help='With --question: name you in the account as the Person it is attributed to (default: not named)')
    parser.add_argument('--claims-repository', action=argparse.BooleanOptionalAction, default=None,
                        help='Offer (or withhold) the query_claims tool. Default: on for --question jobs, off otherwise')
    parser.add_argument('--account-out', help='With --question or --job: save the account document (JSON) to this file')
    parser.add_argument('--network', choices=('restricted', 'open'))
    parser.add_argument('--effort', choices=('low', 'medium', 'high'))
    parser.add_argument('--model')
    # Each limit has a server default and ceiling an operator sets; a value above the ceiling is refused and the message names it.
    parser.add_argument('--timeout', type=int, help='Seconds, 60-3600 (built-in default 900)')
    parser.add_argument('--turns', type=int, help='Max turns (built-in default 50, ceiling 100)')
    parser.add_argument('--budget', type=float, help='Max dollars (built-in default 2, ceiling 5)')
    parser.add_argument('--tool-calls', type=int, help='Paper and graph tool calls in all (built-in default 30)')
    parser.add_argument('--searches', type=int, help='Paper searches (built-in default 3)')
    parser.add_argument('--search-results', type=int, help='Most results per paper search (built-in default 5)')
    parser.add_argument('--papers', type=int, help='Distinct papers to read (built-in default 4)')
    parser.add_argument('--paper-characters', type=int, help='Characters of paper text across all reads (built-in default 48000)')
    parser.add_argument('--job', help='Watch an existing job instead of submitting')
    parser.add_argument('--cancel', metavar='JOB_ID', help='Ask a running job to stop')
    parser.add_argument('--out', help='Also save the final answer to this file')
    parser.add_argument('--poll', type=float, default=3, help='Seconds between event checks (default 3)')
    args = parser.parse_args()
    client = Client(*credentials(args))
    if args.cancel:
        job = client.call('POST', f'/v1/jobs/{args.cancel}/cancel')
        return print(f"Job {job['id']}: {job['status']}")
    if args.job:
        job_id = args.job
    else:
        job = client.call('POST', '/v1/jobs', request_body(args), idempotency=str(uuid.uuid4()))
        job_id = job['id']
        print(f'Job {job_id} queued. Ctrl-C stops watching; the job keeps running (resume with --job {job_id}).')
    try:
        job = watch(client, job_id, args.poll)
    except KeyboardInterrupt:
        sys.exit(f'\nStopped watching. Resume: --job {job_id}   Stop the job: --cancel {job_id}')
    print(f"\nStatus: {job['status']}")
    if job.get('failure'): print(f"Failure: {job['failure'].get('code')}: {job['failure'].get('message')}")
    result = job.get('result') or {}
    if result.get('kind') == 'agent':
        print(f"Turns: {result.get('turns')}  Cost: ${result.get('cost_usd') or 0:.2f}  Time: {result.get('duration_seconds')} s")
        if result.get('files'): print('Files written:', ', '.join(f"{f['path']} ({f['size_bytes']} B)" for f in result['files']))
        if result.get('response_truncated'): print('(The answer was cut at 50,000 characters.)')
        print('\n' + result.get('response', ''))
        if args.out: Path(args.out).write_text(result.get('response', '')); print(f'\nSaved to {args.out}')
        account = result.get('account')
        if account:
            deposit = account['deposit']
            outcome = {'deposited': f"deposited as {deposit.get('deposit_id')}", 'not_configured': 'not deposited (no claims repository configured)'}.get(
                deposit['status'], f"deposit failed ({deposit.get('reason')}); the account is kept with the job")
            print(f"\nScientific Account {account['account_id']} (DAPPER {account['dapper_version']}): {account['claims']} claims, "
                  f"{account['value_locators']} located quotes from {len(account['papers'])} paper texts; {outcome}.")
            for claim in account['requested_claims']:
                print(f"  {'assessed    ' if claim['assessed'] else 'not assessed'}  {claim['statement']}")
            if args.account_out:
                document = client.call('GET', f'/v1/jobs/{job_id}/account')['document']
                Path(args.account_out).write_text(json.dumps(document, indent=1, ensure_ascii=False) + '\n')
                print(f'Account document saved to {args.account_out}')
    return 0 if job['status'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
