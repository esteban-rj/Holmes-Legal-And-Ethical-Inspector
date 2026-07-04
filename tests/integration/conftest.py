"""Conftest for the Cartel de la Cardiología integration tests.

Adds `case_app` (an app pre-wired with the case data + SECOP offline cache)
as a pytest fixture so all integration tests can pull it in by argument.
"""

from __future__ import annotations

import pytest

from ._cartel_helpers import build_case_app


@pytest.fixture()
def case_app():
    return build_case_app()
