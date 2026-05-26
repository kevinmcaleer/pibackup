"""Client-side encryption with age (via the ``pyrage`` library).

Encrypted jobs are streamed ``tar -> zstd -> age`` into a single archive per
snapshot, so the server only ever stores an opaque blob. Decryption (restore)
reverses the pipeline. Uses streaming I/O to keep memory low on a Pi.

The pyrage + zstandard libraries are an optional extra; import them lazily so a
plaintext-only client needs neither.
"""

from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

ARCHIVE_SUFFIX = ".tar.zst.age"


def crypto_available() -> bool:
    try:
        import pyrage  # noqa: F401
        import zstandard  # noqa: F401

        return True
    except ImportError:
        return False


def _require() -> None:
    if not crypto_available():
        raise RuntimeError(
            "Encryption needs extra libraries: pip install 'pibackup[crypto]'"
        )


def generate_keypair() -> tuple[str, str]:
    """Return ``(secret, recipient)`` for a fresh age X25519 identity."""
    _require()
    from pyrage import x25519

    ident = x25519.Identity.generate()
    return str(ident), str(ident.to_public())


def recipient_from_secret(secret: str) -> str:
    _require()
    from pyrage import x25519

    return str(x25519.Identity.from_str(secret).to_public())


def _arcname(path: Path) -> str:
    # Mirror rsync -R: preserve the absolute path, minus the leading slash.
    return str(path).lstrip("/")


def encrypt_archive(
    sources: Sequence[str | Path], out_path: str | Path, recipient: str, *, level: int = 10
) -> int:
    """Stream ``tar | zstd | age`` of ``sources`` into ``out_path``.

    Returns the size of the written archive in bytes.
    """
    _require()
    import pyrage
    import zstandard
    from pyrage import x25519

    rcpt = x25519.Recipient.from_str(recipient)
    out_path = Path(out_path)
    tmp = Path(tempfile.mkstemp(suffix=".tar.zst")[1])
    try:
        cctx = zstandard.ZstdCompressor(level=level)
        with open(tmp, "wb") as raw, cctx.stream_writer(raw) as zw:
            with tarfile.open(fileobj=zw, mode="w|") as tar:
                for source in sources:
                    sp = Path(source)
                    tar.add(sp, arcname=_arcname(sp), recursive=True)
        with open(tmp, "rb") as reader, open(out_path, "wb") as writer:
            pyrage.encrypt_io(reader, writer, [rcpt])
        return out_path.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)


def decrypt_archive(
    archive_path: str | Path, dest_dir: str | Path, identities: Iterable[str]
) -> None:
    """Reverse :func:`encrypt_archive`, extracting into ``dest_dir``."""
    _require()
    import pyrage
    import zstandard
    from pyrage import x25519

    idents = [x25519.Identity.from_str(s) for s in identities]
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkstemp(suffix=".tar.zst")[1])
    try:
        with open(archive_path, "rb") as reader, open(tmp, "wb") as writer:
            pyrage.decrypt_io(reader, writer, idents)
        dctx = zstandard.ZstdDecompressor()
        with open(tmp, "rb") as raw, dctx.stream_reader(raw) as zr:
            with tarfile.open(fileobj=zr, mode="r|") as tar:
                tar.extractall(dest, filter="data")  # filter guards path traversal
    finally:
        tmp.unlink(missing_ok=True)
