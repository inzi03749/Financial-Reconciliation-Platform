"""Tests for SQLite persistence, SQLAlchemy models, and SHA-256 idempotency.

These tests use pytest's `tmp_path` fixture to ensure complete test isolation:
no test modifies the production/demo `reconciliation.db`.
"""

import hashlib
import json
import os
import sys
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

# Ensure root directory is on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from database import init_db
from models import ReconciliationException, SettlementBatch
from reconciler import (
    calculate_file_checksum,
    process_settlement_file,
    STATUS_AMOUNT_MISMATCH,
    STATUS_EXACT_MATCH,
    STATUS_ORPHAN_EXTERNAL,
    STATUS_ORPHAN_INTERNAL,
)


@pytest.fixture
def test_db(tmp_path):
    """Fixture providing an isolated SQLite engine and session for a test."""
    db_file = tmp_path / "test_isolated.db"
    engine = create_engine(f"sqlite:///{db_file}", echo=False)
    init_db(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    yield {"engine": engine, "session": session}

    session.close()
    engine.dispose()


def test_database_tables_creation(test_db):
    """Test 1: Verify that SQLite tables are created successfully."""
    engine = test_db["engine"]
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    assert "settlement_batches" in tables
    assert "reconciliation_exceptions" in tables


def test_process_settlement_file_and_persistence(tmp_path, test_db):
    """Test 2: Verify that a settlement file is processed and exceptions are persisted."""
    session = test_db["session"]

    # Sample internal transactions
    internal_data = [
        {"transaction_id": "TX_101", "amount": 100.50, "currency": "USD"},
        {"transaction_id": "TX_102", "amount": 250.00, "currency": "USD"},
        {"transaction_id": "TX_105", "amount": 120.00, "currency": "USD"},  # Internal orphan
    ]

    # Partner transactions (1 exact match, 1 amount mismatch, 1 external orphan)
    partner_data = [
        {"transaction_id": "TX_101", "amount": 100.50, "currency": "USD"},  # EXACT_MATCH
        {"transaction_id": "TX_102", "amount": 240.00, "currency": "USD"},  # AMOUNT_MISMATCH
        {"transaction_id": "TX_999", "amount": 310.00, "currency": "USD"},  # ORPHAN_EXTERNAL
    ]

    # Create a test settlement file in pytest's temporary directory
    file_path = str(tmp_path / "partner_settlement.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(partner_data, f)

    # Process the file
    batch, already_processed = process_settlement_file(
        file_path=file_path,
        partner_name="TEST_PARTNER",
        internal_transactions=internal_data,
        session=session,
    )

    # Assert batch attributes
    assert already_processed is False
    assert batch.status == "COMPLETED"
    assert batch.partner_name == "TEST_PARTNER"
    assert batch.total_records == 3
    assert batch.matched_records == 1  # TX_101
    assert batch.exception_records == 3  # TX_102 (mismatch), TX_999 (ext orphan), TX_105 (int orphan)

    # Verify database contents
    batches_in_db = session.query(SettlementBatch).all()
    assert len(batches_in_db) == 1
    assert batches_in_db[0].id == batch.id

    exceptions_in_db = (
        session.query(ReconciliationException)
        .filter_by(batch_id=batch.id)
        .all()
    )
    assert len(exceptions_in_db) == 3

    # Verify that EXACT_MATCH is NOT saved as an exception
    tx_ids_with_exceptions = [e.transaction_id for e in exceptions_in_db]
    assert "TX_101" not in tx_ids_with_exceptions
    assert "TX_102" in tx_ids_with_exceptions
    assert "TX_999" in tx_ids_with_exceptions
    assert "TX_105" in tx_ids_with_exceptions

    # Check nullable fields on external orphan (TX_999)
    ext_orphan = next(e for e in exceptions_in_db if e.transaction_id == "TX_999")
    assert ext_orphan.exception_type == STATUS_ORPHAN_EXTERNAL
    assert ext_orphan.internal_amount is None
    assert ext_orphan.internal_currency is None
    assert ext_orphan.external_amount == 310.00
    assert ext_orphan.external_currency == "USD"

    # Check nullable fields on internal orphan (TX_105)
    int_orphan = next(e for e in exceptions_in_db if e.transaction_id == "TX_105")
    assert int_orphan.exception_type == STATUS_ORPHAN_INTERNAL
    assert int_orphan.internal_amount == 120.00
    assert int_orphan.internal_currency == "USD"
    assert int_orphan.external_amount is None
    assert int_orphan.external_currency is None


def test_idempotency_duplicate_file_processing(tmp_path, test_db):
    """Test 3 & 4: Processing the exact same file twice does NOT duplicate batches or exceptions."""
    session = test_db["session"]

    internal_data = [
        {"transaction_id": "TX_201", "amount": 50.00, "currency": "USD"},
    ]
    partner_data = [
        {"transaction_id": "TX_201", "amount": 40.00, "currency": "USD"},  # AMOUNT_MISMATCH
    ]

    file_path = str(tmp_path / "settlement_idempotent.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(partner_data, f)

    # First run: processes file and creates batch + exception
    batch_1, already_processed_1 = process_settlement_file(
        file_path=file_path,
        partner_name="PARTNER_B",
        internal_transactions=internal_data,
        session=session,
    )
    assert already_processed_1 is False
    assert batch_1.matched_records == 0
    assert batch_1.exception_records == 1

    batch_count_after_run1 = session.query(SettlementBatch).count()
    exception_count_after_run1 = session.query(ReconciliationException).count()
    assert batch_count_after_run1 == 1
    assert exception_count_after_run1 == 1

    # Second run: exact same file must trigger idempotency
    batch_2, already_processed_2 = process_settlement_file(
        file_path=file_path,
        partner_name="PARTNER_B",
        internal_transactions=internal_data,
        session=session,
    )
    assert already_processed_2 is True
    assert batch_2.id == batch_1.id

    # Verify no second batch or duplicate exceptions were created
    batch_count_after_run2 = session.query(SettlementBatch).count()
    exception_count_after_run2 = session.query(ReconciliationException).count()

    assert batch_count_after_run2 == 1
    assert exception_count_after_run2 == 1


def test_calculate_file_checksum(tmp_path):
    """Verify calculate_file_checksum computes the exact SHA-256 binary hash."""
    file_path = str(tmp_path / "checksum_test.txt")
    raw_content = b"FinancialReconciliationTest12345"

    with open(file_path, "wb") as f:
        f.write(raw_content)

    expected_hash = hashlib.sha256(raw_content).hexdigest()
    actual_hash = calculate_file_checksum(file_path)

    assert actual_hash == expected_hash

    # Modify file and verify hash changes
    with open(file_path, "wb") as f:
        f.write(b"DifferentContent")

    assert calculate_file_checksum(file_path) != expected_hash


def test_file_checksum_unique_constraint(test_db):
    """Verify SQLite database enforces the unique constraint on file_checksum."""
    session = test_db["session"]

    batch_a = SettlementBatch(
        partner_name="PARTNER_X",
        file_checksum="duplicate_checksum_hash_123",
        total_records=1,
        matched_records=1,
        exception_records=0,
        status="COMPLETED",
    )
    session.add(batch_a)
    session.commit()

    # Attempt to insert a second batch with the identical checksum
    batch_b = SettlementBatch(
        partner_name="PARTNER_Y",
        file_checksum="duplicate_checksum_hash_123",
        total_records=1,
        matched_records=1,
        exception_records=0,
        status="COMPLETED",
    )
    session.add(batch_b)

    with pytest.raises(IntegrityError):
        session.commit()

    session.rollback()
