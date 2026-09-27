"""Fixtures shared across test modules."""

import pytest


@pytest.fixture
def site(tmp_path):
    """A web UI service over a small campus in tmp_path, with a fake job runner."""
    pytest.importorskip("flask")
    from .webhelpers import make_site
    return make_site(tmp_path)
