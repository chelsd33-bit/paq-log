import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nolassist.logging_setup import setup_logging  # noqa: E402
from nolassist.mock_server import MockServer  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _logging():
    setup_logging()


@pytest.fixture(scope="session")
def server():
    srv = MockServer().start()
    yield srv
    srv.stop()
