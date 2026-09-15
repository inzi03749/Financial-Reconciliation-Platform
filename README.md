# Automated Partner Settlement Reconciliation Platform

## Overview

In modern financial technology ecosystems, businesses partner with third-party payment gateways, banks, and merchant acquirers to process customer payments and settlements. At regular intervals (such as daily cut-offs), partners issue external settlement files detailing the transactions they processed and settled. 

Discrepancies frequently arise between the company's internal transaction ledger and external partner settlement reports due to:
- **Amount mismatches** (e.g., unexpected processing fees deducted or processing discrepancies)
- **Currency mismatches** (e.g., cross-border transactions settled in an unexpected currency)
- **Missing transactions** (transactions recorded internally that were dropped by the partner, or unexpected partner transactions not found in the internal system)
- **Duplicate records** (settlement files containing duplicate entries for a single transaction ID)

This platform automates the settlement reconciliation process by systematically matching internal ledger entries against external partner records, identifying discrepancies with average linear-time lookup efficiency, enforcing file idempotency, and persisting discrepancies to a database for auditability and manual investigation.

---

## Problem Statement

Manual, spreadsheet-based financial reconciliation is a major operational bottleneck for growing fintech platforms. As daily settlement volumes scale to tens of thousands of transactions:
1. **Human Error**: Manual checks in spreadsheets routinely miss subtle fee discrepancies, rounding errors, and duplicate entries.
2. **Computational Inefficiency**: Comparing records manually or using naive nested loops scales quadratically ($O(N \times M)$), quickly becoming computationally unfeasible.
3. **Double-Processing Risks**: Accidentally re-uploading an identical settlement file can lead to duplicate entries, skewed financial statements, or duplicate customer credits.
4. **Audit and Compliance Gaps**: Without automated, auditable exception records, finance teams struggle to trace why a specific discrepancy occurred or when a settlement batch was processed.

This project solves these challenges by providing an automated, auditable, and idempotent reconciliation engine.

---

## Current Implementation

The current repository represents a focused **Phase 1 core foundation** containing:
- **Core Reconciliation Engine**: In-memory matching logic using a Python dictionary-based hash-map lookup.
- **SQLite Persistence via SQLAlchemy**: Relational persistence for settlement batches and discrepancy exception records.
- **SHA-256 File Idempotency**: Produces a content-based SHA-256 checksum of raw file bytes, combined with a unique database constraint to prevent duplicate processing.
- **Automated Test Suite**: 11 focused unit tests verifying matching logic, persistence, and constraint enforcement.

