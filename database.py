"""Database setup and session management for SQLite with SQLAlchemy.

Provides the engine, declarative Base, and session factory for persisting
settlement batches and reconciliation exceptions.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_FILE = "reconciliation.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DATABASE_FILE}"

# SQLAlchemy declarative Base for all models
Base = declarative_base()

# Default engine and sessionmaker for reconciliation.db
# expire_on_commit=False ensures objects remain readable after session commits
engine = create_engine(DEFAULT_DATABASE_URL, echo=False)
SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine_instance=None):
    """Create all database tables for the given engine (or the default engine)."""
    target_engine = engine_instance or engine
    Base.metadata.create_all(bind=target_engine)


def get_session(engine_instance=None):
    """Return a new SQLAlchemy session.

    If an engine_instance is supplied, creates a session bound to that engine;
    otherwise uses the default SessionFactory.
    """
    if engine_instance:
        custom_factory = sessionmaker(bind=engine_instance, expire_on_commit=False)
        return custom_factory()
    return SessionFactory()
