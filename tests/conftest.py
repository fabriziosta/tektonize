"""Shared test fixtures for tektonize tests."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_LIB_DIR = FIXTURES_DIR / "sample_lib"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_lib_dir() -> Path:
    return SAMPLE_LIB_DIR


@pytest.fixture
def groovy_source_dir() -> Path:
    return SAMPLE_LIB_DIR
