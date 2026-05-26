# pibackup

Self-contained backup system for Raspberry Pi. Pi clients push their files to a
central server that owns the repository, tracks job status and retention, and
serves a dashboard. CLI-first with a Docker-style command grammar.

> Status: **all phases complete (0–8).** A REST API hosts job config and
> run/snapshot reporting; clients enroll with a one-line bootstrap, rsync to the
> repo (under nice/ionice + a daily systemd timer), and report results, with the
> server pruning expired snapshots and serving a status dashboard. Jobs can be
> encrypted client-side with age, snapshots restore (plaintext or encrypted), a
> captured system manifest can be replayed onto a fresh SD card, and there's a
> CLI, a Textual TUI, and a browser dashboard. The CLI uses the server when
> reachable and falls back to standalone. See [`docs/PLAN.md`](docs/PLAN.md) and
> [`deploy/`](deploy/) for deployment.

## Install (development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[server,dev]"
```

## Server mode (recommended)

On the server, start the daemon (it hosts the repo + job config):

```bash
pibackup serve                              # API + dashboard on http://127.0.0.1:8765
```

Open `http://server:8765/` for the dashboard: per-Pi jobs, last-run status, and
recent runs, auto-refreshing every 30s.

### Add a new Pi (enrollment)

On the server, mint a token; it prints a one-line bootstrap:

```bash
pibackup enroll kitchen-pi
#   pibackup connect http://server:8765 --name kitchen-pi --token <token>
```

Run that on the new Pi — it generates an SSH key, registers it with the server,
and writes `config.toml` pointed at the server and its repo. Then:

```bash
pibackup job create home -s /home/pi --retention 30
pibackup run               # rsync to the repo, report the run to the server
pibackup ps                # runs (from the server)
pibackup snapshot prune    # drop snapshots past retention
```

(Set `authorized_keys = "/home/backup/.ssh/authorized_keys"` in the server's
config to auto-authorise enrolled keys for rsync-over-SSH.)

## Standalone mode

With no `server_url` reachable, jobs come from `config.toml` and runs are
recorded in a local SQLite db — no server needed:

```toml
repo_target = "/mnt/usb/pibackup/repo"

[[job]]
name = "home"
sources = ["/home/pi"]
retention_days = 30
bwlimit_kbps = 0          # 0 = unlimited
```

## Encrypted backups

Encrypted jobs are streamed `tar | zstd | age` into one archive per snapshot, so
the server stores only opaque blobs. Needs the crypto extra:

```bash
pip install 'pibackup[crypto]'
pibackup key create               # generate an age key (auto-used as recipient)
pibackup key ls                   # list keys + public recipients
```

Mark a job `encrypted = true` (or `--encrypt` on `job create`). The recipient is
your only key by default, or set `recipient = "age1…"` in `config.toml`. Keep the
key safe — it's required to restore.

## Restore

```bash
pibackup snapshot ls                         # find the snapshot id
pibackup restore 12                          # → ./pibackup-restore-12/ (safe default)
pibackup restore 12 --target /               # restore to original paths (in place)
```

Plaintext snapshots restore via reverse rsync; encrypted ones are fetched and
decrypted with your local age key. Snapshots preserve absolute paths, so
`--target /` puts files back where they came from.

## System manifest & bare-metal restore

```bash
pibackup manifest -o manifest.json           # capture hostname, packages, services, /etc bits
                                             # (add it to a job's sources to back it up)
pibackup recover manifest.json               # generate restore-system.sh (hostname, apt, pip, services)
pibackup recover manifest.json --apply       # …or run it (as root, on a fresh Pi)
```

Bare-metal restore to a new SD card: install pibackup, `pibackup connect`,
`pibackup restore <id> --target /`, then `pibackup recover manifest.json --apply`.

## Terminal UI

```bash
pip install 'pibackup[tui]'
pibackup tui          # browse jobs/snapshots/runs; press r to run, g to refresh, q to quit
```

## CLI

`pibackup <resource> <verb>` with top-level shortcuts:

```bash
pibackup status            # config, repo target, server reachability
pibackup job ls            # list jobs
pibackup run [JOB]         # run a backup now (--dry-run to preview)
pibackup ps                # running / recent runs
pibackup logs RUN          # a run's message
pibackup snapshot ls       # list stored snapshots
pibackup restore ID        # restore a snapshot
pibackup tui               # terminal UI
pibackup serve             # run the server (API + dashboard)
```

Every `ls` supports `--format json` and `-q/--quiet`, like Docker.

## Deploy & scheduling

systemd unit templates and an installer live in [`deploy/`](deploy/). In short —
on the server run `pibackup serve` (or the `pibackup-server.service` unit); on
each Pi, the `pibackup enroll` one-liner installs pibackup, enrolls, and enables
a daily backup timer. Backups run under `nice`/`ionice` and honour each job's
`bwlimit_kbps`, so they stay out of the way of foreground work. See
[`deploy/README.md`](deploy/README.md).

The three interfaces — CLI, Textual `tui`, and the browser dashboard at the
server's `/` — cover terminal, TUI, and "windowed" use without a native GUI dep.

## Test

```bash
pip install -e ".[server,dev,crypto,tui]"
pytest
```
