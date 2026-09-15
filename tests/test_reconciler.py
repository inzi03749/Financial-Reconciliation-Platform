"""Unit tests for the Core Reconciliation Engine (Phase 1).

These tests verify the behavior of `reconcile()` across standard and edge-case
settlement scenarios using pytest.
"""

import os
import sys

# Ensure root directory is on Python path so reconciler can be imported directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from reconciler import (
    reconcile,
    STATUS_EXACT_MATCH,
    STATUS_AMOUNT_MISMATCH,
    STATUS_CURRENCY_MISMATCH,
    STATUS_ORPHAN_EXTERNAL,
    STATUS_ORPHAN_INTERNAL,
    STATUS_DUPLICATE_TX,
)


def test_exact_match():
    """Test when internal and partner records have identical ID, amount, and currency."""
    internal = [{"transaction_id": "TX_001", "amount": 100.00, "currency": "USD"}]
    partner = [{"transaction_id": "TX_001", "amount": 100.00, "currency": "USD"}]

    results = reconcile(internal, partner)

    assert len(results) == 1
    assert results[0]["transaction_id"] == "TX_001"
    assert results[0]["status"] == STATUS_EXACT_MATCH
    assert results[0]["internal_record"] == internal[0]
    assert results[0]["partner_record"] == partner[0]


def test_amount_mismatch():
    """Test when transaction ID and currency match, but the amount is different."""
    internal = [{"transaction_id": "TX_002", "amount": 100.00, "currency": "USD"}]
    partner = [{"transaction_id": "TX_002", "amount": 95.00, "currency": "USD"}]

    results = reconcile(internal, partner)

    assert len(results) == 1
    assert results[0]["transaction_id"] == "TX_002"
    assert results[0]["status"] == STATUS_AMOUNT_MISMATCH
    assert results[0]["internal_record"]["amount"] == 100.00
    assert results[0]["partner_record"]["amount"] == 95.00


def test_orphan_external():
    """Test when a partner transaction does not exist in the internal dataset."""
    internal = []
    partner = [{"transaction_id": "TX_EXT_ONLY", "amount": 50.00, "currency": "USD"}]

    results = reconcile(internal, partner)

    assert len(results) == 1
    assert results[0]["transaction_id"] == "TX_EXT_ONLY"
    assert results[0]["status"] == STATUS_ORPHAN_EXTERNAL
    assert results[0]["internal_record"] is None
    assert results[0]["partner_record"] == partner[0]


def test_currency_mismatch():
    """Test when transaction ID matches, but the currency is different."""
    internal = [{"transaction_id": "TX_003", "amount": 50.00, "currency": "USD"}]
    partner = [{"transaction_id": "TX_003", "amount": 50.00, "currency": "EUR"}]

    results = reconcile(internal, partner)

    assert len(results) == 1
    assert results[0]["transaction_id"] == "TX_003"
    assert results[0]["status"] == STATUS_CURRENCY_MISMATCH
    assert results[0]["internal_record"]["currency"] == "USD"
    assert results[0]["partner_record"]["currency"] == "EUR"


def test_orphan_internal():
    """Test when an internal transaction has no corresponding partner record."""
    internal = [{"transaction_id": "TX_INT_ONLY", "amount": 75.00, "currency": "USD"}]
    partner = []

    results = reconcile(internal, partner)

    assert len(results) == 1
    assert results[0]["transaction_id"] == "TX_INT_ONLY"
    assert results[0]["status"] == STATUS_ORPHAN_INTERNAL
    assert results[0]["internal_record"] == internal[0]
    assert results[0]["partner_record"] is None


def test_duplicate_transaction():
    """Test that a repeated transaction ID in the partner dataset is flagged as duplicate."""
    internal = [{"transaction_id": "TX_004", "amount": 200.00, "currency": "USD"}]
    partner = [
        {"transaction_id": "TX_004", "amount": 200.00, "currency": "USD"},
        {"transaction_id": "TX_004", "amount": 200.00, "currency": "USD"},  # Duplicate entry
    ]

    results = reconcile(internal, partner)

    # First one should be EXACT_MATCH, second should be DUPLICATE_TX
    assert len(results) == 2
    assert results[0]["status"] == STATUS_EXACT_MATCH
    assert results[1]["status"] == STATUS_DUPLICATE_TX
    assert results[1]["transaction_id"] == "TX_004"
