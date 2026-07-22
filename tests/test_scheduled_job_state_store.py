from datetime import date

from app.services.next_day_sheet_service import SheetTarget
from app.services.scheduled_job_state_store import ScheduledJobStateStore


def test_print_attention_survives_a_new_store_instance(tmp_path) -> None:
    path = tmp_path / "scheduled-state.json"
    first = ScheduledJobStateStore(path, spreadsheet_id="spreadsheet-id")
    target = SheetTarget(
        target_date=date(2026, 7, 27),
        sheet_id=27,
        sheet_name="27S",
    )
    target_key = first.record_daily_target(date(2026, 7, 24), target)
    first.register_print_items(target_key, ["AB-100", "CD-200"])
    first.mark_print_item(target_key, "AB-100", "submitted")
    first.mark_print_item(target_key, "CD-200", "failed")
    first.mark_manual_print_incomplete(target_key, error="printer unavailable")

    restored = ScheduledJobStateStore(path, spreadsheet_id="spreadsheet-id")
    state = restored.latest_print_state(attention_only=True)

    assert state is not None
    assert state.target.sheet_name == "27S"
    assert state.attention_count == 1
    assert [item.part_number for item in state.retryable_items] == ["CD-200"]


def test_user_confirmation_completes_an_uncertain_print(tmp_path) -> None:
    store = ScheduledJobStateStore(
        tmp_path / "scheduled-state.json",
        spreadsheet_id="spreadsheet-id",
    )
    target = SheetTarget(
        target_date=date(2026, 7, 27),
        sheet_id=27,
        sheet_name="27S",
    )
    target_key = store.record_daily_target(date(2026, 7, 24), target)
    store.register_print_items(target_key, ["AB-100"])
    store.mark_print_item(target_key, "AB-100", "uncertain")
    store.mark_manual_print_incomplete(target_key, error="result unknown")

    store.confirm_part_printed(target_key, "AB-100")

    assert store.printing_completed(target_key) is True
    assert store.latest_print_state(attention_only=True) is None
