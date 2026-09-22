import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# The engine is built at import time of `app.db` from `settings.database_url`, and test
# modules import `app.*` at collection time — before any fixture runs. So the override
# must happen here, at conftest import, or the suite silently runs against ./store.db.
_TEST_DB = Path(tempfile.mkdtemp(prefix="store-test-")) / "test.db"
os.environ["STORE_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"


@pytest.fixture(scope="session")
def client():
    from app.db import engine

    assert str(engine.url) == os.environ["STORE_DATABASE_URL"], (
        "tests are not isolated from store.db"
    )

    from app.main import app

    with TestClient(app) as c:
        yield c
