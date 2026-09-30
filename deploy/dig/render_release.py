#!/usr/bin/env python3
"""Render the durable REVEAL HTTP service; never access secrets or deploy resources."""
import argparse
import json
from pathlib import Path
import re

import yaml

from dig_platform.render import render_template, stack_parameters
from dig_platform.schema import load_service_spec


def external_network(template, security_group):
    """Use the pre-provisioned client SG, allowing dependencies on first boot."""
    resources = template['Resources']
    groups = {key for key, value in resources.items() if value['Type'] == 'AWS::EC2::SecurityGroup'}
    def rewrite(value):
        if isinstance(value, dict):
            if set(value) == {'Ref'} and value['Ref'] in groups:
                return security_group
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value
    for key in groups:
        del resources[key]
    # A retired logical SG may still have an output; refs are rewritten above.
    return rewrite(template)


def render(base, service, secret_arn, client_sg, image):
    if not re.fullmatch(r'arn:aws:secretsmanager:us-east-1:005901288866:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}', secret_arn):
        raise ValueError('Use the full existing backend Secrets Manager ARN, without JSON selectors')
    if not re.fullmatch(r'sg-[a-f0-9]{8}(?:[a-f0-9]{9})?', client_sg):
        raise ValueError('Use the ClientSecurityGroupId from the network prerequisite stack')
    if not re.fullmatch(r'005901288866\.dkr\.ecr\.us-east-1\.amazonaws\.com/dig-reveal@sha256:[a-f0-9]{64}', image):
        raise ValueError('Use an immutable dig-reveal image digest')
    raw = yaml.safe_load(service)
    if raw['name'] != 'reveal' or raw['path_pattern'] != '/api/reveal/*':
        raise ValueError('This renderer is only for the reveal service')
    raw['secrets'] = {key: f'{secret_arn}:{key}::' for key in raw['secrets']}
    spec = load_service_spec(yaml.safe_dump(raw))
    # Exactly one environment is enabled until separate data/queue ownership exists.
    template = json.loads(render_template(base, spec, 'qa'))
    params = stack_parameters(spec, 'qa', image)
    return template, params


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--service', type=Path, required=True)
    parser.add_argument('--secret-arn', required=True)
    parser.add_argument('--client-security-group', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    api, params = render(args.base.read_text(), args.service.read_text(), args.secret_arn,
                         args.client_security_group, args.image)
    args.out.mkdir(parents=True, exist_ok=False)
    api_path, params_path = args.out / 'api.json', args.out / 'api-params.json'
    api_path.write_text(json.dumps(api, indent=2) + '\n')
    params_path.write_text(json.dumps(params, indent=2) + '\n')
    output = external_network(json.loads(api_path.read_text()), args.client_security_group)
    api_path.write_text(json.dumps(output, indent=2) + '\n')
    print(f'Rendered durable HTTP service for qa to {args.out}; no resources deployed.')


if __name__ == '__main__':
    main()
