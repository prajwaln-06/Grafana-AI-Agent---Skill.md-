"""
app/storage/database.py

Database engine initialization, session lifecycle, table provisioning,
and transparent fallback to local SQLite when PostgreSQL is unavailable.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.storage.models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_SessionFactory: sessionmaker | None = None


def _get_sqlite_fallback_url() -> str:
    data_dir = Path("app_data")
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "rbac.db"
    return f"sqlite:///{db_file.resolve().as_posix()}"


def get_engine() -> Engine:
    """Return the global SQLAlchemy Engine, initializing it on first call."""
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    settings = get_settings()
    configured_url = settings.database_url or "sqlite:///./app_data/rbac.db"

    # Attempt connection with the configured URL
    connect_args = {}
    if configured_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    try:
        candidate_engine = create_engine(
            configured_url,
            connect_args=connect_args,
            pool_pre_ping=True,
        )
        # Test connection probe
        with candidate_engine.connect() as conn:
            conn.execute(select(1))
        _engine = candidate_engine
        logger.info("Connected to database: %s", configured_url.split("@")[-1])
    except (OperationalError, ModuleNotFoundError, ImportError) as exc:
        if not configured_url.startswith("sqlite"):
            fallback_url = _get_sqlite_fallback_url()
            logger.warning(
                "Could not connect to PostgreSQL (%s). Falling back to local SQLite at %s for development.",
                exc,
                fallback_url,
            )
            _engine = create_engine(
                fallback_url,
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
            )
        else:
            raise

    _SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session_factory() -> sessionmaker:
    """Return the global session factory."""
    global _SessionFactory
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Provide a transactional database session."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for obtaining a database session."""
    with get_db_session() as session:
        yield session


def init_db(auto_seed: bool = True) -> None:
    """Create all tables and optionally seed initial dummy data if empty."""
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified.")

    if auto_seed:
        from app.storage.seed import seed_dummy_data_if_empty
        seed_dummy_data_if_empty()

