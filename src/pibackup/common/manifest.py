"""System manifest capture for bare-metal restore.

Phase 6: snapshot the state needed to rebuild a Pi onto a fresh SD card --
hostname, manually-installed apt packages, ``pip freeze``, enabled systemd
services, key ``/etc`` files, ``/boot/firmware/config.txt``, crontabs, fstab.
"""

from __future__ import annotations


def capture() -> dict:  # pragma: no cover - Phase 6
    raise NotImplementedError("System manifest capture lands in Phase 6.")
