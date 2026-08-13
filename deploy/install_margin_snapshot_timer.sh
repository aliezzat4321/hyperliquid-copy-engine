#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
service_src="$repo_dir/deploy/systemd/hlcopy-margin-snapshot.service"
timer_src="$repo_dir/deploy/systemd/hlcopy-margin-snapshot.timer"
service_dst="/etc/systemd/system/hlcopy-margin-snapshot.service"
timer_dst="/etc/systemd/system/hlcopy-margin-snapshot.timer"
python_bin="$repo_dir/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  echo "missing virtualenv python: $python_bin" >&2
  exit 1
fi

sed \
  -e "s|^ExecStart=.*|ExecStart=$python_bin -m hlcopy.profitability.margin_snapshot_cli --all-dexes --output $repo_dir/data/research/margin_metadata.jsonl|" \
  -e "/^Type=oneshot/a WorkingDirectory=$repo_dir\nEnvironmentFile=-$repo_dir/.env" \
  "$service_src" > "$service_dst"
install -m 0644 "$timer_src" "$timer_dst"

systemctl daemon-reload
systemctl enable --now hlcopy-margin-snapshot.timer
systemctl start hlcopy-margin-snapshot.service
systemctl --no-pager --full status hlcopy-margin-snapshot.service || true
systemctl --no-pager --full status hlcopy-margin-snapshot.timer
