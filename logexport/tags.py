"""Enrich log records with the Azure tags of the resource that emitted them.

Diagnostic and activity log events only carry a `resourceId`, never the resource's tags, so
tags are looked up through Azure Resource Graph and cached in memory. Lookups are batched per
event body and failures never block log delivery: records are shipped without tags instead.
"""

import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Mapping, Protocol

import requests

from logexport.azure_auth import ManagedIdentityCredential

RESOURCE_GRAPH_URL: Final[str] = (
    "https://management.azure.com/providers/Microsoft.ResourceGraph/resources"
    "?api-version=2022-10-01"
)
# Keep the KQL query comfortably below Resource Graph's query length limit.
MAX_IDS_PER_QUERY: Final[int] = 100
# How long to remember a failed lookup before retrying (avoids hammering a throttled API).
FAILURE_BACKOFF_SECONDS: Final[int] = 60

_PROVIDER_RE: Final = re.compile(
    r"^(/subscriptions/[^/]+/resourcegroups/[^/]+/providers/[^/]+/[^/]+/[^/]+)",
    re.IGNORECASE,
)
_RG_RE: Final = re.compile(
    r"^(/subscriptions/[^/]+/resourcegroups/[^/]+)", re.IGNORECASE
)
_INVALID_NAME_CHARS: Final = re.compile(r"[^a-zA-Z0-9_]")


class TokenProvider(Protocol):
    def get_token(self) -> str: ...


def sanitize_name(name: str) -> str:
    """Converts a tag key into a valid Loki label / structured metadata name."""
    sanitized = _INVALID_NAME_CHARS.sub("_", name)
    if not sanitized or sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized


def parent_resource_id(resource_id: str) -> str | None:
    """Returns the top-level resource for a child resource id, e.g. the SQL server of a
    database. Returns None if the id is already top-level or not a resource id."""
    m = _PROVIDER_RE.match(resource_id)
    if m and m.group(1).lower() != resource_id.lower():
        return m.group(1)
    return None


def resource_group_id(resource_id: str) -> str | None:
    m = _RG_RE.match(resource_id)
    return m.group(1) if m else None


@dataclass
class TagConfig:
    """Controls which tags are attached and how.

    label_tags maps tag key (lower case) -> stream label name. Keep this list short: every
    distinct value creates a new Loki stream.
    metadata_tags is the set of tag keys (lower case) added as structured metadata, or None
    for all tags.
    """

    enabled: bool = False
    label_tags: dict[str, str] = field(default_factory=dict)
    metadata_tags: set[str] | None = None
    metadata_prefix: str = "tag_"
    inherit_resource_group: bool = False
    cache_ttl_seconds: int = 3600

    def labels_for(self, tags: Mapping[str, str]) -> dict[str, str]:
        out = {}
        for key, value in tags.items():
            label = self.label_tags.get(key.lower())
            if label is not None:
                out[label] = value
        return out

    def metadata_for(self, tags: Mapping[str, str]) -> dict[str, str]:
        out = {}
        for key, value in tags.items():
            if self.metadata_tags is None or key.lower() in self.metadata_tags:
                out[self.metadata_prefix + sanitize_name(key)] = value
        return out


class TagResolver:
    def __init__(
        self,
        config: TagConfig,
        credential: TokenProvider | None = None,
        session: requests.Session | None = None,
        clock=time.monotonic,
    ):
        self.config = config
        self.credential = credential or ManagedIdentityCredential()
        self.session = session or requests.Session()
        self.clock = clock
        # lower-cased id -> (tags or None if not found, expiry)
        self._cache: dict[str, tuple[dict[str, str] | None, float]] = {}
        self._lock = threading.Lock()

    def _ids_needed(self, resource_id: str) -> list[str]:
        ids = [resource_id]
        parent = parent_resource_id(resource_id)
        if parent:
            ids.append(parent)
        if self.config.inherit_resource_group:
            rg = resource_group_id(resource_id)
            if rg:
                ids.append(rg)
        return ids

    def _cached(self, key: str) -> tuple[bool, dict[str, str] | None]:
        hit = self._cache.get(key)
        if hit is None or hit[1] < self.clock():
            return False, None
        return True, hit[0]

    def prefetch(self, resource_ids: Iterable[str]) -> None:
        """Looks up all uncached resource ids (and their parents / resource groups) in as few
        Resource Graph queries as possible."""
        wanted: dict[str, str] = {}
        with self._lock:
            for rid in resource_ids:
                for i in self._ids_needed(rid):
                    key = i.lower()
                    if key not in wanted and not self._cached(key)[0]:
                        wanted[key] = i
        if not wanted:
            return

        keys = list(wanted)
        for start in range(0, len(keys), MAX_IDS_PER_QUERY):
            batch = keys[start : start + MAX_IDS_PER_QUERY]
            try:
                found = self._query(batch)
                expiry = self.clock() + self.config.cache_ttl_seconds
            except Exception:
                logging.warning(
                    "failed to look up Azure resource tags; shipping logs without tags",
                    exc_info=True,
                )
                found = {}
                expiry = self.clock() + FAILURE_BACKOFF_SECONDS
            with self._lock:
                for key in batch:
                    self._cache[key] = (found.get(key), expiry)

    def tags_for(self, resource_id: str) -> dict[str, str]:
        """Returns merged tags for a resource. On key conflicts the resource itself wins over
        its parent resource (e.g. SQL server for a database), which wins over the resource
        group (only when inherit_resource_group is set). Call prefetch first; misses are
        fetched here."""
        ids = self._ids_needed(resource_id)
        with self._lock:
            missing = [i for i in ids if not self._cached(i.lower())[0]]
        if missing:
            self.prefetch([resource_id])

        merged: dict[str, str] = {}
        with self._lock:
            own = self._cached(ids[0].lower())[1]
            parent = parent_resource_id(resource_id)
            rg = resource_group_id(resource_id)
            parent_tags = self._cached(parent.lower())[1] if parent else None
            rg_tags = (
                self._cached(rg.lower())[1]
                if rg and self.config.inherit_resource_group
                else None
            )

        for source in (rg_tags, parent_tags, own):
            if source:
                merged.update(source)
        return merged

    def _query(self, keys: list[str]) -> dict[str, dict[str, str]]:
        id_list = ",".join("'" + k.replace("'", "") + "'" for k in keys)
        query = (
            f"resources | where id in~ ({id_list}) | project id = tolower(id), tags"
            f" | union (resourcecontainers"
            f" | where type =~ 'microsoft.resources/subscriptions/resourcegroups'"
            f" and id in~ ({id_list}) | project id = tolower(id), tags)"
        )
        body: dict = {
            "query": query,
            "options": {"resultFormat": "objectArray", "$top": 1000},
        }
        result: dict[str, dict[str, str]] = {}
        while True:
            res = self.session.post(
                RESOURCE_GRAPH_URL,
                json=body,
                headers={"Authorization": f"Bearer {self.credential.get_token()}"},
                timeout=15,
            )
            if res.status_code != 200:
                raise RuntimeError(
                    f"Resource Graph query failed: {res.status_code} {res.text[:500]}"
                )
            payload = res.json()
            for row in payload.get("data", []):
                tags = row.get("tags") or {}
                result[row["id"]] = {str(k): str(v) for k, v in tags.items()}
            skip_token = payload.get("$skipToken")
            if not skip_token:
                return result
            body["options"]["$skipToken"] = skip_token
