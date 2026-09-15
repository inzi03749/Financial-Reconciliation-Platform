"""SQLAlchemy models for settlement batches and reconciliation exceptions."""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from database import Base


class SettlementBatch(Base):
    """Represents a settlement file uploaded or processed from a partner."""

    __tablename__ = "settlement_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    partner_name = Column(String, nullable=False)
    file_checksum = Column(String, unique=True, index=True, nullable=False)
    total_records = Column(Integer, default=0, nullable=False)
    matched_records = Column(Integer, default=0, nullable=False)
    exception_records = Column(Integer, default=0, nullable=False)
    status = Column(String, default="PROCESSING", nullable=False)
    created_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # One-to-many relationship: one batch has many reconciliation exceptions
    exceptions = relationship(
        "ReconciliationException",
        back_populates="batch",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<SettlementBatch(id={self.id}, partner='{self.partner_name}', "
            f"checksum='{self.file_checksum[:8]}...', status='{self.status}')>"
        )


class ReconciliationException(Base):
    """Represents a discrepancy/exception identified during reconciliation."""

    __tablename__ = "reconciliation_exceptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("settlement_batches.id"), nullable=False)
    transaction_id = Column(String, nullable=False)
    exception_type = Column(String, nullable=False)
    internal_amount = Column(Float, nullable=True)
    external_amount = Column(Float, nullable=True)
    internal_currency = Column(String, nullable=True)
    external_currency = Column(String, nullable=True)
    status = Column(String, default="OPEN", nullable=False)

    # Relationship back to parent settlement batch
    batch = relationship("SettlementBatch", back_populates="exceptions")

    def __repr__(self) -> str:
        return (
            f"<ReconciliationException(id={self.id}, tx_id='{self.transaction_id}', "
            f"type='{self.exception_type}', status='{self.status}')>"
        )
