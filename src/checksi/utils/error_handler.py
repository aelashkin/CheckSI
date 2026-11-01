"""Compatible tracking imports with a lazy statistics-export adapter."""

from collections.abc import Iterable

from checksi.errors.tracker import ERROR_NAMES, ErrorTracker, add_error


__all__ = ["ERROR_NAMES", "ErrorTracker", "add_error", "export_error_stats"]


def export_error_stats(stats: dict, file_input_pdf: str, error_stat_path: str, log=None,
                       open_file: bool = True, *, compounds: Iterable[dict] | None = None) -> str | None:
    """Load workbook and desktop dependencies only when exporting statistics."""
    from checksi.statistics_export import export_error_stats as export

    options = {'compounds': compounds} if compounds is not None else {}
    return export(stats, file_input_pdf, error_stat_path, log=log, open_file=open_file, **options)
