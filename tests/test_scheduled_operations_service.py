from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.services.document_search import (
    DocumentCandidateResult,
    DocumentSearchResult,
)
from app.services.nas_drawing_service import NasDrawingService
from app.services.next_day_sheet_service import SheetInfo
from app.services.pdf_print_service import PdfPrintError
from app.services.scheduled_job_state_store import (
    ScheduledJobStateError,
    ScheduledJobStateStore,
)
from app.services.scheduled_operations_service import ScheduledOperationsService


class FakeGateway:
    def __init__(self, part_numbers: list[str]) -> None:
        self.part_numbers = part_numbers

    def list_sheets(self) -> list[SheetInfo]:
        return [SheetInfo(sheet_id=27, title="27S")]

    def fetch_part_numbers(
        self,
        sheet_name: str,
        *,
        target_date: date,
    ) -> list[str]:
        assert sheet_name == "27S"
        assert target_date == date(2026, 7, 27)
        return list(self.part_numbers)


class FakeInspectionService:
    configured = True

    def search_many(self, part_numbers) -> dict[str, DocumentSearchResult]:
        results: dict[str, DocumentSearchResult] = {}
        for part_number in part_numbers:
            if part_number == "AB-100":
                results[part_number] = DocumentSearchResult(
                    status="found",
                    url="https://example.com/inspection",
                )
            else:
                results[part_number] = DocumentSearchResult(
                    status="multiple",
                    candidates=(
                        DocumentCandidateResult(
                            name=f"{part_number}-1.xlsx",
                            url="https://example.com/inspection-1",
                        ),
                        DocumentCandidateResult(
                            name=f"{part_number}-2.xlsx",
                            url="https://example.com/inspection-2",
                        ),
                    ),
                )
        return results


class MissingInspectionService:
    configured = True

    def search_many(self, part_numbers) -> dict[str, DocumentSearchResult]:
        return {
            part_number: DocumentSearchResult(status="not_found")
            for part_number in part_numbers
        }


class FakeAraichatService:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_text(self, message: str, *, idempotency_key: str) -> None:
        self.messages.append((message, idempotency_key))


class FakePrinter:
    def __init__(self, *, fail_once_for: str | None = None) -> None:
        self.printed: list[str] = []
        self.fail_once_for = fail_once_for

    def print_pdf(self, pdf_path: Path) -> None:
        if self.fail_once_for == pdf_path.stem:
            self.fail_once_for = None
            raise PdfPrintError("temporary printer failure")
        self.printed.append(pdf_path.stem)


def make_service(tmp_path, *, part_numbers, printer=None, inspection_service=None):
    settings = Settings(
        google_spreadsheet_id="spreadsheet-id",
        nas_drawing_directory=tmp_path,
        scheduled_job_state_path=tmp_path / "job-state.json",
        araichat_base_url="https://example.com",
        araichat_api_key="api-key",
        araichat_room_id="24",
        sharepoint_process_inspection_url="https://example.com/inspection-folder",
        print_retry_delays_seconds="180,300,600",
    )
    chat = FakeAraichatService()
    selected_printer = printer or FakePrinter()
    state_store = ScheduledJobStateStore(
        settings.scheduled_job_state_path,
        spreadsheet_id=settings.google_spreadsheet_id,
    )
    service = ScheduledOperationsService(
        settings,
        gateway=FakeGateway(part_numbers),
        inspection_service=inspection_service or FakeInspectionService(),
        drawing_service=NasDrawingService(tmp_path),
        araichat_service=chat,
        printer=selected_printer,
        state_store=state_store,
    )
    return service, chat, selected_printer


def test_notification_and_printing_are_not_repeated_on_the_weekend(tmp_path) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")
    service, chat, printer = make_service(
        tmp_path,
        part_numbers=["AB-100", "CD-200"],
    )

    friday_check = service.check_and_notify(date(2026, 7, 24))
    friday_recheck = service.recheck_and_notify(date(2026, 7, 24))
    friday_print = service.print_drawings(date(2026, 7, 24))
    saturday_check = service.check_and_notify(date(2026, 7, 25))
    saturday_recheck = service.recheck_and_notify(date(2026, 7, 25))
    saturday_print = service.print_drawings(date(2026, 7, 25))

    assert friday_check.status == "completed"
    assert friday_recheck.status == "completed"
    assert friday_print.status == "completed"
    assert saturday_check.status == "already_processed"
    assert saturday_recheck.status == "already_processed"
    assert saturday_print.status == "already_processed"
    assert len(chat.messages) == 2
    assert chat.messages[0][0].count("CD-200") == 1
    assert "■ CD-200" in chat.messages[0][0]
    assert "【翌営業日セット予定分の検査シート・加工図面確認通知】" in chat.messages[0][0]
    assert "・加工図面\n  → NASの所定フォルダへ保存してください" in chat.messages[0][0]
    assert "https://example.com/inspection-folder" in chat.messages[0][0]
    assert str(tmp_path) in chat.messages[0][0]
    assert "14:30にもう一度確認します。" in chat.messages[0][0]
    assert "【翌営業日セット予定分の再確認（14:30）】" in chat.messages[1][0]
    assert "アップロード後に手動で発行してください。" in chat.messages[1][0]
    assert chat.messages[0][1].startswith("next-day-check:24:")
    assert chat.messages[1][1].startswith("next-day-recheck:24:")
    assert printer.printed == ["AB-100"]

    state = service.state_store.latest_print_state_for_key(friday_print.target_key)
    assert state is not None
    assert [item.part_number for item in state.manual_items] == ["CD-200"]
    assert service.state_store.latest_print_state(attention_only=True) is None


