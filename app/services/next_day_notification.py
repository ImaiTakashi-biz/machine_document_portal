from __future__ import annotations

from pathlib import Path

from app.services.next_day_sheet_service import SheetTarget
from app.services.scheduled_job_state_store import NotificationPhase


def build_next_day_notification(
    target: SheetTarget,
    *,
    phase: NotificationPhase,
    missing_inspections: list[str],
    missing_drawings: list[str],
    sharepoint_location: str | None,
    drawing_location: Path | str | None,
) -> str:
    """Build the user-facing 13:00 or 14:30 ARAICHAT notification."""
    missing_inspection_set = set(missing_inspections)
    missing_drawing_set = set(missing_drawings)
    part_numbers = list(dict.fromkeys([*missing_inspections, *missing_drawings]))

    part_blocks: list[str] = []
    for part_number in part_numbers:
        lines = [f"■ {part_number}"]
        if part_number in missing_inspection_set:
            lines.extend(
                (
                    "・工程内検査シート",
                    "  → SharePointの所定フォルダへ保存してください",
                )
            )
        if part_number in missing_drawing_set:
            lines.extend(
                (
                    "・加工図面",
                    "  → NASの所定フォルダへ保存してください",
                )
            )
        part_blocks.append("\n".join(lines))

    part_list_text = "\n\n".join(part_blocks)
    target_date = f"{target.target_date.month}月{target.target_date.day}日分"
    missing_count = len(part_numbers)
    sharepoint_text = sharepoint_location or "（保存場所が設定されていません）"
    drawing_text = str(drawing_location or "（保存場所が設定されていません）")

    if phase == "recheck":
        heading = "【翌営業日セット予定分の再確認（14:30）】"
        introduction = (
            "14:30に再確認したところ、\n"
            f"現在も{missing_count}品番で確認できないものがありました。"
        )
        closing = (
            "※加工図面を15:00以降にアップロードした場合は、\n"
            "  自動印刷されません。\n"
            "  アップロード後に手動で発行してください。"
        )
    else:
        heading = "【翌営業日セット予定分の検査シート・加工図面確認通知】"
        introduction = (
            "13:00時点で、翌営業日のセット予定に必要な\n"
            "検査シート・加工図面のうち、\n"
            f"{missing_count}品番で確認できないものがありました。"
        )
        closing = (
            "※加工図面は15:00の印刷前までに保存すると、\n"
            "  自動印刷の対象になります。\n\n"
            "14:30にもう一度確認します。"
        )

    return (
        f"{heading}\n\n"
        f"対象：{target.sheet_name}（{target_date}）\n\n"
        f"{introduction}\n\n"
        f"{part_list_text}\n\n\n"
        "【工程内検査シートの保存場所】\n\n"
        f"{sharepoint_text}\n\n\n"
        "【加工図面の保存場所】\n\n"
        f"{drawing_text}\n\n\n"
        f"{closing}"
    )
