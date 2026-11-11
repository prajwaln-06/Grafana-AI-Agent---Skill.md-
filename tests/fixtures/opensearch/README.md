# OpenSearch fixtures

`mapping_*.json` in this directory are **synthetic** `GET <pattern>/_mapping` responses, written
from the documented document shapes of the three supported index families
(see `skills/references/opensearch-logs/`).

## Provenance
These fixtures are **not** exports of the live cluster's mappings:
- The real mappings have not been obtained yet.
- `.keyword` sub-fields, `ignore_above` values (256), and simulated index date names
  (`slingshot_crayfabrichealth-2026.10.26`, `controllerlog-2026.10.26`, `log_iml-2026.10.26`)
  are assumptions introduced to exercise code paths (dynamic string mapping with a
  `.keyword` sub-field, fields with no keyword sub-field, boolean fields, etc.).
- Unit tests rely on these synthetic fixtures for deterministic offline execution.

## Future Live Mapping Exports: Readiness & Structure
When cluster access becomes available, live mapping exports should be placed in a dedicated
subdirectory to preserve unit test determinism:

    tests/fixtures/opensearch/live/
      ├── live_mapping_fabric_health.json
      ├── live_mapping_controller_log.json
      └── live_mapping_ilo_events.json

Existing unit tests will continue to run against the synthetic baseline in `tests/fixtures/opensearch/`
while integration/reconciliation tests can run against `tests/fixtures/opensearch/live/`.

## Sanitization Requirements for Future Live Exports
Before committing any live export:
1. **Remove Credentials & Auth**: Strip any headers, tokens, user credentials, or cluster auth blocks.
2. **Remove Cluster Infrastructure Details**: Remove cluster UUIDs, internal IP addresses, ports,
   routing table details, internal cluster endpoints, and private node names.
3. **Remove Document Payloads**: Mapping files must contain field names, types, and mappings ONLY
   (`{"properties": { ... }}`). Never include `_source` data, log message bodies, user names,
   passwords, or hostnames in mapping fixtures.
4. **Preserve Structural Schema**: Retain the exact field types (`text`, `keyword`, `long`, `boolean`, `date`),
   nested property trees, multi-fields (`fields.keyword`), `ignore_above` settings, and dynamic templates.

## Comparison Procedure
To compare a live export against synthetic fixtures:
1. Run `python scripts/smoke_test_opensearch.py --save-mappings tests/fixtures/opensearch/live/`.
2. Inspect `registered_fields_missing_live` and `role_bindings_dropped` output.
3. Diff the field paths against `queryable_fields` and `groupable_fields` in
   `app/opensearch_catalog/index_registry.json`.
4. Confirm whether text fields in the live cluster actually have `.keyword` subfields before relying on them.
