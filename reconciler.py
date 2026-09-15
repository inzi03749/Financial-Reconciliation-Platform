"""Core Reconciliation Engine (Phase 1).

This module provides:
1. In-memory reconciliation function that matches internal financial
   transactions against partner settlement records (linear average-time lookup).
2. SHA-256 file checksum calculation for idempotent file processing.
3. Settlement file ingestion and persistence into SQLite via SQLAlchemy.

How the Hash-Map Lookup Works:
Instead of comparing each partner transaction against every internal transaction
using nested loops (which would take quadratic time O(N * M)), we build a dictionary
(hash-map) indexed by `transaction_id` from the internal records.

In Python, dictionary lookup is approximately O(1) on average. Looking up M partner
transactions in an N-sized hash-map therefore runs in approximately O(N + M) average time.
Note that we do not claim the entire end-to-end system is strictly O(N); rather, the
core in-memory lookup approach is approximately linear (average-case) due to O(1) dict lookups.
"""

import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from database import get_session, init_db
from models import ReconciliationException, SettlementBatch

# Reconciliation status labels
STATUS_EXACT_MATCH = "EXACT_MATCH"
STATUS_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
STATUS_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
STATUS_ORPHAN_EXTERNAL = "ORPHAN_EXTERNAL"
STATUS_ORPHAN_INTERNAL = "ORPHAN_INTERNAL"
STATUS_DUPLICATE_TX = "DUPLICATE_TX"


# Mock / hardcoded sample datasets for quick demonstration
internal_transactions: List[Dict[str, Any]] = [
    {"transaction_id": "TX_101", "amount": 100.50, "currency": "USD"},
    {"transaction_id": "TX_102", "amount": 250.00, "currency": "USD"},
    {"transaction_id": "TX_103", "amount": 75.25, "currency": "EUR"},
    {"transaction_id": "TX_104", "amount": 500.00, "currency": "USD"},
    {"transaction_id": "TX_105", "amount": 120.00, "currency": "USD"},  # Will be internal orphan
]

partner_transactions: List[Dict[str, Any]] = [
    {"transaction_id": "TX_101", "amount": 100.50, "currency": "USD"},  # EXACT_MATCH
    {"transaction_id": "TX_102", "amount": 240.00, "currency": "USD"},  # AMOUNT_MISMATCH (250 vs 240)
    {"transaction_id": "TX_103", "amount": 75.25, "currency": "GBP"},   # CURRENCY_MISMATCH (EUR vs GBP)
    {"transaction_id": "TX_104", "amount": 500.00, "currency": "USD"},  # EXACT_MATCH
    {"transaction_id": "TX_104", "amount": 500.00, "currency": "USD"},  # DUPLICATE_TX (TX_104 repeated)
    {"transaction_id": "TX_999", "amount": 310.00, "currency": "USD"},  # ORPHAN_EXTERNAL (not in internal)
]


