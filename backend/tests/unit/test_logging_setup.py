"""LOG_LEVEL must also apply to uvicorn's loggers, which uvicorn sets to its own level."""

import logging

import pytest

from dmf.logging_setup import setup_logging


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved_uv = {n: (logging.getLogger(n).handlers[:], logging.getLogger(n).level, logging.getLogger(n).propagate) for n in names}
    yield
    root.handlers[:], level = saved
    root.setLevel(level)
    for n, (h, lv, prop) in saved_uv.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = h
        lg.setLevel(lv)
        lg.propagate = prop


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize(("level", "access_info_shown"), [("WARNING", False), ("INFO", True)])
def test_uvicorn_access_log_follows_log_level(level, access_info_shown):
    access = logging.getLogger("uvicorn.access")
    access.setLevel(logging.INFO)  # what uvicorn does with its default --log-level info
    access.addHandler(logging.NullHandler())
    setup_logging(level, "text")
    assert access.isEnabledFor(logging.INFO) is access_info_shown
    assert access.handlers == []  # records go through our formatter only
    assert access.propagate is True
    assert logging.getLogger("uvicorn.error").isEnabledFor(logging.WARNING)
