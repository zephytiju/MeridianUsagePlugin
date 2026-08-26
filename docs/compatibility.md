# Compatibility

Version 1.0.0 is tested on Python 3.12 through 3.14 against these exact public
releases:

| Distribution | Version |
| --- | --- |
| `meridian-storage-core` | 1.0.0 |
| `meridian-storage-semantics` | 1.0.0 |
| `meridian-storage-query` | 1.0.0 |
| `meridian-storage-evidence` | 1.0.0 |
| `meridian-plugin-observability` | 1.0.0 |

ClickHouse 1.0.0 is an integration-test extra. Runtime source does not import
it and is portable across compatible structured placements.

Locked design evidence:

| Design | Revision |
| --- | --- |
| Meridian HLD | 56 |
| Catalogs / Public Interfaces | 70 |
| Engine Adapters | 24 |
| Kafka Streaming LLD | 6 |
| MeridianConstructs | 45 |
| Usage LLD | 18 |

The task dispatch establishes the public distribution name
`meridian-plugin-usage`. The stable import namespace is
`meridian_storage.plugins.usage`.
