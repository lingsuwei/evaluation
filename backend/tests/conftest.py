"""Shared test fixtures and utilities."""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")


# Create mock async_session for all tests
mock_session_maker = MagicMock()
mock_session = MagicMock()
mock_session.__aenter__ = AsyncMock(return_value=mock_session)
mock_session.__aexit__ = AsyncMock(return_value=None)
mock_result = MagicMock()
mock_result.scalars.return_value = []
mock_session.execute = AsyncMock(return_value=mock_result)
mock_session_maker.return_value = mock_session


@pytest.fixture(autouse=True, scope="session")
def mock_database_async_session():
    """Mock database.async_session for all tests to avoid DB connections."""
    # Patch the database module before it gets imported
    with patch("database.async_session", mock_session_maker):
        yield