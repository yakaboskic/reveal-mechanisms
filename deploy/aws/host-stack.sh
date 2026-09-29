#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/reveal
export DOCKER_CONFIG=/run/reveal/docker
compose() {
  docker compose --env-file /etc/reveal/compose.env -f deploy/compose.yaml -f deploy/compose.ec2.yaml "$@"
}
case "${1:-}" in
  start)
    python3 deploy/aws/materialize-runtime.py
    mkdir -p "$DOCKER_CONFIG"
    aws ecr get-login-password --region us-east-1 |
      docker login --username AWS --password-stdin 005901288866.dkr.ecr.us-east-1.amazonaws.com
    compose pull api worker dispatcher redis caddy
    compose up -d --no-build --wait --wait-timeout 900 api worker dispatcher redis caddy
    compose run --rm --no-deps -T tools python -m reveal_backend.deployment resume
    ;;
  stop)
    compose run --rm --no-deps -T tools python -m reveal_backend.deployment drain --wait 1800
    compose stop
    ;;
  status)
    compose ps
    compose run --rm --no-deps -T tools python -m reveal_backend.deployment status
    ;;
  *) echo 'Usage: host-stack.sh start|stop|status' >&2; exit 2 ;;
esac
