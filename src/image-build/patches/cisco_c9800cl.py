"""C9800-CL V2 adapter.

The launcher transformations live with the Cat9k family, but C9800 has its
own warm-port profile, OCI patch kind and validation lifecycle.
"""

from __future__ import annotations

from .cisco_cat9kv import FILES, apply


KIND = "cisco_c9800cl"
