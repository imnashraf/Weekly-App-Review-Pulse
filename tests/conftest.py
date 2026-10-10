import logging

import pytest


@pytest.fixture(autouse=True)
def restore_root_logging():
    """`main()` installs a stderr handler; don't let it leak into later tests."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
