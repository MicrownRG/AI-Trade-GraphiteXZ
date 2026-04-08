"""
Database connection management using SQLAlchemy sync engine.
Async engine available for FastAPI-based monitoring dashboards.
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from config.settings import DB_URL, DB_ECHO
from database.models import Base
from utils.logger import get_logger

logger = get_logger(__name__)

# Convert asyncpg URL to sync psycopg2 for synchronous operations
_sync_url = DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://")

engine = create_engine(
    _sync_url,
    echo=DB_ECHO,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    """Create all tables if they don't exist."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables initialised")
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        raise


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager for database sessions with auto-rollback on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"DB session error: {e}")
        raise
    finally:
        session.close()
