# Deploying pibackup

## Server

On the host that stores backups:

```bash
pipx install git+https://github.com/kevinmcaleer/pibackup
cp deploy/pibackup-server.service ~/.config/systemd/user/
loginctl enable-linger "$USER"          # start at boot without a login
systemctl --user enable --now pibackup-server
```

Set a client-facing `repo_target` (and optionally `authorized_keys`) in
`~/.config/pibackup/config.toml` so enrolled Pis know where to rsync:

```toml
repo_target     = "backup@server:/srv/pibackup/repo"
authorized_keys = "/home/backup/.ssh/authorized_keys"
```

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
