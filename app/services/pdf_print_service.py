from __future__ import annotations

from pathlib import Path
from typing import Protocol


class PdfPrintError(RuntimeError):
    """A PDF could not be submitted to the configured Windows printer."""

    def __init__(self, message: str, *, may_have_submitted: bool = False) -> None:
        super().__init__(message)
        self.may_have_submitted = may_have_submitted


class PdfPrinter(Protocol):
    def print_pdf(self, pdf_path: Path) -> int | None: ...


class RawPdfPrinter:
    """Submit PDF bytes using the RAW workflow from the existing production script."""

    def __init__(self, printer_name: str) -> None:
        self.printer_name = printer_name

    def print_pdf(self, pdf_path: Path) -> int | None:
        try:
            import win32print
        except ImportError as exc:
            raise PdfPrintError("pywin32 is not installed") from exc

        printer = None
        document_started = False
        page_started = False
        write_started = False
        job_id: int | None = None
        try:
            content = pdf_path.read_bytes()
            printer = win32print.OpenPrinter(self.printer_name)
            job_id = win32print.StartDocPrinter(
                printer,
                1,
                (f"翌営業日加工図_{pdf_path.stem}", None, "RAW"),
            )
            document_started = True
            win32print.StartPagePrinter(printer)
            page_started = True
            write_started = True
            written = win32print.WritePrinter(printer, content)
            if written != len(content):
                raise OSError("The printer accepted only part of the PDF data")
            win32print.EndPagePrinter(printer)
            page_started = False
            win32print.EndDocPrinter(printer)
            document_started = False
        except Exception as exc:
            raise PdfPrintError(
                f"PDF print submission failed: {pdf_path.name}",
                may_have_submitted=write_started,
            ) from exc
        finally:
            if printer is not None:
                if page_started:
                    try:
                        win32print.EndPagePrinter(printer)
                    except Exception:
                        pass
                if document_started:
                    try:
                        win32print.EndDocPrinter(printer)
                    except Exception:
                        pass
                try:
                    win32print.ClosePrinter(printer)
                except Exception:
                    pass
        return job_id
