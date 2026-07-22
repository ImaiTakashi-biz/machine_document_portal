"""Background schedules for dashboard synchronization and daily operations."""

import logging
from asyncio import sleep, to_thread
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.services.google_sheets_memory_sync_service import GoogleSheetsMemorySyncService
from app.services.memory_store import get_memory_store
from app.services.scheduled_operations_service import ScheduledOperationsService


JST = ZoneInfo("Asia/Tokyo")


async def refresh_google_sheets_periodically() -> None:
    """Refresh Google data and documents for changed part numbers only."""

    settings = get_settings()
    if (
        settings.persistence_mode != "memory"
        or settings.use_sample_data
        or settings.auto_refresh_seconds == 0
    ):
        return
    logger = logging.getLogger(__name__)
    service = GoogleSheetsMemorySyncService(settings, get_memory_store())
    while True:
        await sleep(settings.auto_refresh_seconds)
        result = await to_thread(service.sync_changed)
        if result.ok:
            logger.info(
                "Scheduled Google Sheets synchronization completed: processed=%s",
                result.processed_count,
            )
        else:
            logger.error(
                "Scheduled Google Sheets synchronization failed: %s",
                result.message,
            )


def next_document_refresh_at(
    now: datetime,
    schedule: tuple[time, ...],
) -> datetime:
    """Return the next configured document-refresh time in Japan time."""

    candidates = [
        datetime.combine(now.date(), scheduled_time, tzinfo=JST)
        for scheduled_time in schedule
    ]
    future = [candidate for candidate in candidates if candidate > now]
    if future:
        return min(future)
    return datetime.combine(
        now.date() + timedelta(days=1),
        min(schedule),
        tzinfo=JST,
    )


async def refresh_documents_at_scheduled_times() -> None:
    """Fully refresh Google Sheets, SharePoint, and NAS at configured times."""

    settings = get_settings()
    schedule = settings.document_refresh_schedule
    if (
        settings.persistence_mode != "memory"
        or settings.use_sample_data
        or not schedule
    ):
        return
    logger = logging.getLogger(__name__)
    service = GoogleSheetsMemorySyncService(settings, get_memory_store())
    while True:
        next_run = next_document_refresh_at(datetime.now(JST), schedule)
        delay = max((next_run - datetime.now(JST)).total_seconds(), 0)
        await sleep(delay)
        try:
            result = await to_thread(service.sync)
        except Exception:
            logger.exception("Scheduled full dashboard refresh failed unexpectedly")
            continue
        if result.ok:
            logger.info(
                "Scheduled full dashboard refresh completed: processed=%s",
                result.processed_count,
            )
        else:
            logger.error(
                "Scheduled full dashboard refresh failed: %s",
                result.message,
            )


async def run_daily_scheduled_operations() -> None:
    """Run the 13:00/14:30 notifications and 15:00 printing each day."""

    settings = get_settings()
    if (
        not settings.scheduled_operations_enabled
        or settings.persistence_mode != "memory"
        or settings.use_sample_data
    ):
        return
    logger = logging.getLogger(__name__)
    service = ScheduledOperationsService(settings)
    triggered: set[tuple[str, str]] = set()
    while True:
        now = datetime.now(JST)
        run_date = now.date()
        notification_key = (run_date.isoformat(), "next-day-check")
        if now.time() >= time(13, 0) and notification_key not in triggered:
            triggered.add(notification_key)
            try:
                result = await to_thread(service.check_and_notify, run_date)
            except Exception:
                logger.exception("Daily scheduled operation failed: job=next-day-check")
            else:
                logger.info(
                    "Daily scheduled operation finished: job=%s status=%s message=%s",
                    "next-day-check",
                    result.status,
                    result.message,
                )

        recheck_key = (run_date.isoformat(), "next-day-recheck")
        if now.time() >= time(14, 30) and recheck_key not in triggered:
            triggered.add(recheck_key)
            try:
                result = await to_thread(service.recheck_and_notify, run_date)
            except Exception:
                logger.exception("Daily scheduled operation failed: job=next-day-recheck")
            else:
                logger.info(
                    "Daily scheduled operation finished: job=%s status=%s message=%s",
                    "next-day-recheck",
                    result.status,
                    result.message,
                )

        print_key = (run_date.isoformat(), "drawing-print-no-target")
        if (
            now.time() >= time(15, 0)
            and print_key not in triggered
            and service.automatic_print_due(run_date, now)
        ):
            try:
                result = await to_thread(
                    service.print_drawings,
                    run_date,
                    automatic=True,
                    now=now,
                )
            except Exception:
                logger.exception("Daily scheduled operation failed: job=drawing-print")
            else:
                if result.status == "no_target":
                    triggered.add(print_key)
                logger.info(
                    "Daily scheduled operation finished: job=%s status=%s message=%s",
                    "drawing-print",
                    result.status,
                    result.message,
                )
        cutoff = run_date.isoformat()
        triggered = {key for key in triggered if key[0] == cutoff}
        await sleep(30)
