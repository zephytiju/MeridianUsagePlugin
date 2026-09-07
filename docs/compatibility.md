# Compatibility

Version 2.0.0 targets on Python 3.12 through 3.14 against these exact public
releases:

| Distribution | Version |
| --- | --- |
| `meridian-storage-core` | 1.0.1 |
| `meridian-storage-semantics` | 2.0.0 |
| `meridian-storage-query` | 1.0.2 |
| `meridian-storage-evidence` | 1.0.1 |
| `meridian-plugin-observability` | 1.0.2 |

ClickHouse 1.0.1 is an integration-test extra. Runtime source does not import
it and is portable across compatible structured placements.

Locked design evidence:

| Design | Revision |
| --- | --- |
| Meridian HLD | 56 |
| Catalogs / Public Interfaces | 70 |
| Engine Adapters | 24 |
| Kafka Streaming LLD | 6 |
| MeridianConstructs | 45 |
| Usage LLD | 22 |

The authoritative Usage LLD retains the public distribution name
`meridian-plugin-usage`. The stable import namespace remains
`meridian_storage.plugins.usage`.

PostgreSQL 2.1.1 is the real-engine test dependency. This release consumes the
structured put 2.0.0 contract: immutable records use explicit `if_absent`, while
existing checkpoint and claim transitions use conditional `structured.patch` with the storage
version returned by Meridian. Initial state uses `if_absent` without a version;
`expected_revision=0` remains a Usage domain precondition and is never passed
as a storage creation sentinel. There are no deliberate upsert call sites.

Deployments must pin the structured Catalog contract to 2.0.0 and update package
and capability fingerprints together. Unsupported adapters fail closed. Usage
record APIs and v1 decimal/fingerprint encoding are retained. The historical
1.0.0/1.0.2 reproduction jobs retain their separate released package closure.


State schema activation is explicit. The package preserves checkpoint/claim
schema 1.0.0 definitions in its bundle and supplies 2.0.0 definitions for new
state writes. Only watermark/owner/expiry, revision, timestamp and fingerprint
fields become mutable; identity and scope stay immutable. Record shape and
fingerprint encoding do not change. `checkpoint_schema(version="1.0.0")` and
`claim_schema(version="1.0.0")` reproduce the published old definitions.
Deployment owners must activate the new schema versions and matching binding
layouts without dropping/recreating state resources. The library does not
migrate or reset persisted state. Old layouts reject updates until activated.
Conditional patch avoids assigning immutable scope while checking the returned
storage version. State requests derive distinct internal idempotency keys from
the existing Expression fingerprint; replay remains owned by Meridian.


Aggregate numeric readback reuses the 1.0.3 equivalent-decimal text enumeration
and authenticates the entire stored v1 fingerprint. It does not strip scale
from newly published values or change arithmetic. Changed facts remain a
conflict; malformed stored fingerprints are rejected. This retains legacy
aggregate bytes as well as the unchanged Event recovery path.
