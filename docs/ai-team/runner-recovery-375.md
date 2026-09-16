# Issue #375 runner recovery evidence

Observed at: `2026-09-16T15:07:40Z`

Scope: production GitHub Actions runner `signal-engine-hyperliquid` for repository
`aliezzat4321/hyperliquid-copy-engine`. `REAL_TRADING_ENABLED=NO`.

## Recovery result

- Intended systemd unit discovered:
  `actions.runner.aliezzat4321-hyperliquid-copy-engine.signal-engine-hyperliquid.service`.
- Unit identity proof: its description names
  `aliezzat4321-hyperliquid-copy-engine.signal-engine-hyperliquid`, its `ExecStart` is
  `/home/hyperliquid-runner/runsvc.sh`, and its working directory is
  `/home/hyperliquid-runner`.
- `sudo -n true` result: exit code `1`. Sanitized stderr reported that
  `/etc/sudo.conf` is owned by UID 65534 rather than UID 0 and that the sandbox's
  `no new privileges` flag prevents sudo from running as root.
- Before active state: unobservable. `systemctl status` exited `1` with
  `Failed to connect to bus: Operation not permitted`.
- Restart attempted: no. The host systemd bus was inaccessible, noninteractive
  sudo was unavailable, and no existing narrowly scoped runner-restart helper was
  found in the checked conventional system paths.
- Restart succeeded: no; no restart was issued.
- After active state: unobservable because no restart was possible and the host
  systemd bus remained inaccessible.
- Orphan process touched: no. No matching `Runner.Listener`, `Runner.Worker`, or
  `/home/hyperliquid-runner/_work` process was visible in this PID namespace.

## GitHub job evidence

- Workflow run: `35091826262`.
- Job: `104779692531`.
- Issue-reported pre-recovery state: `queued`, `runner_id=0`, blank runner name.
- Post-attempt status/assignment: unobservable. A credential-free GitHub API query
  failed with curl exit code `6` because the sandbox could not resolve
  `api.github.com`; therefore assignment or transition to running is not claimed.

## Blocker

The assigned sandbox exposes the exact runner unit file read-only but does not
provide access to the host systemd bus, usable noninteractive sudo, a scoped
root-owned restart helper, the runner's host PID namespace, or outbound DNS for
GitHub status verification. A root supervisor with host systemd access must restart
only the unit named above and verify that job `104779692531` receives the restored
runner assignment.
