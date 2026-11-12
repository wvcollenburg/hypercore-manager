"""
forwarding.py — decisions the dashboard makes before it forwards a request to the backup service.

Kept free of Flask so the rules can be tested on their own.
"""
from __future__ import annotations


def body_cluster(payload: dict, selected: str, visible: set[str], fill: bool) -> tuple[str | None, str | None]:
    """The cluster a forwarded request body may act on. Returns (cluster_id, error).

    A body that names a cluster keeps it when the user may see that cluster: forms such as proxy
    deployment and proxy registration have their own cluster choice, which used to be overwritten
    by the cluster selected in the sidebar (a proxy picked for cluster 2 was deployed on cluster 1).
    A cluster the user may not see is refused, never silently swapped. A body without one gets the
    selected cluster when the endpoint needs it (`fill`), as before.
    """
    named = payload.get('cluster_id')
    if named:
        named = str(named).strip()
        if named not in visible:
            return None, 'unknown cluster, or not one you may manage'
        return named, None
    if fill:
        return selected, None
    return None, None
