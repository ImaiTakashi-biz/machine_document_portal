from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.config import Settings
from app.services.araichat_service import AraichatAmbiguousError, AraichatService
from app.services.nas_drawing_service import NasDrawingAccessError, NasDrawingService
from app.services.next_day_notification import build_next_day_notification
from app.services.next_day_sheet_service import (
    GoogleSheetsNextDayGateway,
    NextBusinessDaySheetService,
    NextDaySheetGateway,
    SheetTarget,
)
from app.services.pdf_print_service import PdfPrinter, RawPdfPrinter
from app.services.scheduled_job_state_store import (
    NotificationPhase,
    ScheduledJobStateStore,
)
from app.services.sharepoint_service import SharePointService


logger = logging.getLogger(__name__)
_EXTERNAL_ERROR_STATUSES = {
    "auth_error",
    "permission_error",
    "api_error",
    "not_checked",
}


class ScheduledOperationError(RuntimeError):
    """A scheduled check or print operation could not complete safely."""


@dataclass(frozen=True, slots=True)
class ScheduledOperationResult:
    status: str
    message: str
    target: SheetTarget | None = None
    processed_count: int = 0
    target_key: str | None = None


class ScheduledOperationsService:
    def __init__(
        self,
        settings: Settings,
        *,
        gateway: NextDaySheetGateway | None = None,
        inspection_service: SharePointService | None = None,
        drawing_service: NasDrawingService | None = None,
        araichat_service: AraichatService | None = None,
        printer: PdfPrinter | None = None,
        state_store: ScheduledJobStateStore | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway or GoogleSheetsNextDayGateway(settings)
        self.target_service = NextBusinessDaySheetService(
            self.gateway,
            lookahead_days=settings.next_day_sheet_lookahead_days,
        )
        self.inspection_service = inspection_service or SharePointService(settings)
        self.drawing_service = drawing_service or NasDrawingService(
            settings.nas_drawing_directory
        )
        self.araichat_service = araichat_service or AraichatService(settings)
        self.printer = printer or RawPdfPrinter(settings.drawing_printer_name)
        self.state_store = state_store or ScheduledJobStateStore(
            settings.scheduled_job_state_path,
            spreadsheet_id=settings.google_spreadsheet_id,
        )

    def check_and_notify(self, run_date: date) -> ScheduledOperationResult:
        target = self.target_service.find_target(run_date)
        if target is None:
            return ScheduledOperationResult(
                status="no_target",
                message="翌営業日のセット情報シートがないため通知をスキップしました。",
            )

        target_key = self.state_store.record_daily_target(run_date, target)
        return self._check_target_and_notify(
            target_key,
            target,
            phase="initial",
        )

    def recheck_and_notify(self, run_date: date) -> ScheduledOperationResult:
        stored_target = self.state_store.target_for_run_date(run_date)
        if stored_target is None:
            return ScheduledOperationResult(
                status="no_target",
                message="13:00に対象シートが決定されていないため再確認をスキップしました。",
            )
        target_key, target = stored_target
        return self._check_target_and_notify(
            target_key,
            target,
            phase="recheck",
        )

    def _check_target_and_notify(
        self,
        target_key: str,
        target: SheetTarget,
        *,
        phase: NotificationPhase,
    ) -> ScheduledOperationResult:
        notification_status = self.state_store.notification_status(
            target_key,
            phase=phase,
        )
        if notification_status == "ambiguous":
            return ScheduledOperationResult(
                status="ambiguous",
                message=(
                    f"{target.sheet_name} はARAICHAT送信結果が不明なため、"
                    "二重送信防止で停止しています。"
                ),
                target=target,
                target_key=target_key,
            )
        if notification_status == "completed":
            label = "再確認通知" if phase == "recheck" else "通知"
            return ScheduledOperationResult(
                status="already_processed",
                message=f"{target.sheet_name} は{label}済みのためスキップしました。",
                target=target,
                target_key=target_key,
            )

        part_numbers = self.gateway.fetch_part_numbers(
            target.sheet_name,
            target_date=target.target_date,
        )
        if not self.inspection_service.configured:
            raise ScheduledOperationError("SharePoint settings are incomplete")
        inspection_results = self.inspection_service.search_many(part_numbers)
        inspection_errors = {
            result.status
            for result in inspection_results.values()
            if result.status in _EXTERNAL_ERROR_STATUSES
        }
        if inspection_errors:
            raise ScheduledOperationError(
                "SharePoint inspection-sheet lookup failed: "
                + ",".join(sorted(inspection_errors))
            )

        missing_inspections = [
            part_number
            for part_number in part_numbers
            if inspection_results.get(part_number) is None
            or inspection_results[part_number].status == "not_found"
        ]
        missing_drawings: list[str] = []
        self._ensure_drawing_directory_available()
        try:
            for part_number in part_numbers:
                if self.drawing_service.find_pdf(part_number) is None:
                    missing_drawings.append(part_number)
        except NasDrawingAccessError as exc:
            raise ScheduledOperationError("NAS drawing lookup failed") from exc

        if not missing_inspections and not missing_drawings:
            self.state_store.mark_notification(
                target_key,
                "completed",
                phase=phase,
            )
            return ScheduledOperationResult(
                status="no_missing",
                message=f"{target.sheet_name} に確認が必要な資料はありません。",
                target=target,
                processed_count=len(part_numbers),
                target_key=target_key,
            )

        message = build_next_day_notification(
            target,
            phase=phase,
            missing_inspections=missing_inspections,
            missing_drawings=missing_drawings,
            sharepoint_location=self.settings.sharepoint_process_inspection_url,
            drawing_location=self.settings.nas_drawing_directory,
        )
        operation_name = (
            "next-day-recheck" if phase == "recheck" else "next-day-check"
        )
        idempotency_key = (
            f"{operation_name}:{self.settings.araichat_room_id}:"
            f"{target.target_date.isoformat()}:{target.sheet_id}"
        )
        try:
            self.araichat_service.send_text(message, idempotency_key=idempotency_key)
        except AraichatAmbiguousError:
            self.state_store.mark_notification(
                target_key,
                "ambiguous",
                phase=phase,
            )
            raise
        self.state_store.mark_notification(
            target_key,
            "completed",
            phase=phase,
        )
        label = "再確認結果" if phase == "recheck" else "確認結果"
        return ScheduledOperationResult(
            status="completed",
            message=f"{target.sheet_name} の{label}をARAICHATへ送信しました。",
            target=target,
            processed_count=len(part_numbers),
            target_key=target_key,
        )

    def automatic_print_due(self, run_date: date, now: datetime) -> bool:
        stored_target = self.state_store.target_for_run_date(run_date)
        if stored_target is None:
            return True
        target_key, _ = stored_target
        return self.state_store.automatic_print_due(target_key, now)

    def print_drawings(
        self,
        run_date: date,
        *,
        automatic: bool = True,
        now: datetime | None = None,
    ) -> ScheduledOperationResult:
        stored_target = self.state_store.target_for_run_date(run_date)
        if stored_target is None:
            return ScheduledOperationResult(
                status="no_target",
                message="13:00に対象シートが決定されていないため印刷をスキップしました。",
            )
        target_key, target = stored_target
        return self._print_target(
            target_key,
            target,
            automatic=automatic,
            now=now or datetime.now(timezone.utc),
            requested_by="automatic" if automatic else "user",
        )

    def retry_printing(
        self,
        target_key: str,
        *,
        part_number: str | None = None,
        requested_by: str = "user",
    ) -> ScheduledOperationResult:
        target = self.state_store.target_by_key(target_key)
        if target is None:
            return ScheduledOperationResult(
                status="not_found",
                message="印刷内容を確認できませんでした。",
            )
        if part_number is not None:
            self.state_store.mark_part_not_printed(target_key, part_number)
        return self._print_target(
            target_key,
            target,
            automatic=False,
            now=datetime.now(timezone.utc),
            requested_by=requested_by,
            only_part_number=part_number,
        )

    def _print_target(
        self,
        target_key: str,
        target: SheetTarget,
        *,
        automatic: bool,
        now: datetime,
        requested_by: str,
        only_part_number: str | None = None,
    ) -> ScheduledOperationResult:
        with self.state_store.operation_lock():
            if self.state_store.printing_completed(target_key):
                return ScheduledOperationResult(
                    status="already_processed",
                    message=f"{target.sheet_name} は印刷処理済みです。",
                    target=target,
                    target_key=target_key,
                )

            try:
                part_numbers = self.gateway.fetch_part_numbers(
                    target.sheet_name,
                    target_date=target.target_date,
                )
            except Exception as exc:
                logger.exception(
                    "Scheduled drawing list could not be read: target=%s requested_by=%s",
                    target.sheet_name,
                    requested_by,
                )
                return self._finish_incomplete_attempt(
                    target_key,
                    target,
                    automatic=automatic,
                    now=now,
                    error=str(exc),
                )

            self.state_store.register_print_items(target_key, part_numbers)
            statuses = self.state_store.print_item_statuses(target_key)
            eligible = [
                part_number
                for part_number in part_numbers
                if statuses.get(part_number, "pending")
                in {"pending", "failed"}
                and (only_part_number is None or part_number == only_part_number)
            ]
            if only_part_number is not None and only_part_number not in part_numbers:
                logger.warning(
                    "User-requested part is no longer on the target sheet: target=%s part=%s",
                    target.sheet_name,
                    only_part_number,
                )
                return ScheduledOperationResult(
                    status="not_found",
                    message="対象の加工図を印刷内容から確認できませんでした。",
                    target=target,
                    target_key=target_key,
                )

            try:
                self._ensure_drawing_directory_available()
            except ScheduledOperationError as exc:
                for part_number in eligible:
                    self.state_store.mark_print_item(
                        target_key, part_number, "failed", error=str(exc)
                    )
                logger.exception(
                    "Scheduled drawing directory is unavailable: target=%s requested_by=%s",
                    target.sheet_name,
                    requested_by,
                )
                return self._finish_incomplete_attempt(
                    target_key,
                    target,
                    automatic=automatic,
                    now=now,
                    error=str(exc),
                )

            submitted_count = 0
            for part_number in eligible:
                try:
                    drawing_path = self.drawing_service.find_pdf(part_number)
                except NasDrawingAccessError as exc:
                    self.state_store.mark_print_item(
                        target_key, part_number, "failed", error=str(exc)
                    )
                    logger.exception(
                        "Scheduled drawing lookup failed: target=%s part=%s requested_by=%s",
                        target.sheet_name,
                        part_number,
                        requested_by,
                    )
                    continue
                if drawing_path is None:
                    self.state_store.mark_print_item(
                        target_key,
                        part_number,
                        "manual_required",
                    )
                    logger.warning(
                        "Scheduled drawing was unavailable at 15:00 and requires manual "
                        "printing after upload: target=%s part=%s requested_by=%s",
                        target.sheet_name,
                        part_number,
                        requested_by,
                    )
                    continue

                try:
                    job_id = self.printer.print_pdf(drawing_path)
                except Exception as exc:
                    uncertain = bool(getattr(exc, "may_have_submitted", False))
                    self.state_store.mark_print_item(
                        target_key,
                        part_number,
                        "uncertain" if uncertain else "failed",
                        error=str(exc),
                    )
                    logger.exception(
                        "Scheduled drawing submission failed: target=%s part=%s "
                        "requested_by=%s uncertain=%s",
                        target.sheet_name,
                        part_number,
                        requested_by,
                        uncertain,
                    )
                    continue

                self.state_store.mark_part_printed(
                    target_key, part_number, job_id=job_id
                )
                submitted_count += 1
                logger.info(
                    "Scheduled drawing submitted: target=%s part=%s requested_by=%s job_id=%s",
                    target.sheet_name,
                    part_number,
                    requested_by,
                    job_id,
                )

            state = self.state_store.latest_print_state_for_key(target_key)
            if state is not None and not state.unresolved_items:
                self.state_store.mark_printing_completed(target_key)
                logger.info(
                    "Scheduled drawing printing completed: target=%s requested_by=%s",
                    target.sheet_name,
                    requested_by,
                )
                return ScheduledOperationResult(
                    status="completed",
                    message=(
                        f"{target.sheet_name} の加工図 {submitted_count} 件を"
                        "印刷キューへ送信しました。"
                    ),
                    target=target,
                    processed_count=submitted_count,
                    target_key=target_key,
                )

            uncertain = bool(state and state.uncertain_items)
            return self._finish_incomplete_attempt(
                target_key,
                target,
                automatic=automatic,
                now=now,
                error="One or more drawings were not submitted",
                uncertain=uncertain,
                processed_count=submitted_count,
            )

    def _finish_incomplete_attempt(
        self,
        target_key: str,
        target: SheetTarget,
        *,
        automatic: bool,
        now: datetime,
        error: str,
        uncertain: bool = False,
        processed_count: int = 0,
    ) -> ScheduledOperationResult:
        if automatic:
            status = self.state_store.record_automatic_print_failure(
                target_key,
                now=now,
                retry_delays=self.settings.print_retry_delays,
                error=error,
                uncertain=uncertain,
            )
        else:
            self.state_store.mark_manual_print_incomplete(target_key, error=error)
            status = "action_required"
        logger.warning(
            "Scheduled drawing printing incomplete: target=%s status=%s automatic=%s",
            target.sheet_name,
            status,
            automatic,
        )
        return ScheduledOperationResult(
            status=status,
            message="印刷できなかった加工図があります。",
            target=target,
            processed_count=processed_count,
            target_key=target_key,
        )

    def _ensure_drawing_directory_available(self) -> None:
        directory = self.drawing_service.drawing_directory
        if directory is None:
            raise ScheduledOperationError("NAS_DRAWING_DIRECTORY is not configured")
        try:
            available = directory.is_dir()
        except OSError as exc:
            raise ScheduledOperationError("NAS drawing directory is unavailable") from exc
        if not available:
            raise ScheduledOperationError("NAS drawing directory is unavailable")
