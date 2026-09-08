# Compatibility

## 2.0.2 public dependency repair

Usage 2.0.2 supports Python 3.12–3.14. Runtime requirements describe the public API
families needed by this plugin. They are separate from the exact deployment-selected
validation recipe in `requirements-release.in` and its complete public artifact hash
lock, `requirements-release.txt`.

| Distribution | API compatibility bound | Validation release | Reason for minimum |
| --- | --- | --- | --- |
| `meridian-storage-core` | `>=1.1.0,<2` | 1.1.0 | Released runtime/SPI and required Evidence contract |
| `meridian-storage-semantics` | `>=2.0.1,<3` | 2.0.1 | Explicit structured put 2.0 and normally installable Core closure |
| `meridian-storage-query` | `>=1.0.3,<2` | 1.0.3 | Public query normalization and compatible dependency metadata |
| `meridian-storage-evidence` | `>=1.0.2,<2` | 1.0.2 | Atomic append contract and Core 1.1 dependency closure |
| `meridian-plugin-observability` | `>=1.0.3,<2` | 1.0.3 | Existing provider/correlation API and compatible closure |
| `meridian-storage-postgresql` (test extra) | `>=2.2.0,<3` | 2.2.0 | Released descriptor/probe and atomic Evidence repair |
| `meridian-storage-clickhouse` (test extra) | `>=1.1.1,<2` | 1.1.1 | Released query cursor and dependency repairs |

Upper bounds preserve the existing public API major families. They do not certify
untested releases. The lower bounds select the repaired public closure; a historical
exact package recipe is not an operation contract. Both adapters remain test extras;
Usage runtime source imports neither. No overrides, sibling source imports, or
`--no-deps` installs are used.

Reproduce the selected validation environment with:

```console
uv pip compile pyproject.toml requirements-release.in --extra test --universal --generate-hashes -o requirements-release.txt
python -m pip install --require-hashes -r requirements-release.txt
python -m pip install -e '.[test]'
python -m pip check
```

The lock records public wheel/sdist hashes for each resolved dependency, including
Projection 1.0.3 and OTel 1.44.0. CI and release jobs install it with hash verification,
then resolve Usage normally and run package checks. Deployment owners may select
other compatible releases and must validate their own exact closure.

No Usage record, Schema, public API, structured-write choice, arithmetic or duplicate
handling changes. The schema-provider bundle fingerprint changes with the package
version; deployment owners must regenerate that pin. All Event, Meter, Aggregate and
Schema golden fingerprints are unchanged. The packaged informational compatibility
manifest is explicitly versioned as `meridian.usage.compatibility.v2`: `dependencies`
contains distribution ranges and `contracts` contains actual API/Schema contract
identifiers. This does not change persisted records or runtime configuration formats.

The required live suite includes PostgreSQL/PostGIS immutable retries, decimals,
corrections, concurrent registration, checkpoint races and default Resource queries.
It adds explicit same-Binding Usage/Event + required Evidence commit/rollback and
restart/replay checks. An external consumer copies only committed Evidence receipts
to real ClickHouse, exercising append retry, keyset pagination, scope isolation,
unbounded-query rejection and transaction rejection; the serialized public Usage
record and its decimal fingerprint survive the complete path. ClickHouse is an
Evidence consumer and does not gain authoritative conditional Usage write guarantees.
Engine images are pinned by digest in both CI and publication workflows. Only the
combinations actually tested are verified.

## Historical design and migration context

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

The 2.0.0/2.0.1 recipe used PostgreSQL 2.1.1. Usage still consumes the
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


## 2.0.1 query and Resource activation compatibility

The Usage API still accepts `gte`, `lt`, and the other documented Usage operators.
Execution lowers these to Query 1.0.2's `$gte`, `$lt`, etc. Query operand interpretation remains identical to the existing logical plan,
including its field-reference and escaped-dollar syntax; plan fingerprints are unchanged.
The window remains start-inclusive and end-exclusive on `windowStart`.

The default Event and Aggregate Resources now select the existing `time-series`
profile declared by their unchanged 1.0.0 Schemas, instead of unsupported `usage`.
Resource identities, labels, scope behavior, requirements, and relationships stay
unchanged. Resource fingerprints for these two Resources and the provider bundle
fingerprint change. Deployment composition must regenerate those pins and the
corresponding binding layouts, then validate/activate through the existing adapter
migration contract before startup. Startup performs no DDL or silent pin rewriting.
A previously rejected default `usage` profile is not an active supported PostgreSQL
layout. Deployments already using Schema-derived `time-series` layouts retain their
logical identities and data; reconcile fingerprints through deployment activation.
All Schema fingerprints, v1 event/aggregate encoding and decimal arithmetic, and
the Usage 2.0.0 checkpoint/claim migration requirements above remain unchanged.