> **Note on Scope**: The current repository is a local backend core. An operational UI dashboard, FastAPI REST API, and distributed task queues are not part of the current phase and are outlined in [Future Evolution](#future-evolution).

---

## Architecture

The following diagram illustrates the lifecycle of a settlement file through the current reconciliation system:

```text
Partner Settlement File (JSON/CSV)
        |
        v
 SHA-256 Checksum Calculation
        |
        v
 Idempotency Check (Query by Checksum)
        |
        +-----------------------------------+
        | Existing Checksum                 | New Checksum
        v                                   v
 Skip Processing & Return Batch       Create SettlementBatch
                                            |
                                            v
                                 Load Partner Transactions
                                            |
                                            v
 Internal Transactions ------>  Reconciliation Engine (Hash-Map)
                                            |
                         +------------------+------------------+
                         |                                     |
                         v                                     v
                   EXACT_MATCH                        Discrepancies / Exceptions
                (Increment Matched)                            |
                                                               v
                                                      Persist to SQLite
                                                               |
                                          +--------------------+--------------------+
                                          |                                         |
                                          v                                         v
                                   SettlementBatch                       ReconciliationException
                                 (Status: COMPLETED)                      (Status: OPEN)
```

### Component Overview
1. **SHA-256 Checksum Generator**: Reads raw file bytes and computes a 256-bit cryptographic digest.
2. **Idempotency Gate**: Queries the database for existing batches with the computed checksum. If found, processing short-circuits immediately without duplicating data.
3. **Data Ingestion Parser**: Ingests partner transactions from structured files (JSON or CSV).
4. **Reconciliation Engine**: Matches partner transactions against internal ledger records using Python dictionary-based hash-map lookups.
5. **SQLite Persistence Layer**: Updates batch metrics and persists discrepancies into relational tables for operational audit trails.

---

## Reconciliation Logic

### The Hash-Map Approach

A naive reconciliation algorithm compares each partner record against every internal record using nested loops:
$$\text{Time Complexity} = O(N \times M)$$
For 100,000 internal transactions and 100,000 partner transactions, a naive comparison would require up to $10,000,000,000$ operations.

To eliminate this bottleneck, this project implements a Python dictionary-based hash-map lookup:

```text
Internal Transactions List
        |
        v
 Build Dictionary Keyed by transaction_id (O(N))
        |
        v
 Iterate Partner Transactions (M records)
        |
        v
 Dictionary Lookup by transaction_id (Average O(1))
        |
        v
 Compare Amount and Currency
        |
        v
 Discrepancy Classification
```

### Algorithmic Complexity

- **Time Complexity**:
  - Building the internal lookup dictionary: $O(N)$ time, where $N$ is the number of internal transactions.
  - Looking up and matching each partner record: Average-case $O(1)$ dictionary lookup $\times M$ records = $O(M)$ time.
  - Final pass to detect unmatched internal records (`ORPHAN_INTERNAL`): $O(N)$ time.
  - **Total Algorithm Time**: **Approximately $O(N + M)$ average time**, scaling linearly with data volume.
  
  *(Note: We do not claim the entire end-to-end system is strictly $O(N)$, as disk I/O, database writes, and potential hash collisions add overhead. However, the core in-memory comparison runs in near-linear average time).*

- **Space Complexity & Memory Tradeoff**:
  - The hash-map approach requires $O(N)$ auxiliary memory to store internal transactions in memory. For extreme data volumes exceeding system memory, chunking or external database indexing would be used.

---

## Exception Categories

Every transaction evaluated by the engine is assigned one of six deterministic statuses:

| Status / Category | Description | System Action / Persisted Record |
| :--- | :--- | :--- |
| `EXACT_MATCH` | `transaction_id`, `amount`, and `currency` match identically across both internal and partner records. | Increments `matched_records` on the batch. **Not stored** as an exception (normal settlement). |
| `AMOUNT_MISMATCH` | `transaction_id` and `currency` match, but the settled amount differs. | Persisted as `ReconciliationException` with internal and external amounts recorded. |
| `CURRENCY_MISMATCH` | `transaction_id` matches, but the settlement currency differs (e.g., USD vs. EUR). | Persisted as `ReconciliationException` with both currencies recorded. (Amounts cannot be directly compared without FX rates). |
| `ORPHAN_EXTERNAL` | Transaction exists in the partner file but is absent from the internal ledger. | Persisted as `ReconciliationException`. `internal_amount` and `internal_currency` are set to `NULL`. |
| `ORPHAN_INTERNAL` | Transaction exists in the internal ledger but was never reported by the partner. | Persisted as `ReconciliationException`. `external_amount` and `external_currency` are set to `NULL`. |
| `DUPLICATE_TX` | The partner file contains multiple records sharing the same `transaction_id`. | The duplicate record is flagged and persisted as `ReconciliationException` to prevent double-settlement. |

---

## Idempotency

### The Risk of Duplicate Processing
In financial workflows, settlement files are frequently retransmitted due to network timeouts, automated scheduler retries, or human error. Reprocessing a previously settled file without safeguards causes duplicate exception tickets, inaccurate match metrics, and skewed ledger reporting.

### How Idempotency is Enforced

```text
File Bytes (Binary Stream)
         |
         v
  hashlib.sha256()
         |
         v
   file_checksum (Hex Digest)
         |
         v
  SQLite UNIQUE Constraint Check
         |
         +---- Existing Checksum in DB ----> Return Existing Batch (Skip Processing)
         |
         +---- New Checksum in DB ---------> Process File & Persist Batch + Exceptions
```

- **Byte-Level Hashing**: The checksum is calculated by streaming raw file bytes (`rb`) in 64KB chunks. Changing the filename has zero effect on the checksum; modifying a single byte within the file completely alters the hash.
- **Dual-Layer Defense**:
  1. *Application Layer*: `process_settlement_file()` queries `SettlementBatch` by `file_checksum` prior to processing.
  2. *Database Layer*: The `file_checksum` column has a `UNIQUE` constraint, providing a database-level guarantee that duplicate batches with the same checksum cannot both be persisted successfully.

---

## Database Design

Persistence is handled by SQLite via SQLAlchemy declarative models.

```text
+------------------------------------+          +------------------------------------+
|          SettlementBatch           |          |      ReconciliationException       |
+------------------------------------+          +------------------------------------+
| id (PK, Integer)                   | 1      * | id (PK, Integer)                   |
| partner_name (String)              |<-------->| batch_id (FK, Integer)             |
| file_checksum (String, UNIQUE)     |          | transaction_id (String)            |
| total_records (Integer)            |          | exception_type (String)            |
| matched_records (Integer)          |          | internal_amount (Float, Nullable)  |
| exception_records (Integer)        |          | external_amount (Float, Nullable)  |
| status (String)                    |          | internal_currency (String, Null)   |
| created_at (DateTime)              |          | external_currency (String, Null)   |
+------------------------------------+          | status (String, Default: 'OPEN')   |
                                                +------------------------------------+
```

### Models
1. **`SettlementBatch`**:
   - Represents an ingested partner settlement file.
   - Enforces unique `file_checksum` to prevent duplicate ingestion.
   - Tracks aggregate metrics: `total_records`, `matched_records`, and `exception_records`.
2. **`ReconciliationException`**:
   - Represents a specific discrepancy identified during reconciliation.
   - Linked to its parent batch via `batch_id` foreign key.
   - Fields for internal and external amounts/currencies are explicitly nullable, allowing missing records (`ORPHAN_EXTERNAL` and `ORPHAN_INTERNAL`) to store `NULL` where data does not exist.

---

## Project Structure

```text
FinancialReconciliation/
├── .gitignore               # Ignores Python bytecode and cache files
├── database.py              # SQLite engine, Base, and session factory configuration
├── models.py                # SQLAlchemy models (SettlementBatch, ReconciliationException)
├── pytest.ini               # Pytest configuration setting project root on pythonpath
├── reconciler.py            # In-memory reconciliation engine, file hashing, and ingestion
├── requirements.txt         # Core dependencies (SQLAlchemy, pytest)
├── README.md                # Project documentation
└── tests/
    ├── test_database.py     # Tests for persistence, idempotency, and SQLite constraints
    └── test_reconciler.py   # Tests for core reconciliation matching logic
```

---

## Setup

These instructions apply to Windows environments using PowerShell and Conda.

### 1. Activate Environment
Activate your existing Conda environment:
```powershell
conda activate financial-recon
```

### 2. Install Dependencies
Install the required dependencies using pip:
```powershell
python -m pip install -r requirements.txt
```

---

## Running Tests

Execute the automated test suite using `pytest`:

```powershell
pytest -v
```

### Current Test Suite (11 Passing Tests)
- **`tests/test_reconciler.py`** (Core Engine Logic):
  - `test_exact_match`: Identical ID, amount, and currency.
  - `test_amount_mismatch`: Same ID and currency, mismatched amount.
  - `test_orphan_external`: Partner transaction missing internally.
  - `test_currency_mismatch`: Same ID, mismatched currency.
  - `test_orphan_internal`: Internal transaction missing from partner file.
  - `test_duplicate_transaction`: Repeated transaction ID in partner dataset.
- **`tests/test_database.py`** (Persistence & Idempotency):
  - `test_database_tables_creation`: Verifies SQLite schema creation.
  - `test_process_settlement_file_and_persistence`: Verifies batch creation and exception persistence.
  - `test_idempotency_duplicate_file_processing`: Verifies identical files are processed only once.
  - `test_calculate_file_checksum`: Verifies SHA-256 byte hashing accuracy.
  - `test_file_checksum_unique_constraint`: Verifies database-level uniqueness constraint enforcement.

---

## Running the Reconciliation Engine

Execute `reconciler.py` directly to observe a complete end-to-end demonstration:

```powershell
python reconciler.py
```

### What the Script Demonstrates
1. **In-Memory Engine Execution**: Evaluates a mock settlement dataset against an internal transaction ledger and prints a detailed console reconciliation report showing exact matches, amount mismatches, currency mismatches, orphans, and duplicates.
2. **SQLite Persistence & Idempotency Demonstration**: Initializes the local SQLite database (`reconciliation.db`), generates a sample settlement file, computes its SHA-256 checksum, processes and persists the batch, and immediately executes a second pass demonstrating **idempotent skip behavior**.

---

## Design Decisions

- **Python Built-in Structures (`dict`, `set`)**: Used for the core matching engine to keep the code lightweight, easily testable, and beginner-readable while achieving average $O(1)$ lookups.
- **SQLite**: Chosen for zero-configuration, self-contained local persistence ideal for development and isolated unit testing.
- **SQLAlchemy ORM (2.0+)**: Provides clean model definitions, relationships, and migration readiness without writing raw SQL strings. `expire_on_commit=False` is enabled on the sessionmaker to avoid detached instance errors when reading model attributes outside transactions.
- **SHA-256 Checksums**: Standard cryptographic hash algorithm ensuring that any content change produces a completely distinct hash digest.
- **Pytest with `tmp_path`**: All database tests run against temporary, disposable SQLite databases in isolated directories, ensuring the test suite never modifies or depends on the local `reconciliation.db` file.

---

## Current Limitations

The current repository reflects deliberate scope boundaries for Phase 1:
- **In-Memory Matching**: The internal transaction lookup dictionary is held in memory ($O(N)$ space), which is suitable for moderate file sizes but bounded by available RAM.
- **Float Precision**: Monetary amounts currently use standard Python `float` and SQLAlchemy `Float`. While sufficient for Phase 1 demonstration, production financial platforms require arbitrary-precision decimal representations (e.g., Python `Decimal` or integer minor units/cents) to eliminate binary floating-point rounding inaccuracies.
- **Local SQLite Storage**: SQLite does not support high-concurrency write operations.
- **No Background Workers**: File processing executes synchronously within the active process.
- **Concurrency / Race Conditions**: While the database unique constraint prevents duplicate batch insertions, distributed lock mechanisms (e.g., database-level row locks or distributed mutexes) are required in multi-worker environments to prevent simultaneous duplicate processing attempts.

---

## Future Evolution

The architecture is designed to evolve into a production-grade enterprise platform through the following planned enhancements:

1. **FastAPI Ingestion Service**: Expose REST endpoints (`POST /settlements/upload`, `GET /settlements/{batch_id}`, `GET /exceptions`) with schema validation via Pydantic.
2. **Operational Web Dashboard**: Build an interactive frontend interface for finance teams to view settlement summaries, search exceptions, and manually resolve discrepancies.
3. **Monetary Decimal Representation**: Transition all amount fields from `Float` to `Decimal` (stored as fixed-point `NUMERIC(18, 4)` or integer minor units/cents) to guarantee financial precision.
4. **Asynchronous Background Processing**: Offload file parsing and reconciliation to background workers (e.g., Celery or Redis queues) for non-blocking processing of multi-gigabyte settlement files.
5. **Streaming File Processing**: Implement streaming parsers (e.g., chunked CSV readers or generator pipelines) to reconcile files larger than system RAM.
6. **Audit Logs & Exception Workflow**: Add an exception resolution lifecycle (`OPEN` $\to$ `INVESTIGATING` $\to$ `RESOLVED` / `WRITTEN_OFF`) with user audit logging.
7. **PostgreSQL Migration**: Transition from SQLite to PostgreSQL for concurrent multi-tenant production deployments.

---

## Testing Summary

```text
============================= test session starts =============================
platform win32 -- Python 3.13, pytest-8.4.2
collected 11 items

tests/test_database.py::test_database_tables_creation PASSED             [  9%]
tests/test_database.py::test_process_settlement_file_and_persistence PASSED [ 18%]
tests/test_database.py::test_idempotency_duplicate_file_processing PASSED [ 27%]
tests/test_database.py::test_calculate_file_checksum PASSED              [ 36%]
tests/test_database.py::test_file_checksum_unique_constraint PASSED      [ 45%]
tests/test_reconciler.py::test_exact_match PASSED                        [ 54%]
tests/test_reconciler.py::test_amount_mismatch PASSED                    [ 63%]
tests/test_reconciler.py::test_orphan_external PASSED                    [ 72%]
tests/test_reconciler.py::test_currency_mismatch PASSED                  [ 81%]
tests/test_reconciler.py::test_orphan_internal PASSED                    [ 90%]
tests/test_reconciler.py::test_duplicate_transaction PASSED              [100%]

============================= 11 passed in 1.02s ==============================
```

- **11 passing automated tests** verifying core algorithmic matching, edge cases, SQLite persistence, and SHA-256 idempotency.

---

## License

This project is intended as a personal engineering portfolio project.
