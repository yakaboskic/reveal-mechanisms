#!/usr/bin/env bash
# Run as root on the dedicated Amazon Linux 2023 x86-64 host after copying the
# release bundle to /opt/reveal. Does not start workers or modify the database.
set -euo pipefail
umask 077
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
dnf install -y docker awscli-2 python3
systemctl enable --now docker amazon-ssm-agent
install -d -m 0755 /usr/local/lib/docker/cli-plugins
compose_tmp=$(mktemp -d)
trap 'rm -rf "$compose_tmp"' EXIT
curl --fail --silent --show-error --location \
  https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64 \
  -o "$compose_tmp/docker-compose-linux-x86_64"
curl --fail --silent --show-error --location \
  https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64.sha256 \
  -o "$compose_tmp/docker-compose-linux-x86_64.sha256"
(cd "$compose_tmp" && sha256sum -c docker-compose-linux-x86_64.sha256)
install -m 0755 "$compose_tmp/docker-compose-linux-x86_64" /usr/local/lib/docker/cli-plugins/docker-compose
install -d -m 0700 /etc/reveal
install -m 0644 /opt/reveal/deploy/aws/reveal.service /etc/systemd/system/reveal.service
systemctl daemon-reload
echo 'Host prepared. Install /etc/reveal/compose.env, complete the local handoff, then enable reveal.service.'
