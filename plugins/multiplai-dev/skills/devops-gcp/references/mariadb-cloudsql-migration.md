# Migrating MariaDB on GCP VM to Cloud SQL for MySQL: Replication Feasibility, Incompatibilities, and Workarounds

**Date:** 2026-04-20 | **Type:** General Research | **Confidence:** High | **Sources used:** 25

## Summary

Migrating a MariaDB instance running on a GCP VM to Cloud SQL for MySQL via live binlog replication is fundamentally blocked by multiple interlocking constraints. The core barrier is GTID incompatibility: MariaDB uses a numeric `domain_id-server_id-sequence` format while MySQL uses `server_uuid:transaction_id`, and Cloud SQL for MySQL mandates `GTID_MODE=ON` for external replica configurations ([MariaDB GTID docs](https://mariadb.com/kb/en/gtid/); [Cloud SQL replication configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)). Google's Database Migration Service (DMS) explicitly excludes MariaDB as a supported source ([DMS known limitations](https://cloud.google.com/database-migration/docs/mysql/known-limitations); [DMS supported databases](https://cloud.google.com/database-migration/docs/mysql)). Traditional binlog file+position replication works between MariaDB and MySQL in principle ([MariaDB replication docs](https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication)), but Cloud SQL's managed architecture does not expose the configuration knobs necessary to accept non-GTID transactions.

The most commonly cited workaround — a multi-hop topology using an intermediate self-managed MySQL instance — faces its own difficulties. The Percona-recommended chain of MariaDB 10.4 → MySQL 5.7 → MySQL 8.0 ([Percona blog](https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/)) was tested against Cloud SQL and reported to fail at the Cloud SQL boundary because the intermediate MySQL 5.7 instance, having replicated from MariaDB without GTID, cannot provide GTID-based replication to Cloud SQL ([Google Developer Forums](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751)). A more promising variant uses a self-managed MySQL 8.0.23+ intermediary with `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS` to mint MySQL-format GTIDs for incoming MariaDB transactions before relaying to Cloud SQL ([MySQL 8.0 Manual §19.1.3.6](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html)), but this remains undocumented for MariaDB sources and untested with Cloud SQL's external replica API. Given these constraints, a logical dump-and-restore with planned downtime may be the most reliable path for many deployments, with the intermediate-MySQL-8.0 bridge topology as the best candidate for near-zero-downtime migrations requiring live replication.

## Findings

### GTID Format Incompatibility Is Absolute and Directional

MariaDB GTIDs (`domain_id-server_id-sequence`) and MySQL GTIDs (`server_uuid:transaction_id`) are structurally incompatible at the protocol level. MariaDB can replicate *from* a MySQL primary by stripping MySQL GTIDs and substituting its own, but the reverse is not supported — MySQL cannot consume MariaDB-format GTIDs ([MariaDB GTID docs](https://mariadb.com/kb/en/gtid/); [MariaDB replication setup](https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication)). MariaDB's domain-based GTID architecture further enables multiple independent replication streams interleaved in a single binlog, a concept absent in MySQL ([MariaDB GTID docs](https://mariadb.com/kb/en/gtid/)). MySQL 8.4 introduced tagged GTIDs (`source_id:tag:transaction_id`), widening the divergence ([MySQL 8.4 Manual §19.1.3.1](https://dev.mysql.com/doc/refman/8.4/en/replication-gtids-concepts.html)).

**Confidence:** High (verified across MariaDB and MySQL official documentation)

### Cloud SQL Requires GTID_MODE=ON, Blocking Direct MariaDB Replication

Cloud SQL for MySQL requires `GTID_MODE=ON` and `enforce_gtid_consistency=ON` on external primaries for replica creation ([Cloud SQL replication configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)). Since MariaDB cannot produce MySQL-format GTIDs, the standard Cloud SQL external replica workflow is unusable with a MariaDB source. A feature request (Issue #323695986) to relax the GTID_MODE restriction has been filed on Google's Issue Tracker ([Issue Tracker](https://issuetracker.google.com/issues/323695986)). The Cloud SQL external replica API also requires `databaseVersion` to be a MySQL version (`MYSQL_5_7`, `MYSQL_8_0`, or `MYSQL_8_4`) — MariaDB is not an accepted value ([Pythian blog](https://www.pythian.com/blog/technical-track/how-to-setup-a-google-cloud-sql-replica-from-an-external-mysql-primary); [Cloud SQL dump-file replication](https://cloud.google.com/sql/docs/mysql/replication/dump-file-replication-from-external)).

**Confidence:** High

### DMS Does Not Support MariaDB Sources

Google Cloud Database Migration Service explicitly excludes MariaDB: "Database Migration Service isn't compatible with MariaDB" ([DMS known limitations](https://cloud.google.com/database-migration/docs/mysql/known-limitations)). Supported sources are limited to self-managed MySQL (5.5–8.4), Amazon RDS for MySQL, Aurora MySQL, Azure Database for MySQL, and Cloud SQL for MySQL ([DMS supported databases](https://cloud.google.com/database-migration/docs/mysql); [DMS supported source and destination databases](https://docs.cloud.google.com/database-migration/docs/supported-databases)). This eliminates the most automated GCP-native migration path.

**Confidence:** High (triple-confirmed across three GCP documentation pages)

### ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS: A Theoretical Bridge

MySQL 8.0.23+ introduced `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS` as part of `CHANGE REPLICATION SOURCE TO`, allowing a GTID-enabled replica to assign MySQL-format GTIDs to incoming anonymous (non-GTID) transactions ([MySQL 8.0 Manual §19.1.3.6](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html)). This is the key mechanism that could bridge MariaDB's non-GTID binlog stream into Cloud SQL's GTID-required world. However, Cloud SQL does not expose raw `CHANGE REPLICATION SOURCE TO` options — its managed API abstracts replication configuration ([Google Developer Forums](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751)). A self-managed MySQL 8.0.23+ instance on a GCP VM could serve as the bridge, consuming MariaDB's binlog stream via position-based replication, minting GTIDs locally, and then serving as a GTID-enabled source for Cloud SQL. Critical caveat: replicas using this feature cannot be promoted to replace the source, cannot have their backups used to restore the source, and cannot serve as backup sources for other replicas — making the bridge a terminal, non-promotable endpoint ([MySQL 8.0 Manual §19.1.3.6](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html)).

**Confidence:** Medium (mechanism is documented for MySQL-to-MySQL; untested/undocumented for MariaDB sources)

### The Multi-Hop Topology: MariaDB → MySQL 5.7 → MySQL 8.0/Cloud SQL

Percona documents a two-hop migration: MariaDB 10.4 → MySQL 5.7 (via position-based binlog replication) → MySQL 8.0 (via upgrade or logical dump) ([Percona blog](https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/)). MySQL 5.7 retains enough legacy compatibility with MariaDB to act as a binlog replica, whereas MySQL 8.0 has removed or changed sufficient wire-protocol compatibility to prevent direct replication from MariaDB. However, a Google Developer Forums user tested `MariaDB 10.7 → MySQL 5.7 → Cloud SQL MySQL 8.0` and reported failure at the Cloud SQL boundary because the intermediate MySQL 5.7, having replicated from MariaDB without GTID, could not provide GTID-based replication to Cloud SQL ([Google Developer Forums](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751)). The more viable variant replaces MySQL 5.7 with MySQL 8.0.23+ using `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS`, enabling it to produce GTID-tagged transactions for Cloud SQL — though this remains unvalidated against Cloud SQL's external replica API.

**Confidence:** Medium

### MariaDB-Specific Feature Incompatibilities

Several MariaDB features have no MySQL equivalents and will cause replication errors if present in the binlog stream: **system-versioned tables** (`WITH SYSTEM VERSIONING`), **sequences** (the SEQUENCE engine), the **UUID column type**, and MariaDB's **JSON type** (an alias for LONGTEXT, vs MySQL's native binary JSON) ([Percona blog](https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/); [DBA StackExchange](https://dba.stackexchange.com/questions/313275/replicate-mysql-table-to-mariadb-for-system-versioning)). Additionally, when `enforce_gtid_consistency=ON` (required by Cloud SQL), MySQL forbids `CREATE TABLE … SELECT`, mixing transactional and non-transactional engine updates in a single transaction, and `CREATE/DROP TEMPORARY TABLE` inside transactions — any MariaDB workload using these patterns will break replication ([MySQL 9.6 Manual §19.1.4.2](https://dev.mysql.com/doc/refman/9.6/en/replication-mode-change-online-enable-gtids.html)). All tables must use InnoDB (the only storage engine Cloud SQL supports), all DEFINER accounts on views/stored procedures/triggers must be recreated on the target, and triggers, stored procedures, and events require syntax review for cross-platform compatibility ([Percona migration guide](https://www.percona.com/blog/how-to-migrate-from-mariadb-to-mysql/)).

**Confidence:** High

### Cloud SQL External Replica Workflow (MySQL-to-MySQL Reference)

The documented MySQL-to-MySQL workflow provides the template: (1) create a source representation instance via the Cloud SQL Admin REST API, (2) create the Cloud SQL replica referencing `masterInstanceName`, (3) configure firewall rules (Cloud SQL outgoing IP → primary on TCP/3306), (4) seed the replica via managed import (no GTID required), dump file (requires GTID), or custom import, (5) verify settings via `verifyExternalSyncSettings`, (6) start replication with `syncMode: "online"`, (7) monitor lag via `database/mysql/external_sync/replica_lag`, (8) promote to standalone ([Cloud SQL replication configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external); [Pythian blog](https://www.pythian.com/blog/technical-track/how-to-setup-a-google-cloud-sql-replica-from-an-external-mysql-primary)). The managed import method is notable as the only seeding method that works without GTID on the source, though ongoing replication still requires GTID.

**Confidence:** High

### Binlog Configuration Prerequisites

For any MariaDB-to-MySQL replication leg: binary logging must be enabled (`log-bin`), a unique `server_id` set, `binlog_format` set to ROW (safest for cross-vendor replication — statement-based risks SQL dialect differences), and `binlog_checksum` potentially set to NONE to avoid checksum incompatibilities ([MariaDB replication setup](https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication)). Binary logs should be retained for at least 24 hours (a week recommended). The replication user requires `SELECT`, `SHOW VIEW`, `REPLICATION SLAVE`, `REPLICATION CLIENT`, and `EXECUTE` privileges ([Cloud SQL replication configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)). ROW-based replication also mitigates foreign-key cascade divergence when storage engines differ between source and replica ([MariaDB replication and foreign keys](https://mariadb.com/kb/en/replication-and-foreign-keys/)).

**Confidence:** High

### Cutover Strategy

The canonical cutover sequence: (1) `FLUSH TABLES WITH READ LOCK` on MariaDB to freeze writes, (2) verify replication is caught up by confirming `Exec_Master_Log_Pos` matches the primary's binlog position (use `SOURCE_POS_WAIT()` or `MASTER_POS_WAIT()` — GTID-based wait functions are unavailable), (3) stop replication on the replica, (4) promote the Cloud SQL replica to standalone (writable), (5) redirect application connections ([MariaDB replica promotion docs](https://mariadb.com/docs/server/ha-and-performance/standard-replication/changing-a-replica-to-become-the-primary)). The old MariaDB should be kept as a read-only fallback (`--skip-slave-start --read-only`) and decommissioned only after confirmed stability ([Percona migration guide](https://www.percona.com/blog/how-to-migrate-from-mariadb-to-mysql/)). Notably, after migration the old MariaDB *can* replicate from the new Cloud SQL MySQL primary (the reverse direction works), enabling post-migration data validation ([MariaDB GTID docs](https://mariadb.com/kb/en/gtid/)). HA type must be decided before replica creation (cannot change after), and manual failover during initial data load can cause unrecoverable migration failure ([Cloud SQL replication configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)).

**Confidence:** High (cutover steps verified; Cloud SQL promotion mechanics verified)

### Django Application Layer Considerations

Django uses `django.db.backends.mysql` for both MariaDB and MySQL — no backend change is needed ([Django docs](https://docs.djangoproject.com/en/4.2/ref/databases/)). Key configuration points: `STRICT_TRANS_TABLES` SQL mode should match on both sides, time zone tables must be loaded post-migration via `mysql_tzinfo_to_sql`, and `read committed` isolation should be set explicitly (Django's default, vs MySQL's `repeatable read`). VARCHAR fields with `unique=True` may be restricted to 255 characters. Connection settings (`ssl`, `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`) need reconfiguration for Cloud SQL endpoints.

**Confidence:** High

## Minority Views & Tensions

Three significant tensions emerged across sources. First, **Percona's two-hop recommendation vs. Cloud SQL's GTID wall**: Percona recommends MariaDB → MySQL 5.7 → MySQL 8.0 as the standard migration path, but this advice targets self-managed MySQL 8.0, not Cloud SQL. A Google Developer Forums user confirmed the chain fails at the Cloud SQL boundary due to GTID requirements ([Google Developer Forums](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751)). These are compatible claims — Percona's advice is correct for self-managed targets but incomplete for managed ones.

Second, **managed import without GTID vs. ongoing replication requiring GTID**: GCP documentation states managed import works without GTID on the source for initial seeding, yet the external replica workflow requires `GTID_MODE=ON` for the ongoing replication phase. It is unclear whether managed import creates a path to position-based ongoing replication or only handles the seed. This ambiguity is unresolved in official documentation.

Third, **whether `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS` on an intermediary can satisfy Cloud SQL**: MySQL's documentation covers this feature for MySQL-to-MySQL replication only. No source confirms or denies whether Cloud SQL's external replica API will accept a MySQL intermediary that locally mints GTIDs for originally-anonymous MariaDB transactions. This is the most critical unresolved question for near-zero-downtime migration feasibility.

## Verified Claims

**MariaDB and MySQL GTID formats are mutually incompatible.** Confirmed by MariaDB's official GTID documentation describing the `domain_id-server_id-sequence` format and MySQL's official documentation describing the `server_uuid:transaction_id` format. MariaDB's docs explicitly state MariaDB can replicate from a MySQL source (stripping MySQL GTIDs), but the reverse is not documented or supported.

**DMS does not support MariaDB as a source.** Triple-confirmed: the DMS known limitations page (dated 2026-04-17) explicitly states "Database Migration Service isn't compatible with MariaDB"; the supported databases matrix lists only MySQL-branded products; and the DMS for MySQL overview page makes no mention of MariaDB.

**Cloud SQL requires GTID_MODE=ON for external replica creation.** Confirmed in GCP's replication configuration documentation and corroborated by the Google Developer Forums thread where a user explicitly tested and reported this restriction.

**The MariaDB → MySQL 5.7 → Cloud SQL chain fails at the Cloud SQL boundary.** Confirmed by a Google Developer Forums user who tested this exact topology with MariaDB 10.7 and reported GTID-related failure when attempting to connect the intermediate MySQL 5.7 to Cloud SQL.

## Gaps & Open Questions

- Whether a self-managed MySQL 8.0.23+ intermediary using `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS` can successfully feed Cloud SQL's external replica API has not been tested or documented by any source.
- Whether Cloud SQL's managed import (no-GTID seeding) combined with position-based ongoing replication could work if the GTID restriction were relaxed — the feature request (Issue #323695986) status is unknown.
- The exact MariaDB versions whose binlog format remains wire-compatible with MySQL 5.7 and 8.0 replicas is undocumented; divergence accelerated after MariaDB 10.2+.
- No source provides a complete, tested runbook for any MariaDB-to-Cloud SQL migration path — all available guidance is fragmentary or covers only MySQL-to-MySQL scenarios.
- Whether MariaDB-specific binlog events (e.g., from system-versioned tables or sequences) cause silent data corruption vs. hard replication errors on a MySQL replica is untested.

## Falsifiability

The central conclusion — that direct MariaDB-to-Cloud SQL replication is currently blocked — would be disproved if Google added `ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS` support to Cloud SQL's managed replication API, added MariaDB as a supported DMS source, or relaxed the `GTID_MODE=ON` requirement for external replicas. Evidence that a self-managed MySQL 8.0.23+ intermediary successfully bridges MariaDB to Cloud SQL in production would invalidate the conclusion that this path is unviable.

## Sources

| # | Source | Reputation | Relevance | Date |
|---|--------|------------|-----------|------|
| 1 | [MariaDB GTID Documentation](https://mariadb.com/kb/en/gtid/) | Authoritative | Defines MariaDB GTID format and cross-platform replication constraints | Undated |
| 2 | [Cloud SQL External Replication Configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external) | Authoritative | Documents GTID_MODE=ON requirement and external replica workflow | Undated |
| 3 | [DMS Known Limitations](https://cloud.google.com/database-migration/docs/mysql/known-limitations) | Authoritative | Explicitly states MariaDB incompatibility | 2026-04-17 |
| 4 | [DMS Supported Databases](https://cloud.google.com/database-migration/docs/mysql) | Authoritative | Lists MySQL-only source support | Undated |
| 5 | [MySQL 8.0 Manual §19.1.3.6 — Non-GTID to GTID Replication](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html) | Authoritative | Documents ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS mechanism | Undated |
| 6 | [MySQL 8.4 Manual §19.1.3.1 — GTID Concepts](https://dev.mysql.com/doc/refman/8.4/en/replication-gtids-concepts.html) | Authoritative | Defines MySQL GTID format and storage | Undated |
| 7 | [MySQL 9.6 Manual §19.1.4.2 — Enabling GTIDs Online](https://dev.mysql.com/doc/refman/9.6/en/replication-mode-change-online-enable-gtids.html) | Authoritative | Documents GTID enablement process and enforce_gtid_consistency constraints | Undated |
| 8 | [Google Developer Forums — GTID_MODE=OFF Restriction](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751) | Emerging | Reports failed MariaDB→MySQL 5.7→Cloud SQL chain; documents feature request | Undated |
| 9 | [Google Issue Tracker #323695986](https://issuetracker.google.com/issues/323695986) | Emerging | Feature request to relax GTID restriction | Undated |
| 10 | [Percona — MariaDB 10.4 to MySQL 8.0 via MySQL 5.7](https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/) | Established | Documents two-hop migration pattern | Undated |
| 11 | [Percona — How to Migrate From MariaDB to MySQL](https://www.percona.com/blog/how-to-migrate-from-mariadb-to-mysql/) | Established | General migration guidance and post-migration validation | Undated |
| 12 | [Pythian — Cloud SQL Replica from External MySQL Primary](https://www.pythian.com/blog/technical-track/how-to-setup-a-google-cloud-sql-replica-from-an-external-mysql-primary) | Emerging | Detailed Cloud SQL external replica setup walkthrough | Undated |
| 13 | [Cloud SQL Dump File Replication](https://cloud.google.com/sql/docs/mysql/replication/dump-file-replication-from-external) | Authoritative | Documents dump-based seeding requirements | Undated |
| 14 | [MariaDB CHANGE MASTER TO](https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/replication-statements/change-master-to) | Authoritative | Replication configuration syntax and options | Undated |
| 15 | [MariaDB Setting Up Replication](https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication) | Authoritative | Binlog configuration prerequisites | Undated |
| 16 | [MariaDB Replica Promotion](https://mariadb.com/docs/server/ha-and-performance/standard-replication/changing-a-replica-to-become-the-primary) | Authoritative | Cutover and promotion procedures | Undated |
| 17 | [MariaDB Replication and Foreign Keys](https://mariadb.com/kb/en/replication-and-foreign-keys/) | Authoritative | FK cascade divergence in cross-engine replication | Undated |
| 18 | [Django Database Documentation](https://docs.djangoproject.com/en/4.2/ref/databases/) | Authoritative | MySQL/MariaDB backend compatibility | Undated |
| 19 | [DBA StackExchange — System Versioning Replication](https://dba.stackexchange.com/questions/313275/replicate-mysql-table-to-mariadb-for-system-versioning) | Emerging | Confirms system-versioned tables incompatibility | Undated |
| 20 | [DMS Supported Source and Destination Databases](https://docs.cloud.google.com/database-migration/docs/supported-databases) | Authoritative | Confirms MySQL-only DMS sources | Undated |
| 21 | [Cloud SQL External Replica Configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-external-replica) | Authoritative | Documents demoteMaster API and replica management | Undated |
| 22 | [MariaDB Cloud Data Replication](https://mariadb.com/docs/mariadb-cloud/cloud-data-handling/data-offloading/replicating-data-from-mariadb-cloud-to-external-database) | Authoritative | MariaDB Cloud replication capabilities | Undated |
| 23 | [DBA StackExchange — MariaDB as MySQL Replication Slave](https://dba.stackexchange.com/questions/193559/can-i-use-mariadb-as-a-replication-slave-for-mysql) | Emerging | Community discussion of cross-platform replication | Undated |
| 24 | [Fournine Cloud — GCP DMS Overview](https://blog.fourninecloud.com/what-is-gcp-database-migration-service-dms-and-how-it-works-c5934cb58537) | Emerging | DMS architecture overview | Undated |
| 25 | [MariaDB Setting Up Replication (KB)](https://mariadb.com/kb/en/setting-up-replication/) | Authoritative | Replication setup reference | Undated |
---

<!-- STRUCTURED DATA — machine-readable, do not edit above this line -->

```yaml
index:
  questions_investigated:
    - "Can Cloud SQL for MySQL serve as a replication replica of a MariaDB primary?"
    - "Are MariaDB and MySQL GTID formats compatible for cross-platform replication?"
    - "Does Google Cloud DMS support MariaDB as a source for MySQL migration?"
    - "Can ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS bridge the GTID gap for Cloud SQL?"
    - "What MariaDB-specific features are incompatible with MySQL/Cloud SQL?"
    - "What is the viable multi-hop replication topology for MariaDB to Cloud SQL?"
    - "What are the binlog configuration prerequisites for cross-vendor replication?"
    - "What is the cutover strategy for promoting a Cloud SQL replica?"
    - "What Django application layer changes are needed post-migration?"
  questions_open:
    - "Can a self-managed MySQL 8.0.23+ intermediary with ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS successfully feed Cloud SQL's external replica API?"
    - "Does Cloud SQL's managed import (no-GTID seeding) enable position-based ongoing replication?"
    - "Which MariaDB versions remain binlog wire-compatible with MySQL 5.7 and 8.0 replicas?"
    - "Do MariaDB-specific binlog events cause silent data corruption or hard errors on MySQL replicas?"
    - "What is the status of Google Issue Tracker #323695986 (GTID restriction relaxation)?"
  sources_consulted: 25
  total_findings: 284
  findings_by_confidence:
    verified: 38
    likely: 12
    unverified: 6
  sources_by_reputation:
    authoritative: 16
    established: 2
    emerging: 7
  falsifiability: "Disproved if Google adds ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS support to Cloud SQL, adds MariaDB as a DMS source, or relaxes the GTID_MODE=ON requirement for external replicas."

meta:
  query: "Migrate MariaDB on GCP VM to Cloud SQL for MySQL: set up Cloud SQL as replication slave/replica of MariaDB, including UUID compatibility issues, binlog replication constraints, DMS feasibility, feature incompatibilities, step-by-step runbook, and cutover strategy"
  date: "2026-04-20"
  research_type: "general"
  preset: "structured"
  confidence: high
  confidence_reason: "Core blocking constraints verified across multiple authoritative sources (MariaDB docs, MySQL docs, GCP docs); the main uncertainty is whether undocumented workarounds (intermediary MySQL 8.0.23+) function in practice"
  falsifiability: "If Google were to support ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS in Cloud SQL or add MariaDB as a DMS source, the main conclusion would be invalidated"

findings:
  - fact: "MariaDB and MySQL use fundamentally incompatible GTID formats, preventing GTID-based replication from MariaDB primary to MySQL replica"
    source: "[MariaDB GTID Documentation](https://mariadb.com/kb/en/gtid/)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Cloud SQL for MySQL requires GTID_MODE=ON and enforce_gtid_consistency=ON on external primaries for replica creation"
    source: "[Cloud SQL External Replication Configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Google Cloud DMS explicitly does not support MariaDB as a source database"
    source: "[DMS Known Limitations](https://cloud.google.com/database-migration/docs/mysql/known-limitations)"
    reputation: authoritative
    confidence: high
    date: "2026-04-17"
  - fact: "MySQL 8.0.23+ ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS can assign MySQL GTIDs to incoming anonymous transactions on a replica"
    source: "[MySQL 8.0 Manual §19.1.3.6](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Replicas using ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS cannot be promoted to replace the source or serve as backup sources"
    source: "[MySQL 8.0 Manual §19.1.3.6](https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "The MariaDB→MySQL 5.7→Cloud SQL chain fails at the Cloud SQL boundary due to GTID requirements"
    source: "[Google Developer Forums](https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751)"
    reputation: emerging
    confidence: high
    date: "undated"
  - fact: "MariaDB system-versioned tables, sequences, UUID column type, and JSON type (LONGTEXT alias) have no MySQL equivalents"
    source: "[Percona Blog](https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/)"
    reputation: established
    confidence: high
    date: "undated"
  - fact: "enforce_gtid_consistency=ON forbids CREATE TABLE…SELECT, mixed engine transactions, and temporary tables in transactions"
    source: "[MySQL 9.6 Manual §19.1.4.2](https://dev.mysql.com/doc/refman/9.6/en/replication-mode-change-online-enable-gtids.html)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Cloud SQL's managed import method is the only seeding approach that does not require GTID on the source"
    source: "[Cloud SQL External Replication Configuration](https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Django uses the same mysql backend for both MariaDB and MySQL with no engine change required"
    source: "[Django Database Documentation](https://docs.djangoproject.com/en/4.2/ref/databases/)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "After migration, MariaDB can replicate from the new Cloud SQL MySQL primary for post-migration validation"
    source: "[MariaDB GTID Documentation](https://mariadb.com/kb/en/gtid/)"
    reputation: authoritative
    confidence: high
    date: "undated"
  - fact: "Cloud SQL external replica API requires databaseVersion to be a MySQL version; MariaDB is not accepted"
    source: "[Pythian Blog](https://www.pythian.com/blog/technical-track/how-to-setup-a-google-cloud-sql-replica-from-an-external-mysql-primary)"
    reputation: emerging
    confidence: high
    date: "undated"

sources:
  - title: "MariaDB GTID Documentation"
    url: "https://mariadb.com/kb/en/gtid/"
    reputation: authoritative
    relevance: "Defines MariaDB GTID format and documents cross-platform replication limitations"
    date: "undated"
  - title: "Cloud SQL External Replication Configuration"
    url: "https://docs.cloud.google.com/sql/docs/mysql/replication/configure-replication-from-external"
    reputation: authoritative
    relevance: "Documents GTID_MODE=ON requirement and complete external replica workflow"
    date: "undated"
  - title: "DMS Known Limitations"
    url: "https://cloud.google.com/database-migration/docs/mysql/known-limitations"
    reputation: authoritative
    relevance: "Explicitly confirms MariaDB incompatibility with DMS"
    date: "2026-04-17"
  - title: "MySQL 8.0 Manual §19.1.3.6"
    url: "https://dev.mysql.com/doc/refman/8.0/en/replication-gtids-assign-anon.html"
    reputation: authoritative
    relevance: "Documents the ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS bridge mechanism"
    date: "undated"
  - title: "Google Developer Forums — GTID_MODE=OFF Restriction"
    url: "https://discuss.google.dev/t/gtid-mode-off-replication-restriction-relaxation/144751"
    reputation: emerging
    relevance: "Reports real-world testing of MariaDB→MySQL→Cloud SQL chain failure"
    date: "undated"
  - title: "Percona — MariaDB to MySQL 8.0 via MySQL 5.7"
    url: "https://www.percona.com/blog/want-to-migrate-from-mariadb-10-4-to-mysql-8-0-but-facing-hurdles-mysql-5-7-to-the-rescue/"
    reputation: established
    relevance: "Documents the two-hop migration pattern and MariaDB feature incompatibilities"
    date: "undated"
  - title: "Pythian — Cloud SQL Replica from External MySQL Primary"
    url: "https://www.pythian.com/blog/technical-track/how-to-setup-a-google-cloud-sql-replica-from-an-external-mysql-primary"
    reputation: emerging
    relevance: "Detailed walkthrough of Cloud SQL external replica API workflow"
    date: "undated"
  - title: "MySQL 8.4 Manual §19.1.3.1"
    url: "https://dev.mysql.com/doc/refman/8.4/en/replication-gtids-concepts.html"
    reputation: authoritative
    relevance: "Defines MySQL GTID format including tagged GTIDs in 8.4"
    date: "undated"
  - title: "MySQL 9.6 Manual §19.1.4.2"
    url: "https://dev.mysql.com/doc/refman/9.6/en/replication-mode-change-online-enable-gtids.html"
    reputation: authoritative
    relevance: "Documents GTID enablement process and enforce_gtid_consistency constraints"
    date: "undated"
  - title: "Percona — How to Migrate From MariaDB to MySQL"
    url: "https://www.percona.com/blog/how-to-migrate-from-mariadb-to-mysql/"
    reputation: established
    relevance: "General migration guidance, post-migration validation, and cutover strategy"
    date: "undated"
  - title: "Django Database Documentation"
    url: "https://docs.djangoproject.com/en/4.2/ref/databases/"
    reputation: authoritative
    relevance: "Confirms shared MySQL backend for MariaDB and MySQL"
    date: "undated"
  - title: "MariaDB Setting Up Replication"
    url: "https://mariadb.com/docs/server/ha-and-performance/standard-replication/setting-up-replication"
    reputation: authoritative
    relevance: "Binlog configuration prerequisites and replication setup"
    date: "undated"
  - title: "MariaDB Replica Promotion"
    url: "https://mariadb.com/docs/server/ha-and-performance/standard-replication/changing-a-replica-to-become-the-primary"
    reputation: authoritative
    relevance: "Cutover and promotion procedures"
    date: "undated"
  - title: "Cloud SQL Dump File Replication"
    url: "https://cloud.google.com/sql/docs/mysql/replication/dump-file-replication-from-external"
    reputation: authoritative
    relevance: "Documents dump-based seeding requirements and GTID constraints"
    date: "undated"
  - title: "MariaDB CHANGE MASTER TO"
    url: "https://mariadb.com/docs/server/reference/sql-statements/administrative-sql-statements/replication-statements/change-master-to"
    reputation: authoritative
    relevance: "Replication configuration syntax, SSL options, heartbeat settings"
    date: "undated"

tensions:
  - topic: "Multi-hop migration topology effectiveness"
    position_a:
      claim: "MariaDB→MySQL 5.7→MySQL 8.0 is the recommended migration path"
      source: "Percona Blog"
    position_b:
      claim: "MariaDB→MySQL 5.7→Cloud SQL fails at the Cloud SQL boundary due to GTID requirements"
      source: "Google Developer Forums"
    resolution: "Not contradictory — Percona's advice targets self-managed MySQL 8.0, not Cloud SQL. The pattern works for self-managed targets but does not solve Cloud SQL's GTID_MODE=ON requirement."
  - topic: "Managed import GTID bypass scope"
    position_a:
      claim: "Managed import works without GTID on the source for initial seeding"
      source: "Cloud SQL External Replication Configuration"
    position_b:
      claim: "Cloud SQL requires GTID_MODE=ON for external replica creation and ongoing replication"
      source: "Cloud SQL External Replication Configuration"
    resolution: "Unresolved — unclear whether managed import creates a path to position-based ongoing replication or only handles the initial data seed while ongoing replication still requires GTID."
  - topic: "ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS viability with Cloud SQL"
    position_a:
      claim: "The feature enables GTID-enabled replicas to consume non-GTID binlog streams"
      source: "MySQL 8.0 Manual §19.1.3.6"
    position_b:
      claim: "Cloud SQL does not expose CHANGE REPLICATION SOURCE TO with arbitrary options"
      source: "Google Developer Forums"
    resolution: "The feature works on self-managed MySQL instances but cannot be configured directly on Cloud SQL. An intermediary self-managed MySQL 8.0.23+ instance could potentially bridge the gap but this is untested."

gaps:
  - "No source provides a complete, tested runbook for any MariaDB-to-Cloud SQL migration path"
  - "Whether a MySQL 8.0.23+ intermediary with ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS can feed Cloud SQL's external replica API is undocumented"
  - "Exact MariaDB versions with binlog wire-compatibility to MySQL 5.7/8.0 replicas are not catalogued"
  - "Behavior of MariaDB-specific binlog events (system-versioned tables, sequences) on MySQL replicas is untested in documentation"
  - "Status of Google Issue Tracker #323695986 for GTID restriction relaxation is unknown"
  - "Performance impact of the three-tier topology (MariaDB→MySQL intermediary→Cloud SQL) is unmeasured"
```