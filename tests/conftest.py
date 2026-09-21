import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["STORE_DATABASE_URL"] = f"sqlite:///{db_path}"

    from app.main import app

    with TestClient(app) as c:
        yield c
