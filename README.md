An Azure function that consume eventhub messages and ships them to Grafana Loki / Grafana Cloud. 

The function consumes resource and activity log messages from an Azure EventHub and ships them to a Grafana Loki instance.

It supports Azure Diagnostics
[resource](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/resource-logs-schema) and
[activity](https://learn.microsoft.com/en-us/azure/azure-monitor/essentials/activity-log-schema) log events as well as
Defender for Clouds [continuous export](https://learn.microsoft.com/en-us/azure/defender-for-cloud/continuous-export)
messages.

## Installation

The repository comes with an [Azure ARM template](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/overview)
that can be used to deploy the function to an existing Azure resource group:

```bash
az group deployment create -g <resource-group> -n <deployment-name> --template-file azdeploy.json \
  --parameters packageUri=https://github.com/grafana/azure_eventhub_to_loki/releases/download/<version>/logexport.<version>.zip
```

This command will ask for the Loki endpoint and username/password.

The template can be consumed from Terraform:

```terraform
resource "azurerm_resource_group" "logexport" {
  name     = "logexport-group"
  location = var.location
}

resource "azurerm_resource_group_template_deployment" "logexport" {
  name                = "${azurerm_resource_group.logexport.name}-deploy"
  resource_group_name = azurerm_resource_group.logexport.name
  deployment_mode     = "Incremental"
  template_content    = file("azdeploy.json")

  parameters_content = jsonencode({
    "lokiEndpoint" = {
      value = var.loki_endpoint
    }
    "lokiUsername" = {
      value = var.loki_username
    }
    "lokiPassword" = {
      value = var.loki_password
    }
    "packageUri" = {
      value = var.package_uri
    }
  })
}
```

## Configuration

- `EVENTHUB_NAME`: The name of the EventHub to consume from.
- `EVENTHUB_CONNECTION`: The connection string for the EventHub.
- `FUNCTION_NAME`: The name of the Azure Function; defaults to `logexport`.
- `LOKI_ENDPOINT`: The root URL of the Loki instance to send logs to.
- `LOKI_USERNAME`: The optional username to use for authentication with Loki.
- `LOKI_PASSWORD`: The optional password to use for authentication with Loki.

### Additional Labels

Additional stream labels can be configured by setting environment variables with the prefix `ADDITIONAL_LABEL_`. For example,
setting `ADDITIONAL_LABEL_cluster=dev` will add a label `cluster=dev` to all streams.

### Azure Resource Tags

Azure diagnostic and activity logs only contain the `resourceId` of the resource that emitted them, never its tags. With
`RESOURCE_TAGS=true` the function looks up each resource's tags through
[Azure Resource Graph](https://learn.microsoft.com/en-us/azure/governance/resource-graph/overview), caches them in
memory, and attaches them to every entry. The log line itself is not modified.

| Variable | Default | Description |
| --- | --- | --- |
| `RESOURCE_TAGS` | `false` | Enables tag enrichment. |
| `RESOURCE_TAG_METADATA` | `*` | Tags added as [structured metadata](https://grafana.com/docs/loki/latest/get-started/labels/structured-metadata/) named `tag_<key>`: `*` for all, a comma-separated list of tag keys, or `none`. |
| `RESOURCE_TAG_METADATA_PREFIX` | `tag_` | Prefix for the structured metadata names. |
| `RESOURCE_TAG_LABELS` | _(empty)_ | Comma-separated tag keys promoted to stream labels. Use `tag:label` to rename, e.g. `Environment:env,Owner:team`. |
| `RESOURCE_TAGS_INHERIT_RESOURCE_GROUP` | `false` | Also merge in the resource group's tags. |
| `RESOURCE_TAGS_CACHE_TTL_SECONDS` | `3600` | How long looked-up tags are cached. Tag changes show up after at most this long. |
| `AZURE_CLIENT_ID` | _(unset)_ | Client ID of a user-assigned managed identity. The system-assigned identity is used when unset. |

Tag keys are matched case-insensitively, and characters that are not valid in label names are replaced with `_`. Child
resources such as SQL databases also get their parent resource's tags; on conflicts the more specific resource wins.

Only promote low-cardinality tags (environment, team, cost centre) to labels: every distinct combination of label values
creates a new Loki stream. Query everything else as structured metadata, e.g.
`{job="integrations/azure-logexport"} | tag_Owner="team-data"`.

**Permissions:** the function app needs a managed identity with `Reader` or `Monitoring Reader` on every subscription (or
a management group) whose logs it receives. `Monitoring Reader` is the role the
[Azure serverless metrics](https://grafana.com/docs/grafana-cloud/observe-and-act/monitor-infrastructure/monitor-cloud-provider/azure/collect-azure-serverless/)
setup already requires, so one assignment can cover both. The ARM template is resource group scoped and cannot create
this assignment itself; it enables a system-assigned identity and outputs its `functionAppPrincipalId`:

```bash
az role assignment create --assignee-object-id <functionAppPrincipalId> --assignee-principal-type ServicePrincipal \
  --role Reader --scope /subscriptions/<subscription-id>
```

If a lookup fails (missing permission, throttling) a warning is logged, the records are shipped without tags, and the
lookup is retried after 60 seconds.

### Record Processing and Filtering

Each event record can be processed with [jq](https://jqlang.org/manual/) by defining the optional `JQ_PIPELINE` environment variable.

For example given the following event:

```json
{
  "properties": {
    "log": "{\"kind\": \"AuditEvent\"}"
  }
}
```

The nested JSON can be extract with `JQ_PIPELINE=".properties.log | fromjson"`.

If the pipeline returns an array each element will be a new entry.

Empty results are omitted. Thus the pipeline can also be used for filtering.

## Release

A release is triggered by a new tag. Call `make release BUMP=<major|minor|patch>` to create a new tagged release.

The logexport function is packaged as a ZIP file via `make "logexport.$(python -m setuptools_scm).zip"`. The version is
derived from the `setuptools_scm` package. The build process also updates the version in the `_version.py` file.

Once built the package can deployed to Azure using the Azure CLI:

```bash
az functionapp deployment source config-zip -g <resource-group> -n <function-app-name> --src <path-to-zip-file>
```

Changes to `logexport/_version.py` have been ignored with

```bash
git update-index --skip-worktree logexport/_version.py
```
