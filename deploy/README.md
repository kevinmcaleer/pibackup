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

On the server: `pibackup enroll kitchen-pi` prints a bootstrap. On the new Pi:

```bash
curl -fsSL https://raw.githubusercontent.com/kevinmcaleer/pibackup/main/deploy/install.sh \
  | sh -s -- --server http://server:8765 --name kitchen-pi --token <token> --timer
```

That installs pibackup, enrolls (generating + registering an SSH key), and
enables the daily backup timer.

## Scheduling

Backups run on a client-side systemd timer (`pibackup-backup.timer`, daily with a
randomised delay). The service runs `pibackup run` under `Nice=19` /
`IOSchedulingClass=idle`, and the client also wraps rsync in `nice`/`ionice`, so
backups stay out of the way of foreground work. Tune per-job throughput with
`bwlimit_kbps`.

## Manual timer install

```bash
cp deploy/pibackup-backup.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now pibackup-backup.timer
systemctl --user list-timers pibackup-backup.timer
```
