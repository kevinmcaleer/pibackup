# pibackup

Self-contained backup system for Raspberry Pi. Pi clients push their files to a
central server that owns the repository, tracks job status and retention, and
serves a dashboard. CLI-first with a Docker-style command grammar.

> Status: **Phase 3 — web dashboard.** A REST API hosts job config and
> run/snapshot reporting; clients fetch jobs, rsync to the repo, and report
> results, with the server pruning expired snapshots and serving a status
> dashboard. The CLI uses the server when reachable and falls back to standalone
> (config.toml + local db). Encryption and restore arrive in later phases. See
> [`docs/PLAN.md`](docs/PLAN.md) for the full plan.

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

On each Pi, point `config.toml` at the server and its repo, then create jobs:

```toml
# ~/.config/pibackup/config.toml
repo_target = "pi@server:/srv/pibackup/repo"   # rsync destination
client_name = "kitchen-pi"                      # defaults to the hostname
server_url  = "http://server:8765"
```

```bash
pibackup job create home -s /home/pi --retention 30
pibackup run               # rsync to the repo, report the run to the server
pibackup ps                # runs (from the server)
pibackup snapshot prune    # drop snapshots past retention
```

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

## CLI

`pibackup <resource> <verb>` with top-level shortcuts:

```bash
pibackup status            # config, repo target, server reachability
pibackup job ls            # list jobs
pibackup run [JOB]         # run a backup now (--dry-run to preview)
pibackup ps                # running / recent runs
pibackup logs RUN          # a run's message
pibackup snapshot ls       # list stored snapshots
pibackup serve             # run the server (API + dashboard)
```

Every `ls` supports `--format json` and `-q/--quiet`, like Docker.

## Test

```bash
pytest
```
