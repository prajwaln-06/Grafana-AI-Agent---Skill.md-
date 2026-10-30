# OpenSearch fixtures

`mapping_*.json` are **synthetic** `GET <pattern>/_mapping` responses, written
from the documented document shapes of the three supported index families
(see `skills/references/opensearch-logs/`). They are **not** exports of the
live cluster's mappings: the real mappings have not been obtained yet, and
`.keyword` sub-fields, `ignore_above` values and per-day drift here are
assumptions made to exercise the code paths (dynamic string mapping with a
`.keyword` sub-field; a field with no keyword sub-field; a field present only
in the newer of two daily indices).

They contain field names and types only: no log content, hostnames, addresses
or user names. Do not add real log documents or live exports here. Replace or
supplement these with sanitised real mappings once available.
