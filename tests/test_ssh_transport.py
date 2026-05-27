"""Enrolled-key SSH transport.

Backups push over SSH using the key generated at enrollment, auto-trust the
server's host key on first contact, and never block on a prompt — so a client
needs no hand-written ``~/.ssh/config`` and unattended timer runs don't hang.
"""

from pibackup.common.transfer import (
    Destination,
    build_rsync_command,
    ssh_options,
    ssh_rsh,
)


def test_ssh_rsh_is_none_without_a_key():
    # Unenrolled / local: leave rsync's default SSH untouched.
    assert ssh_rsh(None) is None


def test_ssh_options_are_unattended_and_safe():
    opts = " ".join(ssh_options("/home/pi/.config/pibackup/ssh/id_ed25519"))
    assert "-i /home/pi/.config/pibackup/ssh/id_ed25519" in opts
    assert "IdentitiesOnly=yes" in opts                 # use only this key
    assert "StrictHostKeyChecking=accept-new" in opts   # trust host once, no prompt
    assert "BatchMode=yes" in opts                       # fail fast, never hang


def test_build_rsync_command_injects_the_transport():
    cmd = build_rsync_command("/data", "pi@server:/repo/", rsh=ssh_rsh("/k"))
    assert "-e" in cmd
    assert cmd[cmd.index("-e") + 1].startswith("ssh -i /k")


def test_destination_uses_key_for_remote_only():
    remote = Destination("pi@server:/srv/repo", ssh_key="/k")
    assert remote.rsh and "-i /k" in remote.rsh
    argv = remote._ssh_argv()
    assert "-i" in argv and "/k" in argv

    # A local repo never shells out to ssh, so no transport is injected into
    # the rsync command regardless of any key.
    local = Destination("/srv/repo", ssh_key="/k")
    assert local.rsh is None
