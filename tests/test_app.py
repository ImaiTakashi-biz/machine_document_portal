from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas.dashboard import DocumentCandidate, DocumentState, MachineCard
from app.services.memory_store import get_memory_store
from app.services.next_day_sheet_service import SheetTarget
from app.services.scheduled_job_state_store import ScheduledJobStateStore


@pytest.fixture(autouse=True)
def use_sample_mode(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("PERSISTENCE_MODE", "memory")
    monkeypatch.setenv("USE_SAMPLE_DATA", "true")
    monkeypatch.setenv("AUTO_REFRESH_SECONDS", "120")
    monkeypatch.setenv("SCHEDULED_JOB_STATE_PATH", str(tmp_path / "job-state.json"))
    get_settings.cache_clear()
    get_memory_store.cache_clear()
    yield
    get_settings.cache_clear()
    get_memory_store.cache_clear()


def test_dashboard_renders_in_sample_mode_without_postgresql() -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "稼働中工程内検査シート" in response.text
    assert "A-1" in response.text
    assert "候補が複数あります" in response.text
    assert "生産中品番なし" in response.text
    assert "メモリ（サンプル）" not in response.text
    assert 'class="sidebar-meta"' not in response.text
    assert "再起動すると状態はリセット" in response.text
    assert response.text.count('class="group-column"') == 6
    assert response.text.count('class="overview-lane"') == 5
    assert response.text.count('class="machine-row ') == 61
    assert 'data-dashboard-revision="' in response.text
    assert 'class="refresh-controls"' in response.text
    assert "工程内検査シート・加工図面を更新するときに押してください。" in response.text
    assert 'class="badge badge-running">稼働中</span>' in response.text
    assert 'class="badge badge-stopped">停止中</span>' in response.text
    assert 'class="badge badge-finished">生産終了</span>' in response.text
    assert 'class="badge badge-setup">セット中</span>' in response.text
    assert "machine-updated-at" not in response.text
    assert 'aria-label="A-1_AX-1200-01_加工図面"' in response.text
    assert 'target="_blank"' in response.text
    assert 'aria-label="全号機一覧"' in response.text
    assert ">測定機器点検表</span>" in response.text
    assert "27737bffefe881aca5aac2e44de8cb2e" in response.text
    assert ">外部リンク</p>" in response.text
    assert response.text.count('class="nav-item nav-item-external"') == 3
    assert response.text.count('class="external-link-mark"') == 3
    assert response.text.count(">検査シート</span>") == 6
    assert response.text.count(">加工図面</span>") == 6
    assert "印刷の確認" not in response.text


def test_corrupt_print_state_does_not_break_dashboard_or_allow_print_actions() -> None:
    settings = get_settings()
    settings.scheduled_job_state_path.write_text("{broken", encoding="utf-8")

    with TestClient(app) as client:
        dashboard_response = client.get("/")
        attention_response = client.get("/api/printing/attention")
        printing_response = client.get("/printing")
        retry_response = client.post("/printing/unknown/retry")

    assert dashboard_response.status_code == 200
    assert "印刷の確認" not in dashboard_response.text
    assert attention_response.json() == {"required": False, "count": 0}
    assert printing_response.status_code == 503
    assert retry_response.status_code == 503


def test_manual_refresh_endpoint() -> None:
    with TestClient(app) as client:
        response = client.post("/api/refresh")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_dashboard_revision_endpoint_tracks_completed_dashboard_updates() -> None:
    with TestClient(app) as client:
        before = client.get("/api/dashboard/revision")
        current_dashboard = get_memory_store().get_dashboard()
        get_memory_store().replace_dashboard(current_dashboard.machines)
        after = client.get("/api/dashboard/revision")

    assert before.status_code == 200
    assert after.status_code == 200
    assert after.json()["updated_at"] is not None
    assert after.json()["updated_at"] != before.json()["updated_at"]


def test_drawing_viewer_opens_as_a_separate_page() -> None:
    with TestClient(app) as client:
        response = client.get("/drawings/A-1")
    assert response.status_code == 200
    assert "A-1_AX-1200-01_加工図面" in response.text
    assert 'data-drawing-viewer-image' in response.text
    assert 'src="/api/drawings/A-1/preview"' in response.text


def test_multiple_inspection_files_are_listed_on_a_separate_page() -> None:
    inspection = DocumentState(
        status="found",
        url="/inspections/E-4",
        candidates=(
            DocumentCandidate(
                name="T798129-1.xlsx",
                url="https://example.com/inspection-1",
                location="Vendor A",
            ),
            DocumentCandidate(
                name="T798129-2.xlsx",
                url="https://example.com/inspection-2",
                location="Vendor B",
            ),
        ),
    )
    with TestClient(app) as client:
        get_memory_store().replace_dashboard(
            [
                MachineCard(
                    machine_id="E-4",
                    group_name="E",
                    machine_number=4,
                    part_number="T798129",
                    inspection=inspection,
                )
            ]
        )
        dashboard_response = client.get("/")
        selection_response = client.get("/inspections/E-4")

    assert dashboard_response.status_code == 200
    assert 'href="/inspections/E-4"' in dashboard_response.text
    assert 'class="machine-doc-count"' in dashboard_response.text
    assert selection_response.status_code == 200
    assert "T798129-1.xlsx" in selection_response.text
    assert "T798129-2.xlsx" in selection_response.text
    assert "Vendor A" in selection_response.text
    assert "Vendor B" in selection_response.text
    assert selection_response.text.count('target="_blank"') >= 2


def test_print_attention_appears_only_in_sidebar_and_opens_simple_page() -> None:
    settings = get_settings()
    store = ScheduledJobStateStore(
        settings.scheduled_job_state_path,
        spreadsheet_id="spreadsheet-id",
    )
    target = SheetTarget(
        target_date=date(2026, 7, 23),
        sheet_id=27,
        sheet_name="23S",
    )
    target_key = store.record_daily_target(date(2026, 7, 22), target)
    store.register_print_items(target_key, ["AB-100", "CD-200"])
    store.mark_print_item(target_key, "AB-100", "submitted")
    store.mark_print_item(target_key, "CD-200", "failed")
    store.mark_manual_print_incomplete(target_key, error="printer unavailable")

    with TestClient(app) as client:
        dashboard_response = client.get("/")
        printing_response = client.get("/printing")
        attention_response = client.get("/api/printing/attention")

    assert dashboard_response.status_code == 200
    assert 'href="/printing"' in dashboard_response.text
    assert ">印刷の確認</span>" in dashboard_response.text
    assert 'class="print-attention-count"' in dashboard_response.text
    assert printing_response.status_code == 200
    assert "一部の加工図を印刷できませんでした" in printing_response.text
    assert "CD-200" in printing_response.text
    assert "AB-100" not in printing_response.text
    assert "未印刷分を印刷する" in printing_response.text
    assert "printer unavailable" not in printing_response.text
    assert attention_response.json() == {"required": True, "count": 1}


def test_printing_page_allows_a_simple_retry_when_contents_could_not_be_read() -> None:
    settings = get_settings()
    store = ScheduledJobStateStore(
        settings.scheduled_job_state_path,
        spreadsheet_id="spreadsheet-id",
    )
    target = SheetTarget(
        target_date=date(2026, 7, 23),
        sheet_id=27,
        sheet_name="23S",
    )
    target_key = store.record_daily_target(date(2026, 7, 22), target)
    store.mark_manual_print_incomplete(target_key, error="technical detail")

    with TestClient(app) as client:
        response = client.get("/printing")

    assert response.status_code == 200
    assert "明日の印刷内容を確認できませんでした" in response.text
    assert "もう一度確認する" in response.text
    assert "technical detail" not in response.text


def test_drawing_missing_at_cutoff_does_not_show_sidebar_attention() -> None:
    settings = get_settings()
    store = ScheduledJobStateStore(
        settings.scheduled_job_state_path,
        spreadsheet_id="spreadsheet-id",
    )
    target = SheetTarget(
        target_date=date(2026, 7, 23),
        sheet_id=27,
        sheet_name="23S",
    )
    target_key = store.record_daily_target(date(2026, 7, 22), target)
    store.register_print_items(target_key, ["AB-100"])
    store.mark_print_item(target_key, "AB-100", "manual_required")
    store.mark_printing_completed(target_key)

    with TestClient(app) as client:
        dashboard_response = client.get("/")
        printing_response = client.get("/printing")
        attention_response = client.get("/api/printing/attention")

    assert dashboard_response.status_code == 200
    assert "印刷の確認" not in dashboard_response.text
    assert printing_response.status_code == 200
    assert "自動印刷の処理は完了しました" in printing_response.text
    assert "保存後に手動で発行してください" in printing_response.text
    assert "AB-100" in printing_response.text
    assert attention_response.json() == {"required": False, "count": 0}
