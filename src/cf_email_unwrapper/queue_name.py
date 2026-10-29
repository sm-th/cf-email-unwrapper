"""Queue name derivation shared with cf-email-gateway (see README, "Queue naming")."""

import hashlib
import re

_MAX_LEN = 63


def queue_name(address: str) -> str:
    a = address.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", a).strip("-")
    name = "mail-" + slug
    if len(name) > _MAX_LEN:
        digest = hashlib.sha256(a.encode("utf-8")).hexdigest()[:8]
        name = name[:54].rstrip("-") + "-" + digest
    return name
