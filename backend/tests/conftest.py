from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_TMP = tempfile.mkdtemp(prefix="tridock-test-")
os.environ["TRIDOCK_DATA"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app.db import DB_PATH, connect  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture()
def client():
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    seed_if_empty()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
