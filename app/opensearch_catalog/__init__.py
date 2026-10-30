"""
OpenSearch source-specific catalogs (kept separate from the Prometheus metric
catalog in app/catalog/ by design).

  registry.py       index registry -- which patterns/fields MAY be queried
                    (reviewed configuration: index_registry.json)
  field_catalog.py  runtime field catalog -- what the live mappings say EXISTS
                    and what each field can do (discovered, TTL-cached)

The registry grants permission; the field catalog confirms existence and
capability. A generated query may use only what both agree on
(`build_field_view`).
"""
from app.opensearch_catalog.field_catalog import (
    FieldCatalog,
    FieldCatalogCache,
    FieldInfo,
    PatternFieldView,
    build_field_catalog,
    build_field_view,
    make_mapping_fetcher,
)
from app.opensearch_catalog.registry import (
    DEFAULT_REGISTRY_PATH,
    IndexRegistry,
    PatternEntry,
    RegistryError,
    load_registry,
)

__all__ = [
    "DEFAULT_REGISTRY_PATH", "FieldCatalog", "FieldCatalogCache", "FieldInfo",
    "IndexRegistry", "PatternEntry", "PatternFieldView", "RegistryError",
    "build_field_catalog", "build_field_view", "load_registry", "make_mapping_fetcher",
]
