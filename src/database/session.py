"""Database session initialization and connection management."""

from pathlib import Path
from sqlalchemy import create_engine
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

def get_db():
    """Context manager or generator for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
