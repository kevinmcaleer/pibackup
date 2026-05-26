# pibackup

Self-contained backup system for Raspberry Pi. Pi clients push their files to a
central server that owns the repository, tracks job status and retention, and
serves a dashboard. CLI-first with a Docker-style command grammar.

> Status: **Phase 6 — manifest + restore.** A REST API hosts job config and
> run/snapshot reporting; clients fetch jobs, rsync to the repo, and report
> results, with the server pruning expired snapshots and serving a status
> dashboard. Jobs can be encrypted client-side with age (the server stores only
> opaque blobs), snapshots can be restored (plaintext via reverse rsync,
> encrypted via decrypt + extract), and a system manifest can be captured. The
> CLI uses the server when reachable and falls back to standalone. Bare-metal
> restore + easy onboarding come next. See [`docs/PLAN.md`](docs/PLAN.md).

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

## System manifest

```bash
pibackup manifest                            # print hostname, packages, services, /etc bits
pibackup manifest -o manifest.json           # …or save it (add to a job's sources to back it up)
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
