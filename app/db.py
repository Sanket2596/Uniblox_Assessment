from collections.abc import Iterator

from sqlalchemy import create_engine, event  
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker 
from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    is_sqlite = url.startswith("sqlite")
    kwargs: dict = {
        "connect_args": {"check_same_thread": False} if is_sqlite else {},
    }
    if not is_sqlite:
        # Pooled engines (e.g. Postgres): check a connection is alive before handing it out
        # (a dead one raises OperationalError -> 503 + reconnect), and cap the wait for a
        # free connection so pool exhaustion fails fast as a retryable 503 (SQLAlchemy
        # TimeoutError) instead of hanging the request.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_timeout"] = 5
    engine = create_engine(url, **kwargs)
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            # Wait for a writer lock instead of failing fast; concurrent checkouts rely on this.
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db
