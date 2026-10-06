#!/usr/bin/env python3
"""Create a private science:read key handoff without HTTP or deployment changes.

Use an existing owner-only (0700) output directory. Both outputs must be new;
rotation creates a new credential ID and never overwrites a previous handoff.
The environment file is used only to check the intended API URL. No principal
is created, account is read, or remote configuration installed.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import secrets
import sys
from uuid import uuid4

from issue_api_key import IssuanceError, Parser, api_base, private_output, save
from local_deployment import read_env
from reveal_backend.admin_read_keys import SCOPE, SETTINGS


def issue(args):
    base = api_base(args.api_url, read_env(args.env_file))
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', args.label):
        raise IssuanceError('Label must contain 1–64 letters, digits, dots, underscores or hyphens.')
    with ExitStack() as stack:
        output = stack.enter_context(private_output(args.output))
        config = stack.enter_context(private_output(args.config_output))
        key = 'rvl_admin_' + secrets.token_urlsafe(32)
        key_id = str(uuid4())
        save(config, dict(zip(SETTINGS, (hashlib.sha256(key.encode()).hexdigest(), key_id))))
        save(output, {'status': 'issued', 'api_url': base, 'api_key': key,
                      'key_id': key_id, 'scope': SCOPE, 'label': args.label,
                      'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})


def main(argv=None):
    parser = Parser(description=__doc__)
    for flag in ('env-file', 'api-url', 'label', 'output', 'config-output'):
        parser.add_argument('--' + flag, required=True,
                            type=Path if flag in ('env-file', 'output', 'config-output') else str)
    try:
        issue(parser.parse_args(argv))
    except IssuanceError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Issuance failed; no configuration was installed. Inspect private output files before retrying.',
              file=sys.stderr)
        return 1
    print('Private administrative read key handoff written. Deployment configuration was not changed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
