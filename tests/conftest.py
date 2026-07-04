"""Shared fixtures."""

from __future__ import annotations

import pytest

from holmes_swarm.api.app import build_app


@pytest.fixture
def app():
    application = build_app(config_path="config/example.yml")
    yield application


@pytest.fixture
def bus(app):
    return app.state.bus


@pytest.fixture
def registry(app):
    return app.state.registry


@pytest.fixture
def consensus(app):
    return app.state.consensus


@pytest.fixture
def svc(app):
    return app.state.investigation_service


@pytest.fixture
def audit(app):
    return app.state.audit
