# Deploying pibackup

## Server

On the host that stores backups:

```bash
# The [server] extra pulls fastapi/uvicorn/jinja2/python-multipart (API + dashboard).
sudo pibackup admin install-service
```

That single command runs the server as a robust systemd **system** service: it
creates the `pibackup` system user, provisions the shared state dir at
`/var/lib/pibackup`, writes `/etc/systemd/system/pibackup-server.service`, and
enables + starts it. No login session, no linger, no session bus — manage it
with plain systemctl:

```bash
sudo systemctl status pibackup-server
sudo systemctl restart pibackup-server
```

> Prefer to do it by hand? Copy `deploy/pibackup-server.service` to
> `/etc/systemd/system/`, then `sudo systemctl daemon-reload && sudo systemctl
> enable --now pibackup-server`. Run `sudo pibackup admin install-service --dry-run`
> to see exactly what the command would do.

### Migrating from an older `--user` service

Earlier versions ran the server as a `--user` service that depended on
`loginctl enable-linger` and a session bus — fragile, and unmanageable if linger
was never enabled. To move an existing install onto the system service, pointing
it at the old database so clients/jobs/admin are preserved:

```bash
# stop + disable the old user service first (if it was running)
sudo -u pibackup XDG_RUNTIME_DIR=/run/user/$(id -u pibackup) \
  systemctl --user disable --now pibackup-server 2>/dev/null || true
sudo loginctl disable-linger pibackup 2>/dev/null || true

sudo pibackup admin install-service \
  --migrate-from /home/pibackup/.local/share/pibackup
```

The dashboard is locked until an administrator is set. Create one on the server:

```bash
sudo pibackup admin set-password        # prompts for username + password
```

Set a client-facing `repo_target` (and optionally `authorized_keys`) in
`/etc/pibackup/config.toml` so enrolled Pis know where to rsync:

```toml
repo_target     = "backup@server:/srv/pibackup/repo"
authorized_keys = "/home/backup/.ssh/authorized_keys"
```

## Server admin access (the `pibackup` group)

By default the server runs as a dedicated `pibackup` service user, which would
force every admin command through `sudo -u pibackup -H /home/pibackup/.local/bin/pibackup …`.
Instead, grant yourself **Docker-style** access: run admin commands as yourself
once you're in the `pibackup` group.

```bash
sudo pibackup admin enable-group           # adds $SUDO_USER to the group
sudo pibackup admin enable-group alice      # or name the operator explicitly
sudo pibackup admin enable-group --dry-run  # show what it'll do, change nothing
```

This creates the `pibackup` group, provisions a shared state dir at
`/var/lib/pibackup` (group-owned, setgid so new files inherit the group),
writes `/etc/pibackup/config.toml` pointing every operator at that shared DB,
and adds the operator to the group.

Then — exactly like Docker's `docker` group — **log out and back in once** (or
run `newgrp pibackup`) so the new group membership takes effect. After that you
run admin commands directly, as yourself:

```bash
pibackup client ls          # no sudo -u, no full path
pibackup enroll kitchen-pi
pibackup job ls --client dev01
```

> Security note: membership of the `pibackup` group grants full admin over the
> backups (read/write of the shared DB and repo) — the same trade-off Docker
> makes with its group. Only add trusted operators.

## Add a Pi (one-liner)

On the server: `pibackup enroll kitchen-pi` prints a bootstrap. On the new Pi
(run as **root** so backups can read every file, including root-only paths
under `/etc`):

```bash
curl -fsSL https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh \
  | sudo sh -s -- --server http://server:8765 --name kitchen-pi --token <token> --timer --agent
```

That installs pibackup for root, enrolls (generating + registering an SSH key),
enables the daily backup timer as a **system** service, and (with `--agent`)
enables the agent poller so server-issued start/stop commands are picked up
automatically. No SSH setup is needed on the client: backups use the enrolled
key automatically and trust the server's host key on first contact, so there's
nothing to add to `~/.ssh/config`.

Run client commands with `sudo` (e.g. `sudo pibackup run`, `sudo pibackup
status`) so they use root's config and can read everything.

## Upgrading

Once pibackup is installed, upgrade it in place with:

```bash
pibackup update            # pull the latest, migrate the database
pibackup update --dry-run  # show what it would do, change nothing
pibackup update --restart  # also restart any active pibackup services
```

`update` detects how pibackup was installed and upgrades the right way: a pipx
install runs `pipx upgrade pibackup` (reusing the recorded spec, so extras like
`[server]`/`[crypto]` are preserved); the `install.sh` venv fallback re-runs
`pip install --upgrade` against the same git spec, re-deriving its extras. It
then re-execs the freshly installed binary to run database migrations (so the
*new* migration code runs, not the old) and reports the old → new version.

Run it as the user that owns the install: on a server that's the service user
(`pibackup update`); on a client installed by `install.sh` that's root
(`sudo pibackup update`). If pibackup runs as a systemd service, those services
keep running the old code until restarted — `update` prints the exact
`systemctl restart` commands, or pass `--restart` to have it do them for you.

## Scheduling

Backups run on a **system** systemd timer (`pibackup-backup.timer`, daily with a
randomised delay, as root). The service runs `pibackup run` under `Nice=19` /
`IOSchedulingClass=idle`, and the client also wraps rsync in `nice`/`ionice`, so
backups stay out of the way of foreground work. Tune per-job throughput with
`bwlimit_kbps`.

## On-demand backups from the server

You can start (and stop) a backup from the server — via the dashboard's Start/Stop
buttons, `pibackup job start NAME` / `pibackup job stop NAME`, or the TUI (`s`/`x`).
The server queues a command for the client's job; the client acts on it on its next
poll. The recommended way to poll is the agent **system** service installed by
`install.sh --agent` (or `AGENT=1`), which runs `pibackup agent --interval 5`
under systemd with `Restart=on-failure`. See [Agent poller](#agent-poller) below
for the manual install and how to tune the interval.

You can also run the poller by hand:

```bash
pibackup agent            # long-running: poll every few seconds
pibackup agent --once     # drain the queue once (e.g. from a frequent timer)
```

A `start` runs the job immediately; a `stop` cancels an in-flight run (its rsync
is torn down and the run is recorded as a cancelled failure).

A `stop` only takes effect while the client is polling, and at worst one poll
interval after it's queued — with the default `--interval 5` that's ≈5 seconds.
Lower the interval for snappier stops, raise it to lighten server load.

## Manual timer install

```bash
sudo cp deploy/pibackup-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now pibackup-backup.timer
sudo systemctl list-timers pibackup-backup.timer
```

## Agent poller

The agent poller is a long-running **system** service that runs `pibackup agent`
and acts on server-issued start/stop commands. `install.sh --agent` (or `AGENT=1`)
installs and enables it automatically; to set it up by hand:

```bash
sudo cp deploy/pibackup-agent.service /etc/systemd/system/
sudo systemctl enable --now pibackup-agent.service
sudo systemctl status pibackup-agent.service
```

The unit runs `pibackup agent --interval 5`, polling every 5 seconds with
`Restart=on-failure`. Adjust the `--interval` in `ExecStart` to trade stop
latency against server load (a queued `stop` takes effect within one interval).
