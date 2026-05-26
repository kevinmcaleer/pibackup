"""Server-side data access.

The implementation is shared with the client; see :mod:`pibackup.common.store`.
Re-exported here so server code can keep importing ``server.store``.
"""

from pibackup.common.store import Store

__all__ = ["Store"]
