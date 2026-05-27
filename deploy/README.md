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
  | sudo sh -s -- --server http://server:8765 --name kitchen-pi --token <token> --timer
```

That installs pibackup for root, enrolls (generating + registering an SSH key),
and enables the daily backup timer as a **system** service. No SSH setup is
needed on the client: backups use the enrolled key automatically and trust the
server's host key on first contact, so there's nothing to add to `~/.ssh/config`.

Run client commands with `sudo` (e.g. `sudo pibackup run`, `sudo pibackup
status`) so they use root's config and can read everything.

## Scheduling

Backups run on a **system** systemd timer (`pibackup-backup.timer`, daily with a
randomised delay, as root). The service runs `pibackup run` under `Nice=19` /
`IOSchedulingClass=idle`, and the client also wraps rsync in `nice`/`ionice`, so
backups stay out of the way of foreground work. Tune per-job throughput with
`bwlimit_kbps`.

## Manual timer install

```bash
sudo cp deploy/pibackup-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now pibackup-backup.timer
sudo systemctl list-timers pibackup-backup.timer
```
