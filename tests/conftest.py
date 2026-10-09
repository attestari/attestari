"""Suite-wide check: every cached projection equals a fresh full fold.

The projection backends fold only the events appended since their last read
(ProjectionCache). In tests, each projection the cache returns is also compared
with a full fold of the same log by a new Projector, field by field and in
order, so every test exercises the cache as well as what it is testing. A test
that counts store reads can opt out with `@pytest.mark.no_projection_crosscheck`.
"""

from __future__ import annotations

import pytest

from attestari.backend import ProjectionCache
from attestari.projection import Projection, Projector
from attestari.store import LOG_START


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "no_projection_crosscheck: don't compare cached projections with a full fold",
    )


def assert_same_projection(got: Projection, want: Projection) -> None:
    assert list(got.episodes.items()) == list(want.episodes.items())
    assert list(got.edges.items()) == list(want.edges.items())
    assert list(got.entities.items()) == list(want.entities.items())
    assert got.alias_of == want.alias_of
    assert got.forgotten == want.forgotten


@pytest.fixture(autouse=True)
def _cross_check_projection_cache(request: pytest.FixtureRequest, monkeypatch) -> None:
    if request.node.get_closest_marker("no_projection_crosscheck"):
        return
    current = ProjectionCache.current

    def checked(self: ProjectionCache) -> Projection:
        with self._lock:
            got = current(self)
            position = self._position
            full = getattr(self.store, "changes_since", None)
            full = full(LOG_START) if full is not None else None
        # Another connection may have appended in between; then there is no
        # single log to compare against, so skip this one.
        if full is not None and full[0] == position:
            assert_same_projection(got, Projector(self.projector.embedder).build(full[1]))
        return got

    monkeypatch.setattr(ProjectionCache, "current", checked)
