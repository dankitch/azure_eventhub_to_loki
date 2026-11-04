import json

from logexport.config import Config, get_tag_config_from_mapping
from logexport.deserialize import create_labels_string, stream_from_event_body
from logexport.filter import Filter
from logexport.resource import Resource
from logexport.tags import (
    TagConfig,
    TagResolver,
    parent_resource_id,
    resource_group_id,
    sanitize_name,
)

SUB = "/subscriptions/0000-1111"
RG = f"{SUB}/resourceGroups/rg-app"
SQL = f"{RG}/providers/Microsoft.Sql/servers/sql-1"
DB = f"{SQL}/databases/db-1"
KV = f"{RG}/providers/Microsoft.KeyVault/vaults/kv-1"

GRAPH = {
    SQL.lower(): {"env": "prod", "Owner": "team-data"},
    KV.lower(): {"env": "dev", "cost-centre": "42"},
    RG.lower(): {"env": "rg-default", "department": "platform"},
}


class FakeToken:
    def get_token(self) -> str:
        return "token"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeGraph:
    """Mimics the Resource Graph endpoint by matching ids quoted in the query."""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.calls = 0

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        assert headers["Authorization"] == "Bearer token"
        if self.status_code != 200:
            return FakeResponse(self.status_code, {"error": "throttled"})
        query = json["query"]
        rows = [{"id": k, "tags": v} for k, v in GRAPH.items() if f"'{k}'" in query]
        return FakeResponse(200, {"data": rows})


def resolver(graph: FakeGraph, **kwargs) -> TagResolver:
    return TagResolver(TagConfig(enabled=True, **kwargs), FakeToken(), graph)  # type: ignore[arg-type]


def test_id_helpers():
    assert parent_resource_id(DB.upper()) == SQL.upper()
    assert parent_resource_id(SQL) is None
    assert resource_group_id(DB) == RG
    assert sanitize_name("cost-centre") == "cost_centre"
    assert sanitize_name("1st") == "_1st"


def test_tags_case_insensitive_and_cached():
    graph = FakeGraph()
    r = resolver(graph)
    r.prefetch([KV.upper(), SQL.upper()])
    assert graph.calls == 1
    assert r.tags_for(KV.upper()) == {"env": "dev", "cost-centre": "42"}
    assert r.tags_for(SQL) == {"env": "prod", "Owner": "team-data"}
    assert graph.calls == 1


def test_child_resource_inherits_parent_and_rg():
    graph = FakeGraph()
    assert resolver(graph).tags_for(DB.upper()) == {"env": "prod", "Owner": "team-data"}
    with_rg = resolver(graph, inherit_resource_group=True)
    assert with_rg.tags_for(DB) == {
        "env": "prod",  # resource/parent wins over resource group
        "Owner": "team-data",
        "department": "platform",
    }


def test_lookup_failure_does_not_raise_and_backs_off():
    graph = FakeGraph(status_code=429)
    r = resolver(graph)
    assert r.tags_for(KV) == {}
    assert r.tags_for(KV) == {}
    assert graph.calls == 1


def test_tag_config_from_env():
    cfg = get_tag_config_from_mapping(
        {
            "RESOURCE_TAGS": "true",
            "RESOURCE_TAG_LABELS": "env, Owner:team",
            "RESOURCE_TAG_METADATA": "owner,cost-centre",
            "RESOURCE_TAGS_INHERIT_RESOURCE_GROUP": "yes",
        }
    )
    assert cfg.enabled and cfg.inherit_resource_group
    assert cfg.label_tags == {"env": "env", "owner": "team"}
    tags = {"env": "prod", "Owner": "a", "cost-centre": "42", "other": "x"}
    assert cfg.labels_for(tags) == {"env": "prod", "team": "a"}
    assert cfg.metadata_for(tags) == {"tag_Owner": "a", "tag_cost_centre": "42"}
    assert not get_tag_config_from_mapping({}).enabled
    assert get_tag_config_from_mapping({}).metadata_tags is None


def test_labels_escape_and_skip_reserved():
    resource = Resource("id", "name", "rg", "cat", "typ")
    labels = create_labels_string(
        resource, None, {"cluster": "x"}, {"env": 'a"b', "job": "evil", "cluster": "y"}
    )
    assert labels == (
        '{job="integrations/azure-logexport",cluster="x",category="cat",'
        'resourceGroup="rg",resourceName="name",resourceType="typ",env="a\\"b"}'
    )


def test_stream_enrichment_end_to_end():
    r = resolver(FakeGraph(), label_tags={"env": "env"})
    body = json.dumps(
        {
            "records": [
                {
                    "resourceId": KV.upper(),
                    "category": "AuditEvent",
                    "time": "2024-06-05T10:47:31Z",
                },
                {
                    "resourceId": DB.upper(),
                    "category": "SQLSecurityAuditEvents",
                    "time": "2024-06-05T10:47:31Z",
                },
                {"category": "NoResource", "time": "2024-06-05T10:47:31Z"},
            ]
        }
    ).encode()
    streams = list(stream_from_event_body(body, Config(Filter(None), {}, r)))
    by_cat = {s.labels.split('category="')[1].split('"')[0]: s for s in streams}

    assert 'env="dev"' in by_cat["AuditEvent"].labels
    assert 'env="prod"' in by_cat["SQLSecurityAuditEvents"].labels
    assert "env=" not in by_cat["NoResource"].labels

    meta = {m.name: m.value for m in by_cat["AuditEvent"].entries[0].structuredMetadata}
    assert meta["tag_env"] == "dev"
    assert meta["tag_cost_centre"] == "42"
    # Original log line is untouched.
    assert "tags" not in json.loads(by_cat["AuditEvent"].entries[0].line)


def test_disabled_by_default_matches_previous_output():
    body = json.dumps({"resourceId": KV, "category": "AuditEvent"}).encode()
    (stream,) = stream_from_event_body(body, Config(Filter(None), {}))
    assert not any(
        m.name.startswith("tag_") for m in stream.entries[0].structuredMetadata
    )
