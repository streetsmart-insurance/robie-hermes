"""Database session initialization and connection management."""

from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, scoped_session
from src.config import settings
from src.database.models import Base

# Ensure directory for SQLite DB exists if using sqlite
if settings.database_url.startswith("sqlite"):
    db_file = settings.database_url.replace("sqlite:///", "")
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

def init_db():
    """Initializes schema and tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_carrier_voice_attempted_column()


def _ensure_carrier_voice_attempted_column() -> None:
    """Add carrier_voice_attempted on existing SQLite files (create_all won't)."""
    if not settings.database_url.startswith("sqlite"):
        return
    try:
        inspector = inspect(engine)
        if "policy_renewals" not in inspector.get_table_names():
            return
        cols = {col["name"] for col in inspector.get_columns("policy_renewals")}
        if "carrier_voice_attempted" in cols:
            return
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE policy_renewals "
                    "ADD COLUMN carrier_voice_attempted BOOLEAN DEFAULT 0 NOT NULL"
                )
            )
    except Exception:
        return

def get_db():
    """Context manager or generator for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
