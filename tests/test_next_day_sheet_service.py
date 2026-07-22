from datetime import date

from app.config import Settings
from app.services.next_day_sheet_service import (
    GoogleSheetsNextDayGateway,
    NextBusinessDaySheetService,
    SheetInfo,
)


def test_finds_next_existing_date_sheet_regardless_of_tab_order() -> None:
    gateway = GoogleSheetsNextDayGateway(
        Settings(),
        metadata_fetcher=lambda: [
            SheetInfo(sheet_id=27, title="27S"),
            SheetInfo(sheet_id=24, title="24S"),
        ],
    )

    target = NextBusinessDaySheetService(gateway).find_target(date(2026, 7, 24))

    assert target is not None
    assert target.target_date == date(2026, 7, 27)
    assert target.sheet_name == "27S"
    assert target.sheet_id == 27


def test_finds_first_day_sheet_after_month_end() -> None:
    gateway = GoogleSheetsNextDayGateway(
        Settings(),
        metadata_fetcher=lambda: [SheetInfo(sheet_id=801, title="1S")],
    )

    target = NextBusinessDaySheetService(gateway).find_target(date(2026, 7, 31))

    assert target is not None
    assert target.target_date == date(2026, 8, 1)
    assert target.sheet_name == "1S"


def test_reads_non_blank_unique_part_numbers_from_current_date_columns() -> None:
    captured: list[str] = []

    def fetch_values(range_name: str):
        captured.append(range_name)
        if range_name.endswith("B36:K36"):
            return [[
                "2026/7/27",
                "2026/7/26",
                "2026/7/27",
                "2026/7/27",
                "2026/7/28",
            ]]
        return [[" AB-100 ", "OLD-100", "CD-200", "AB-100", "CD-200"]]

    gateway = GoogleSheetsNextDayGateway(
        Settings(),
        values_fetcher=fetch_values,
    )

    values = gateway.fetch_part_numbers("27S", target_date=date(2026, 7, 27))

    assert captured == ["'27S'!B36:K36", "'27S'!B40:K40"]
    assert values == [" AB-100 ", "CD-200", "AB-100"]


def test_keeps_parts_when_column_date_is_blank_or_unrecognized(caplog) -> None:
    def fetch_values(range_name: str):
        if range_name.endswith("B36:K36"):
            return [["", "日付未入力", "2026-07-27", "2026年7月28日"]]
        return [["AB-100", "CD-200", "EF-300", "GH-400"]]

    gateway = GoogleSheetsNextDayGateway(Settings(), values_fetcher=fetch_values)

    values = gateway.fetch_part_numbers("27S", target_date=date(2026, 7, 27))

    assert values == ["AB-100", "CD-200", "EF-300", "GH-400"]
    assert "column=C" in caplog.text


def test_includes_later_current_occurrence_when_older_duplicate_is_skipped() -> None:
    def fetch_values(range_name: str):
        if range_name.endswith("B36:K36"):
            return [[date(2026, 7, 26), date(2026, 7, 27)]]
        return [["AB-100", "AB-100"]]

    gateway = GoogleSheetsNextDayGateway(Settings(), values_fetcher=fetch_values)

    values = gateway.fetch_part_numbers("27S", target_date=date(2026, 7, 27))

    assert values == ["AB-100"]
