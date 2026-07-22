import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.dashboard import DocumentCandidate, DocumentState
from app.services.memory_store import MemoryDashboardStore
from app.services.production_service import ProductionService


def test_unsafe_document_url_is_not_exposed() -> None:
    state = DocumentState(status="found", url="javascript:alert(1)")
    assert state.status == "not_checked"
    assert state.url is None


def test_unsafe_candidate_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DocumentCandidate(name="AB-100-1.xlsx", url="javascript:alert(1)")


def test_safe_local_document_url_is_exposed() -> None:
    state = DocumentState(status="found", url="/api/drawings/A-1")
    assert state.status == "found"
    assert state.url == "/api/drawings/A-1"


def test_missing_database_configuration_is_a_friendly_degraded_result() -> None:
    settings = Settings(
        use_sample_data=False,
        persistence_mode="postgresql",
        database_url=None,
    )
    data = ProductionService(settings).get_dashboard(None)
    assert data.degraded is True
    assert data.machines == []
    assert "データベース" in (data.notice or "")


def test_sample_mode_does_not_need_postgresql(tmp_path) -> None:
    settings = Settings(
        use_sample_data=True,
        persistence_mode="memory",
        database_url=None,
        dashboard_snapshot_path=tmp_path / "dashboard.json",
    )
    store = MemoryDashboardStore(settings)
    data = ProductionService(settings, store).get_dashboard(None)
    assert data.degraded is False
    assert len(data.machines) == 61
    assert data.source_label == "メモリ（サンプル）"


def test_memory_mode_without_external_data_does_not_request_database(tmp_path) -> None:
    settings = Settings(
        use_sample_data=False,
        persistence_mode="memory",
        database_url=None,
        dashboard_snapshot_path=tmp_path / "dashboard.json",
    )
    data = ProductionService(settings, MemoryDashboardStore(settings)).get_dashboard(None)
    assert data.degraded is False
    assert data.machines == []
    assert data.source_label == "メモリ"
    assert data.notice is None
