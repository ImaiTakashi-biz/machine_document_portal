import pytest
from pydantic import ValidationError

from app.config import Settings


def test_dashboard_revision_poll_defaults_to_300_seconds() -> None:
    settings = Settings(_env_file=None)

    assert settings.dashboard_revision_poll_seconds == 300


def test_dashboard_revision_poll_accepts_zero_to_disable_polling() -> None:
    settings = Settings(_env_file=None, dashboard_revision_poll_seconds=0)

    assert settings.dashboard_revision_poll_seconds == 0


def test_dashboard_revision_poll_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, dashboard_revision_poll_seconds=-1)
