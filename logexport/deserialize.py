import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from io import IOBase
from typing import Final, Iterator, Tuple

from logexport._version import __version__
from logexport.config import Config
from logexport.push import push_pb2
from logexport.resource import Resource, resource_from_id_and_category

VERSION_LABEL_KEY: Final[str] = "__grafana_azure_logexport_version__"
RESERVED_LABELS: Final[frozenset[str]] = frozenset(
    {"job", "category", "resourceGroup", "resourceName", "resourceType", "type"}
)


def entry_from_event_record(
    load: dict, current_ts_nanos: int
) -> Tuple[Resource, str | None, push_pb2.EntryAdapter]:
    """Returns the category, resource group, and type of the event and the entry."""
    entry = push_pb2.EntryAdapter()

    ts = get_timestamp(load)
    if ts is not None:
        entry.timestamp.FromJsonString(ts)
    else:
        entry.timestamp.FromNanoseconds(current_ts_nanos)

    entry.structuredMetadata.add(name=VERSION_LABEL_KEY, value=__version__)

    resource = Resource("unknown", None, None, None, None)
    if "resourceId" in load:
        entry.structuredMetadata.add(name="resourceId", value=load["resourceId"])
        resource = resource_from_id_and_category(
            load["resourceId"], load.get("category")
        )

    if "correlationId" in load:
        entry.structuredMetadata.add(name="correlationId", value=load["correlationId"])

    entry.line = json.dumps(load)

    # Override category for backwards compatibility.
    cat = load.get("category")
    if cat is not None:
        resource.category = cat

    record_type = load.get("type")
    if (
        record_type is None
        and load.get("ProductName") == "Microsoft Defender for Cloud"
    ):
        record_type = "Alert/" + (load.get("AlertType") or "Unknown")

    return resource, record_type, entry


def stream_from_event_body(
    f: IOBase | bytes, config: Config
) -> Iterable[push_pb2.StreamAdapter]:
    """Deserializes a single event body into a list of streams.
    Each stream has the job label and category and type labels if present.
    """

    stream_index: dict[str, push_pb2.StreamAdapter] = {}
    current_ts = time.time_ns()

    if isinstance(f, bytes):
        data = json.loads(f)
    else:
        data = json.load(f)

    records = list(get_records(data))
    tags = config.tags
    if tags is not None:
        # One batched lookup for every resource in this event body.
        tags.prefetch(r["resourceId"] for r in records if "resourceId" in r)

    for record in records:
        # Each record should receive it's own unique timestamp.
        current_ts += 1
        resource, record_type, entry = entry_from_event_record(record, current_ts)

        tag_labels: dict[str, str] = {}
        if tags is not None and "resourceId" in record:
            resource_tags = tags.tags_for(record["resourceId"])
            for name, value in tags.config.metadata_for(resource_tags).items():
                entry.structuredMetadata.add(name=name, value=value)
            tag_labels = tags.config.labels_for(resource_tags)

        for i in config.filter.apply(record):
            updated = push_pb2.EntryAdapter()
            updated.CopyFrom(entry)
            updated.line = json.dumps(i)

            labels = create_labels_string(
                resource, record_type, config.additional_labels, tag_labels
            )
            stream = stream_index.setdefault(
                labels, push_pb2.StreamAdapter(labels=labels)
            )
            stream.entries.append(updated)

    return stream_index.values()


def get_records(data: dict) -> Iterator[dict]:
    for i in data.get("records", []):
        yield i
    if "records" not in data:
        yield data


def streams_from_events(
    events: Iterable[bytes], config: Config
) -> Iterable[push_pb2.StreamAdapter]:
    for event in events:
        for stream in stream_from_event_body(event, config):
            yield stream


def get_timestamp(load: dict) -> str | None:
    return (
        load.get("timeStamp")
        or load.get("timestamp")
        or load.get("time")
        or load.get("created")
    )


def create_labels_string(
    resource: Resource,
    record_type: str | None,
    addional_labels: dict[str, str],
    tag_labels: dict[str, str] | None = None,
) -> str:
    labels = 'job="integrations/azure-logexport"'

    for key, value in addional_labels.items():
        labels += f',{key}="{value}"'

    if resource.category is not None:
        labels += f',category="{resource.category}"'

    if resource.group is not None:
        labels += f',resourceGroup="{resource.group}"'

    if resource.name is not None:
        labels += f',resourceName="{resource.name}"'

    if resource.typ is not None:
        labels += f',resourceType="{resource.typ}"'

    if record_type is not None:
        labels += f',type="{record_type}"'

    if tag_labels:
        reserved = RESERVED_LABELS | set(addional_labels)
        for key, value in tag_labels.items():
            # Never emit a duplicate label name; Loki rejects the whole push.
            if key not in reserved:
                labels += f',{key}="{escape_label_value(value)}"'

    return "{" + labels + "}"


def escape_label_value(value: str) -> str:
    """Escapes a value for use inside a quoted LogQL label value."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
