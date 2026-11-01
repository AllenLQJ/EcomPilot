import pytest

from pilot.core.testdb import test_database


@pytest.fixture()
def db_name():
    """每个测试一个独立库（含 seed）。"""
    with test_database() as name:
        yield name
