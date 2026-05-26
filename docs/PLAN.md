# Raspberry Pi Backup — Build Plan

A self-contained backup system for Raspberry Pi: clients push their files to a
central server that owns the repository, tracks job status/retention, and serves
a dashboard. CLI-first, with a TUI and (later) a desktop GUI over the same engine.

## Decisions

| Area        | Choice                | Why |
|-------------|-----------------------|-----|
| Language    | **Python**            | Best fit for the Pi ecosystem; Typer (CLI), Textual (TUI), FastAPI (server). Runtime ships on Pi OS. |
| Transfer    | **rsync over SSH**    | Pre-installed on Pi OS. Free delta transfer, `-z` compression, `--bwlimit` throttling, `--partial` resume. |
| Encryption  | **age**               | Modern, tiny keys, simple management. Client-side encrypt — server never sees plaintext. |
| Compression | **zstd**              | On ARM beats gzip on ratio *and* speed; far cheaper CPU than xz. |
| Scheduling  | **Client-push**, server owns config + retention | Follows "clients connect and upload" + "server shows retention". A sleeping Pi shows as *overdue*, never blocks. |

## Architecture

```
┌─────────────────┐         rsync over SSH (-z, --bwlimit)        ┌──────────────────────┐
│  Pi client(s)   │  ───────────────────────────────────────────▶ │   pibackup serve     │
│  pibackup CLI   │                                                │  FastAPI + SQLite    │
│  + TUI          │  ◀───── job config / report run results ─────▶ │  backup repo on disk │
│  + age encrypt  │                REST API (HTTPS)                │  web dashboard       │
└─────────────────┘                                                └──────────────────────┘
```

- **Server** is the source of truth: job definitions, retention policy, run
  history, on-disk repository. Exposes REST API + web dashboard + an SSH endpoint
  that rsync targets. Started with `pibackup serve` (the "daemon", cf. `dockerd`).
- **Client** pulls its job config from the server, runs the backup (rsync push),
  optionally encrypts first, then reports the result back over the API.

### Two backup modes

| Mode | Mechanism | Trade-off |
|------|-----------|-----------|
| Plaintext (default) | rsync `--link-dest` hardlink snapshots | Cheap dedup, instant browsable restores; server sees files |
| Encrypted | client streams `tar │ zstd │ age` → rsync the blob | Server never sees plaintext; no cross-snapshot dedup |

### On background transfer / BITS

BITS is Windows-only — nothing to adopt on Pi. The "don't saturate the link"
behavior comes from rsync `--bwlimit`, plus `ionice -c3` / `nice` and off-peak
scheduling. A configurable bandwidth cap is part of every transfer.

## Repo layout

```
pibackup/
├── pyproject.toml
├── src/pibackup/
│   ├── common/   # config, models, crypto (age), manifest, transfer wrapper
│   ├── client/   # Typer CLI, Textual TUI, backup engine, restore
│   └── server/   # FastAPI app, SQLite store, retention/prune, dashboard
├── deploy/       # systemd units + install/bootstrap scripts
├── docs/
└── tests/
```

## CLI design (Docker-style)

Grammar: **`pibackup <resource> <verb> [args] [flags]`**, with top-level shortcuts
for the everyday actions. The point is discoverability — a small set of resource
nouns and one consistent verb set, exactly like Docker.

**Consistent verbs** across every resource: `ls`, `inspect`, `create`, `rm`,
`prune`, `logs`.

**Resources**
- `job` — backup job definitions
- `snapshot` — stored point-in-time backups
- `client` — registered Raspberry Pis (server-side view)
- `key` — age encryption keys

**Lifecycle / enrollment**
- `pibackup serve` — run the server + dashboard (the daemon, cf. `dockerd`)
- `pibackup connect <url>` — enroll *this* Pi against a server (uses a token)
- `pibackup enroll <name>` — (on server) mint a one-line bootstrap + token for a new Pi

**Top-level shortcuts** (cf. `docker run`, `docker ps`, `docker logs`)
- `pibackup run [JOB]` — run a backup now
- `pibackup ps` — running / recent runs
- `pibackup restore <SNAPSHOT> [PATH]` — restore files
- `pibackup status` — dashboard summary in the terminal
- `pibackup logs <RUN>` — run logs

**Full nested forms**
- `pibackup job ls | create | inspect <JOB> | rm <JOB>`
- `pibackup snapshot ls | inspect <ID> | rm <ID> | prune`
- `pibackup client ls | inspect <PI> | rm <PI>`
- `pibackup key ls | create | export | rm <KEY>`

**Familiar parallels**

| Docker | pibackup |
|--------|----------|
| `dockerd` | `pibackup serve` |
| `docker ps` | `pibackup ps` |
| `docker run …` | `pibackup run home-backup` |
| `docker logs <id>` | `pibackup logs <run>` |
| `docker image ls` | `pibackup snapshot ls` |
| `docker container rm <id>` | `pibackup snapshot rm <id>` |
| `docker system prune` | `pibackup snapshot prune` |

**Implementation note:** Typer maps onto this cleanly — each resource is a sub-app
(`app.add_typer(...)`), verbs are commands within it, and the top-level shortcuts
are commands on the root app. Mirror Docker's scripting affordances too:
`--format json` and `-q/--quiet` on every `ls`.

## Build phases

**Phase 0 — Scaffolding**
pyproject + packaging, shared config model, SQLite schema (clients, jobs, runs,
snapshots), logging. `pibackup` and `pibackup serve` entry points wired up with the
Docker-style command tree (stubbed verbs).

**Phase 1 — Core client backup (plaintext)**
rsync wrapper with `--link-dest` incremental snapshots, `-z`, `--partial`,
`--bwlimit`, exit-code handling. `pibackup run` end-to-end against a manually
configured server.

**Phase 2 — Server: jobs, status & retention**
REST API for job config + run reporting; retention engine that prunes old
snapshots server-side (it owns storage). Client fetches config, reports
success/failure, bytes, duration. `pibackup job`, `pibackup snapshot`, `pibackup ps`.

**Phase 3 — Web dashboard**
FastAPI views: per-client jobs, last run, status (✓/✗), next-due, retention.
Satisfies "server shows the backup jobs".

**Phase 4 — Encryption (age)**
Client-side `tar │ zstd │ age` encrypted mode, key generation/management
(`pibackup key`), recipient = server-stored public key, restore-side decryption.

**Phase 5 — TUI**
Textual interface over the same client engine: trigger runs, watch progress,
browse snapshots.

**Phase 6 — System manifest + restore**
Capture hostname, apt packages (`apt-mark showmanual`), `pip freeze`, enabled
systemd services, `/etc` + `/boot/firmware/config.txt`, crontabs, fstab →
`manifest.json`. File-level restore via reverse rsync.

**Phase 7 — Easy onboarding + bare-metal restore**
`pibackup enroll <name>` emits a one-line bootstrap (install script + token); the
client auto-generates an SSH keypair, registers, pulls default jobs. Restore-to-new-SD
bootstrap replays the manifest (hostname, packages, `/etc`, home dirs).

**Phase 8 — Polish & ship**
Bandwidth/background tuning, systemd timer templates, desktop GUI (thin front-end
over the client engine — deferred since CLI/TUI cover the need first), docs,
install one-liner.

## Open assumptions (reversible)

- Client-push model with server-owned config/retention.
- Desktop/X GUI deferred to Phase 8; CLI + TUI deliver full functionality first.
- Single `pibackup` binary for both client ops and management API calls; `pibackup
  serve` runs the server daemon.