def test_recheck_does_not_notify_when_missing_drawing_has_been_added(tmp_path) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")
    service, chat, _ = make_service(
        tmp_path,
        part_numbers=["AB-100", "CD-200"],
    )

    initial = service.check_and_notify(date(2026, 7, 24))
    (tmp_path / "CD-200.pdf").write_bytes(b"pdf")
    recheck = service.recheck_and_notify(date(2026, 7, 24))

    assert initial.status == "completed"
    assert recheck.status == "no_missing"
    assert len(chat.messages) == 1


def test_initial_check_does_not_notify_when_nothing_is_missing(tmp_path) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")
    service, chat, _ = make_service(tmp_path, part_numbers=["AB-100"])

    result = service.check_and_notify(date(2026, 7, 24))

    assert result.status == "no_missing"
    assert chat.messages == []


def test_notification_groups_missing_document_types_under_one_part(tmp_path) -> None:
    service, chat, _ = make_service(
        tmp_path,
        part_numbers=["ZM3-5052314A#02"],
        inspection_service=MissingInspectionService(),
    )

    result = service.check_and_notify(date(2026, 7, 24))

    assert result.status == "completed"
    assert len(chat.messages) == 1
    message = chat.messages[0][0]
    assert message.count("■ ZM3-5052314A#02") == 1
    assert "1品番で確認できないものがありました。" in message
    assert "・工程内検査シート" in message
    assert "・加工図面" in message


def test_print_retry_submits_only_parts_not_already_recorded(tmp_path) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")
    (tmp_path / "CD-200.pdf").write_bytes(b"pdf")
    printer = FakePrinter(fail_once_for="CD-200")
    service, _, _ = make_service(
        tmp_path,
        part_numbers=["AB-100", "CD-200"],
        printer=printer,
    )
    service.check_and_notify(date(2026, 7, 24))
    started = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)

    first = service.print_drawings(date(2026, 7, 24), now=started)

    assert service.automatic_print_due(
        date(2026, 7, 24), started + timedelta(seconds=30)
    ) is False
    assert service.automatic_print_due(
        date(2026, 7, 24), started + timedelta(seconds=181)
    ) is True

    result = service.print_drawings(
        date(2026, 7, 24), now=started + timedelta(seconds=181)
    )

    assert first.status == "retry_scheduled"
    assert result.status == "completed"
    assert printer.printed == ["AB-100", "CD-200"]


def test_automatic_printing_stops_and_asks_for_user_action_after_retries(
    tmp_path,
) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")

    class FailingPrinter:
        def print_pdf(self, pdf_path: Path) -> None:
            raise PdfPrintError("printer unavailable")

    service, _, printer = make_service(
        tmp_path,
        part_numbers=["AB-100"],
        printer=FailingPrinter(),
    )
    service.check_and_notify(date(2026, 7, 24))
    started = datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc)

    results = [
        service.print_drawings(
            date(2026, 7, 24),
            now=started + timedelta(minutes=index * 20),
        )
        for index in range(4)
    ]

    assert [result.status for result in results] == [
        "retry_scheduled",
        "retry_scheduled",
        "retry_scheduled",
        "action_required",
    ]
    state = service.state_store.latest_print_state(attention_only=True)
    assert state is not None
    assert state.attention_count == 1
    assert state.retryable_items[0].part_number == "AB-100"


def test_unknown_submission_result_requires_user_confirmation_without_retry(
    tmp_path,
) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")

    class UncertainPrinter:
        def print_pdf(self, pdf_path: Path) -> None:
            raise PdfPrintError("result unknown", may_have_submitted=True)

    service, _, _ = make_service(
        tmp_path,
        part_numbers=["AB-100"],
        printer=UncertainPrinter(),
    )
    service.check_and_notify(date(2026, 7, 24))

    result = service.print_drawings(date(2026, 7, 24))

    assert result.status == "action_required"
    assert service.automatic_print_due(
        date(2026, 7, 24), datetime.now(timezone.utc) + timedelta(days=1)
    ) is False
    state = service.state_store.latest_print_state(attention_only=True)
    assert state is not None
    assert state.uncertain_items[0].part_number == "AB-100"


def test_print_job_is_not_submitted_when_preflight_state_save_fails(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "AB-100.pdf").write_bytes(b"pdf")
    service, _, printer = make_service(tmp_path, part_numbers=["AB-100"])
    service.check_and_notify(date(2026, 7, 24))
    original_mark_print_item = service.state_store.mark_print_item

    def fail_before_submission(target_key, part_number, status, **kwargs) -> None:
        if status == "uncertain":
            raise ScheduledJobStateError("simulated state-storage failure")
        original_mark_print_item(target_key, part_number, status, **kwargs)

    monkeypatch.setattr(
        service.state_store,
        "mark_print_item",
        fail_before_submission,
    )

    with pytest.raises(ScheduledJobStateError):
        service.print_drawings(date(2026, 7, 24))

    assert printer.printed == []