def reconcile(
    internal_transactions: List[Dict[str, Any]],
    partner_transactions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Reconcile internal transactions against partner transactions.

    Steps:
    1. Build an internal lookup dictionary (hash map) indexed by `transaction_id`.
    2. Iterate through partner transactions:
       - Check for duplicate IDs in the partner dataset.
       - Look up the matching internal transaction by ID in ~O(1) average time.
       - If not found internally, mark as ORPHAN_EXTERNAL.
       - If found, compare currency and amount to identify EXACT_MATCH,
         CURRENCY_MISMATCH, or AMOUNT_MISMATCH.
       - Track matched internal transaction IDs.
    3. Iterate through internal transactions to find any unmatched IDs
       and mark them as ORPHAN_INTERNAL.

    Args:
        internal_transactions: List of internal transaction dictionaries.
        partner_transactions: List of partner transaction dictionaries.

    Returns:
        A list of reconciliation result dictionaries.
    """
    results: List[Dict[str, Any]] = []

    # Step 1: Build the internal hash map for ~O(1) average-time lookups
    internal_map: Dict[str, Dict[str, Any]] = {
        tx["transaction_id"]: tx for tx in internal_transactions
    }

    # Tracking sets
    seen_partner_ids = set()
    matched_internal_ids = set()

    # Step 2: Reconcile partner transactions against internal records
    for partner_tx in partner_transactions:
        tx_id = partner_tx["transaction_id"]

        # Check for duplicate transaction IDs within the partner dataset
        if tx_id in seen_partner_ids:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_DUPLICATE_TX,
                "internal_record": internal_map.get(tx_id),
                "partner_record": partner_tx,
                "details": "Duplicate transaction ID found in partner records.",
            })
            continue

        seen_partner_ids.add(tx_id)

        # Check if transaction exists internally
        if tx_id not in internal_map:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_ORPHAN_EXTERNAL,
                "internal_record": None,
                "partner_record": partner_tx,
                "details": "Transaction found in partner records but missing from internal records.",
            })
            continue

        internal_tx = internal_map[tx_id]
        matched_internal_ids.add(tx_id)

        # Compare currency first (amounts cannot be meaningfully compared across different currencies)
        if internal_tx["currency"] != partner_tx["currency"]:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_CURRENCY_MISMATCH,
                "internal_record": internal_tx,
                "partner_record": partner_tx,
                "details": f"Currency mismatch: internal={internal_tx['currency']} vs partner={partner_tx['currency']}.",
            })
        elif internal_tx["amount"] != partner_tx["amount"]:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_AMOUNT_MISMATCH,
                "internal_record": internal_tx,
                "partner_record": partner_tx,
                "details": f"Amount mismatch: internal={internal_tx['amount']} vs partner={partner_tx['amount']}.",
            })
        else:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_EXACT_MATCH,
                "internal_record": internal_tx,
                "partner_record": partner_tx,
                "details": "Transaction matched exactly on ID, amount, and currency.",
            })

    # Step 3: Identify internal transactions that were never matched
    for internal_tx in internal_transactions:
        tx_id = internal_tx["transaction_id"]
        if tx_id not in matched_internal_ids:
            results.append({
                "transaction_id": tx_id,
                "status": STATUS_ORPHAN_INTERNAL,
                "internal_record": internal_tx,
                "partner_record": None,
                "details": "Transaction exists in internal records but was not reported by partner.",
            })

    return results


def calculate_file_checksum(file_path: str) -> str:
    """Calculate the SHA-256 checksum of a file's exact binary contents.

    Reads the file in 64KB chunks to efficiently and accurately hash files of any size.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def load_transactions_from_file(file_path: str) -> List[Dict[str, Any]]:
    """Load partner transactions from a JSON or CSV file into a list of dictionaries."""
    if file_path.lower().endswith(".csv"):
        import csv
        transactions = []
        with open(file_path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                transactions.append({
                    "transaction_id": row["transaction_id"],
                    "amount": float(row["amount"]),
                    "currency": row["currency"],
                })
        return transactions
    else:
        # Default to JSON format
        with open(file_path, mode="r", encoding="utf-8") as f:
            return json.load(f)


def process_settlement_file(
    file_path: str,
    partner_name: str,
    internal_transactions: List[Dict[str, Any]],
    session=None,
) -> Tuple[SettlementBatch, bool]:
    """Process a settlement file idempotently and persist results to SQLite.

    Idempotency Workflow:
    1. Calculate SHA-256 checksum of the file's exact bytes.
    2. Check if a SettlementBatch with this checksum already exists in the database.
    3. If it already exists:
       - Return (existing_batch, True), indicating the file was already processed.
       - Do NOT create another SettlementBatch.
       - Do NOT insert duplicate ReconciliationException records.
    4. If it does not exist:
       - Create a new SettlementBatch (status='PROCESSING').
       - Load partner transactions from the file.
       - Run reconcile(internal_transactions, partner_transactions).
       - Persist all discrepancies (AMOUNT_MISMATCH, CURRENCY_MISMATCH,
         ORPHAN_EXTERNAL, ORPHAN_INTERNAL, DUPLICATE_TX) as ReconciliationExceptions.
       - Successful EXACT_MATCH results do NOT create exception rows.
       - Update batch statistics: total_records, matched_records, exception_records.
       - Mark batch as COMPLETED and commit.
       - Return (batch, False).

    Args:
        file_path: Path to the partner settlement file.
        partner_name: Name of the partner (e.g., 'ACME_PAY').
        internal_transactions: List of internal transaction records.
        session: Optional SQLAlchemy session (defaults to global session).

    Returns:
        A tuple (batch, already_processed).
    """
    checksum = calculate_file_checksum(file_path)

    close_session_when_done = False
    if session is None:
        session = get_session()
        close_session_when_done = True

    try:
        # Step 1: Idempotency check - look for existing batch with identical SHA-256 checksum
        existing_batch = (
            session.query(SettlementBatch)
            .filter_by(file_checksum=checksum)
            .first()
        )
        if existing_batch is not None:
            # File already processed; avoid duplicate batch or exception insertion
            return existing_batch, True

        # Step 2: Load partner transactions from the file
        partner_txs = load_transactions_from_file(file_path)
        total_records = len(partner_txs)

        # Step 3: Create initial SettlementBatch record
        batch = SettlementBatch(
            partner_name=partner_name,
            file_checksum=checksum,
            total_records=total_records,
            matched_records=0,
            exception_records=0,
            status="PROCESSING",
        )
        session.add(batch)
        session.flush()  # Flush so batch.id is populated for foreign key references

        # Step 4: Run core reconciliation engine
        reconciliation_results = reconcile(internal_transactions, partner_txs)

        matched_count = 0
        exception_count = 0

        for item in reconciliation_results:
            status = item["status"]
            if status == STATUS_EXACT_MATCH:
                matched_count += 1
            else:
                # All non-matching statuses create a ReconciliationException record
                exception_count += 1
                internal_rec = item.get("internal_record")
                partner_rec = item.get("partner_record")

                exception_entry = ReconciliationException(
                    batch_id=batch.id,
                    transaction_id=item["transaction_id"],
                    exception_type=status,
                    internal_amount=internal_rec["amount"] if internal_rec else None,
                    external_amount=partner_rec["amount"] if partner_rec else None,
                    internal_currency=internal_rec["currency"] if internal_rec else None,
                    external_currency=partner_rec["currency"] if partner_rec else None,
                    status="OPEN",
                )
                session.add(exception_entry)

        # Step 5: Update batch summary counts and mark COMPLETED
        batch.matched_records = matched_count
        batch.exception_records = exception_count
        batch.status = "COMPLETED"

        session.commit()
        return batch, False

    except Exception:
        session.rollback()
        raise
    finally:
        if close_session_when_done:
            session.close()


def print_reconciliation_summary(results: List[Dict[str, Any]]) -> None:
    """Print a clean, human-readable reconciliation report."""
    print("=" * 70)
    print(" RECONCILIATION REPORT (PHASE 1 ENGINE)")
    print("=" * 70)
    print(f"Total results evaluated: {len(results)}\n")

    for idx, item in enumerate(results, start=1):
        print(f"[{idx}] TX ID: {item['transaction_id']}")
        print(f"    Status : {item['status']}")
        print(f"    Details: {item['details']}")
        if item["internal_record"]:
            print(f"    Internal: {item['internal_record']}")
        if item["partner_record"]:
            print(f"    Partner : {item['partner_record']}")
        print("-" * 70)


if __name__ == "__main__":
    import os
    import tempfile

    # 1. Display Phase 1 in-memory engine results
    reconciliation_results = reconcile(internal_transactions, partner_transactions)
    print_reconciliation_summary(reconciliation_results)

    # 2. Demonstrate Phase 2 SQLite persistence and SHA-256 idempotency
    print("\n" + "=" * 70)
    print(" DEMONSTRATING HOUR 2: SQLITE PERSISTENCE & SHA-256 IDEMPOTENCY")
    print("=" * 70)

    # Initialize SQLite database tables
    init_db()

    # Create a temporary partner settlement JSON file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(partner_transactions, tmp, indent=2)
        temp_file_path = tmp.name

    try:
        checksum = calculate_file_checksum(temp_file_path)
        print(f"Settlement File: {temp_file_path}")
        print(f"File SHA-256 Checksum: {checksum}\n")

        # First run: new file processing
        batch_1, already_processed_1 = process_settlement_file(
            file_path=temp_file_path,
            partner_name="ACME_PAY",
            internal_transactions=internal_transactions,
        )
        print(f"Run 1 -> Batch ID: {batch_1.id}")
        print(f"         Already Processed: {already_processed_1}")
        print(f"         Status: {batch_1.status}")
        print(f"         Total Records: {batch_1.total_records}")
        print(f"         Matched Records: {batch_1.matched_records}")
        print(f"         Exception Records: {batch_1.exception_records}\n")

        # Second run: same file -> Idempotency skips processing
        batch_2, already_processed_2 = process_settlement_file(
            file_path=temp_file_path,
            partner_name="ACME_PAY",
            internal_transactions=internal_transactions,
        )
        print(f"Run 2 -> Batch ID: {batch_2.id}")
        print(f"         Already Processed: {already_processed_2} (IDEMPOTENT - skipped!)")
        print("=" * 70)
    finally:
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)
