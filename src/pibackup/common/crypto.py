"""Optional client-side encryption with age.

Phase 4: encrypt the ``tar | zstd`` stream to the server's age recipient before
upload so the server only ever stores opaque blobs. Key management lands here too.
"""

from __future__ import annotations

import shutil


def age_available() -> bool:
    """True if an ``age`` binary is on PATH."""
    return shutil.which("age") is not None


def encrypt_stream(*args, **kwargs):  # pragma: no cover - Phase 4
    raise NotImplementedError("age encryption lands in Phase 4.")


def decrypt_stream(*args, **kwargs):  # pragma: no cover - Phase 4
    raise NotImplementedError("age decryption lands in Phase 4.")
