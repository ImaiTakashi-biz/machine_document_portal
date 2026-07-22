from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.config import Settings


logger = logging.getLogger(__name__)


class NextDaySheetError(RuntimeError):
    """The next-business-day sheet could not be inspected."""


@dataclass(frozen=True, slots=True)
class SheetInfo:
    sheet_id: int
    title: str


@dataclass(frozen=True, slots=True)
class SheetTarget:
    target_date: date
    sheet_id: int
    sheet_name: str


class NextDaySheetGateway(Protocol):
    def list_sheets(self) -> list[SheetInfo]: ...

    def fetch_part_numbers(
        self,
        sheet_name: str,
        *,
        target_date: date,
    ) -> list[str]: ...


MetadataFetcher = Callable[[], Sequence[SheetInfo]]
ValuesFetcher = Callable[[str], Sequence[Sequence[object]]]


class GoogleSheetsNextDayGateway:
    """Read column dates and part numbers using the production service account."""

    _SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

    def __init__(
        self,
        settings: Settings,
        *,
        metadata_fetcher: MetadataFetcher | None = None,
        values_fetcher: ValuesFetcher | None = None,
    ) -> None:
        self.settings = settings
        self._metadata_fetcher = metadata_fetcher
        self._values_fetcher = values_fetcher

    def list_sheets(self) -> list[SheetInfo]:
        if self._metadata_fetcher is not None:
            return list(self._metadata_fetcher())
        service, spreadsheet_id = self._service()
        try:
            response = (
                service.spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets.properties(sheetId,title)",
                )
                .execute()
            )
        except Exception as exc:
            raise NextDaySheetError("Google Sheets metadata request failed") from exc
        sheets: list[SheetInfo] = []
        for item in response.get("sheets", []):
            properties = item.get("properties", {}) if isinstance(item, dict) else {}
            sheet_id = properties.get("sheetId")
            title = properties.get("title")
            if isinstance(sheet_id, int) and isinstance(title, str):
                sheets.append(SheetInfo(sheet_id=sheet_id, title=title))
        return sheets

    def fetch_part_numbers(
        self,
        sheet_name: str,
        *,
        target_date: date,
    ) -> list[str]:
        escaped_name = sheet_name.replace("'", "''")
        date_range_name = f"'{escaped_name}'!B36:K36"
        part_range_name = f"'{escaped_name}'!B40:K40"
        date_rows = self._fetch_values(date_range_name)
        part_rows = self._fetch_values(part_range_name)

        column_dates = date_rows[0] if date_rows else []
        values = part_rows[0] if part_rows else []
        part_numbers: list[str] = []
        seen: set[str] = set()
        for column_index, raw_value in enumerate(values[:10]):
            value = str(raw_value)
            if not value.strip():
                continue

            raw_column_date = (
                column_dates[column_index]
                if column_index < len(column_dates)
                else ""
            )
            column_date = self._parse_column_date(raw_column_date)
            if column_date is not None and column_date < target_date:
                continue
            if raw_column_date not in (None, "") and column_date is None:
                logger.warning(
                    "Next-day sheet column date could not be interpreted; "
                    "keeping the part in scope: sheet=%s column=%s value=%r",
                    sheet_name,
                    self._column_name(column_index),
                    raw_column_date,
                )

            if value in seen:
                continue
            seen.add(value)
            part_numbers.append(value)
        return part_numbers

    def _fetch_values(self, range_name: str) -> Sequence[Sequence[object]]:
        if self._values_fetcher is not None:
            return self._values_fetcher(range_name)

        service, spreadsheet_id = self._service()
        try:
            response = (
                service.spreadsheets()
                .values()
                .get(
                    spreadsheetId=spreadsheet_id,
                    range=range_name,
                    majorDimension="ROWS",
                    valueRenderOption="FORMATTED_VALUE",
                )
                .execute()
            )
        except Exception as exc:
            raise NextDaySheetError("Google Sheets values request failed") from exc
        return response.get("values", [])

    @staticmethod
    def _parse_column_date(raw_value: object) -> date | None:
        if isinstance(raw_value, datetime):
            return raw_value.date()
        if isinstance(raw_value, date):
            return raw_value

        value = str(raw_value).strip()
        if not value:
            return None
        for date_format in ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日"):
            try:
                return datetime.strptime(value, date_format).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _column_name(column_index: int) -> str:
        return chr(ord("B") + column_index)

    def _service(self):
        spreadsheet_id = (self.settings.google_spreadsheet_id or "").strip()
        credentials_path = self.settings.google_credentials_path
        if not spreadsheet_id:
            raise NextDaySheetError("GOOGLE_SPREADSHEET_ID is not configured")
        if not credentials_path or not Path(credentials_path).is_file():
            raise NextDaySheetError("GOOGLE_CREDENTIALS_PATH is not readable")
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build

            credentials = Credentials.from_service_account_file(
                credentials_path, scopes=[self._SCOPE]
            )
            return (
                build("sheets", "v4", credentials=credentials, cache_discovery=False),
                spreadsheet_id,
            )
        except Exception as exc:
            raise NextDaySheetError("Google Sheets client could not be created") from exc


class NextBusinessDaySheetService:
    def __init__(
        self,
        gateway: NextDaySheetGateway,
        *,
        lookahead_days: int = 31,
    ) -> None:
        self.gateway = gateway
        self.lookahead_days = lookahead_days

    def find_target(self, today: date) -> SheetTarget | None:
        sheets_by_title = {sheet.title: sheet for sheet in self.gateway.list_sheets()}
        for offset in range(1, self.lookahead_days + 1):
            candidate_date = today + timedelta(days=offset)
            candidate_name = f"{candidate_date.day}S"
            sheet = sheets_by_title.get(candidate_name)
            if sheet is not None:
                return SheetTarget(
                    target_date=candidate_date,
                    sheet_id=sheet.sheet_id,
                    sheet_name=sheet.title,
                )
        return None
