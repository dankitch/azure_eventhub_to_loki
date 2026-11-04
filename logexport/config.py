import os
from dataclasses import dataclass
from typing import Final, Mapping

import jq  # type: ignore

from logexport.filter import Filter
from logexport.tags import TagConfig, TagResolver, sanitize_name

ADDITIONAL_LABEL_PREFIX: Final[str] = "ADDITIONAL_LABEL_"
FILTER: Final[str] = "JQ_PIPELINE"

RESOURCE_TAGS: Final[str] = "RESOURCE_TAGS"
RESOURCE_TAG_LABELS: Final[str] = "RESOURCE_TAG_LABELS"
RESOURCE_TAG_METADATA: Final[str] = "RESOURCE_TAG_METADATA"
RESOURCE_TAG_METADATA_PREFIX: Final[str] = "RESOURCE_TAG_METADATA_PREFIX"
RESOURCE_TAGS_INHERIT_RG: Final[str] = "RESOURCE_TAGS_INHERIT_RESOURCE_GROUP"
RESOURCE_TAGS_CACHE_TTL: Final[str] = "RESOURCE_TAGS_CACHE_TTL_SECONDS"

_TRUE: Final = {"1", "true", "yes", "on"}


@dataclass
class Config:
    filter: Filter
    additional_labels: dict[str, str]
    tags: TagResolver | None = None


def get_additional_labels() -> dict[str, str]:
    """Returns a dictionary of additional labels to add to the exported streams.
    The labels are configured through environment variables."""
    return get_additional_labels_from_mapping(os.environ)


def get_additional_labels_from_mapping(env: Mapping[str, str]) -> dict[str, str]:
    labels = {}
    for key, value in env.items():
        if key.startswith(ADDITIONAL_LABEL_PREFIX):
            label_name = key[len(ADDITIONAL_LABEL_PREFIX) :]
            if label_name:
                label_name = label_name.replace(",", "_")
                labels[label_name] = value.replace(",", "_")

    return labels


def get_filter() -> Filter:
    """Returns a jq filter to apply to the exported streams.
    The filter is configured through an environment variable."""
    return get_filter_from_mapping(os.environ.get(FILTER))


def get_filter_from_mapping(filter: str | None) -> Filter:
    if filter is not None:
        return Filter(filter)

    return Filter(None)


def get_tag_config() -> TagConfig:
    return get_tag_config_from_mapping(os.environ)


def get_tag_config_from_mapping(env: Mapping[str, str]) -> TagConfig:
    """Builds the resource tag configuration.

    RESOURCE_TAGS=true                    enable tag enrichment
    RESOURCE_TAG_LABELS=env,Owner:team    tags promoted to stream labels; `tag:label` renames
    RESOURCE_TAG_METADATA=*               tags added as structured metadata (* = all,
                                          comma list, or `none`); default *
    RESOURCE_TAG_METADATA_PREFIX=tag_     prefix for structured metadata names
    RESOURCE_TAGS_INHERIT_RESOURCE_GROUP  also merge in the resource group's tags
    RESOURCE_TAGS_CACHE_TTL_SECONDS=3600  how long looked-up tags are cached
    """
    enabled = env.get(RESOURCE_TAGS, "").strip().lower() in _TRUE

    label_tags: dict[str, str] = {}
    for item in env.get(RESOURCE_TAG_LABELS, "").split(","):
        item = item.strip()
        if not item:
            continue
        tag, _, label = item.partition(":")
        label_tags[tag.strip().lower()] = sanitize_name(label.strip() or tag.strip())

    raw_metadata = env.get(RESOURCE_TAG_METADATA, "*").strip()
    metadata_tags: set[str] | None
    if raw_metadata == "*":
        metadata_tags = None
    elif raw_metadata.lower() in ("", "none"):
        metadata_tags = set()
    else:
        metadata_tags = {
            t.strip().lower() for t in raw_metadata.split(",") if t.strip()
        }

    return TagConfig(
        enabled=enabled,
        label_tags=label_tags,
        metadata_tags=metadata_tags,
        metadata_prefix=env.get(RESOURCE_TAG_METADATA_PREFIX, "tag_"),
        inherit_resource_group=env.get(RESOURCE_TAGS_INHERIT_RG, "").strip().lower()
        in _TRUE,
        cache_ttl_seconds=int(env.get(RESOURCE_TAGS_CACHE_TTL, "3600")),
    )
