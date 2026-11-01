"""Statistics workbook export and explicitly requested desktop opening."""

from collections.abc import Iterable
import json
import os
import subprocess
import sys
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from checksi.artifacts import reserve_file
from checksi.errors.tracker import ERROR_NAMES


def export_error_stats(stats: dict, file_input_pdf: str, error_stat_path: str, log=None,
                       open_file: bool = True, *, compounds: Iterable[dict] | None = None) -> str | None:
    """Export diagnostic counts and optional per-compound carbon evidence."""
    _log = log or print
    if not error_stat_path:
        _log("Warning: error_stat_path is not configured. Skipping error statistics export.")
        return None

    normalized_path = os.path.abspath(os.path.normpath(error_stat_path))
    output_dir = os.path.dirname(normalized_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    # Preserve canonical ordering and ensure all keys exist.
    ordered_stats = {name: int(stats.get(name, 0)) for name in ERROR_NAMES}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "error_stat"

    ws["A1"] = "ERROR"
    ws["B1"] = file_input_pdf
    ws["B1"].data_type = "s"  # Source filenames are text, including a leading equals sign.
    yellow_fill = PatternFill(start_color="FFFF00", end_color="FFFF00", fill_type="solid")

    for row_index, (key, value) in enumerate(ordered_stats.items(), start=2):
        ws.cell(row=row_index, column=1, value=key)
        cell = ws.cell(row=row_index, column=2, value=value)
        if value != 0:
            cell.fill = yellow_fill

    if compounds is not None:
        _add_carbon_sheets(wb, compounds)

    output_path = str(reserve_file(normalized_path))
    try:
        wb.save(output_path)
    except Exception:
        Path(output_path).unlink(missing_ok=True)
        raise
    finally:
        wb.close()

    if open_file:
        try:
            if sys.platform.startswith("win"):
                os.startfile(output_path)
            elif sys.platform.startswith("darwin"):
                subprocess.run(["open", output_path], check=True)
            elif sys.platform.startswith("linux"):
                subprocess.run(["xdg-open", output_path], check=True)
        except Exception as exc:
            _log(f"Failed to open the file {output_path}: {exc}")

    return output_path


def _append_values(sheet, row: int, values) -> None:
    """Keep source strings literal, including Excel formula/error prefixes."""
    for column, value in enumerate(values, start=1):
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False)
        cell = sheet.cell(row=row, column=column, value=value)
        if isinstance(value, str):
            cell.data_type = 's'


def _evidence_sheet(workbook, title: str, headers: tuple[str, ...]):
    sheet = workbook.create_sheet(title)
    _append_values(sheet, 1, headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical='top')
        sheet.column_dimensions[cell.column_letter].width = 22
    sheet.column_dimensions['C'].width = 32
    sheet.row_dimensions[1].height = 45
    sheet.freeze_panes = 'D2'
    return sheet


def _diagnostic_note(compound: dict, code: str) -> str | None:
    return '\n'.join(line for line in compound.get('all_warnings', []) if code + '.' in line) or None


def _add_carbon_sheets(workbook, compounds: Iterable[dict]) -> None:
    """Project existing analysis fields without recalculating scientific results."""
    scope_sheet = _evidence_sheet(workbook, 'Carbon scope', (
        'Compound ID', 'Identifier', 'Name', 'Full expected environments',
        'Native candidate environments', 'Listed observations', 'Represented signals',
        'Accepted absent direct C–B environments', 'Accepted absent direct C–B labels',
        'Accepted absence note',
        'Carbon partition complete', 'Carbon partition reason', 'Configuration status',
        'Structural meso status', 'Structural meso reason', 'Structural enumeration complete',
        'DP5 status', 'DP5 reason', 'DP5 message', 'DP5 score (%)', 'Scored environments',
        'DP5 original observations', 'Score scope', 'Shared observations', 'Scope note',
        'Selected isomer SMILES',
        'Expected carbon counts by configuration', 'Carbon grouping depends on configuration',
        'Any nucleus grouping depends on configuration', 'Configuration warning',
        'Selected preparation isomer SMILES', 'Conditional DP5 warning',
    ))
    assignments_sheet = _evidence_sheet(workbook, 'DP5 assignments', (
        'Compound ID', 'Identifier', 'Name', 'Carbon environment', 'Atom labels',
        'Original observation index (0-based)', 'Evidence weight', 'Experimental shift (ppm)',
        'Calculated shift (ppm)', 'Corrected error (ppm)', 'DP5 compatibility (%)',
    ))
    assignment_row = 2
    for scope_row, compound in enumerate(compounds, start=2):
        identity = (compound.get('id'), compound.get('identifier'), compound.get('name'))
        scope = compound.get('carbon_scope') or {}
        stereo = compound.get('stereo_assessment') or {}
        dp5 = compound.get('dp5') or {}
        diagnostics = dp5.get('diagnostics') or {}
        assessed = dp5 if dp5.get('status') == 'success' else {}
        score_scope = diagnostics if assessed else {}
        _append_values(scope_sheet, scope_row, (*identity,
            scope.get('full_environment_count'), scope.get('native_candidate_environment_count'),
            scope.get('observation_count'), scope.get('represented_signal_count'),
            scope.get('accepted_missing_direct_cb_count'), scope.get('accepted_missing_direct_cb_environments'),
            _diagnostic_note(compound, 'c13_boron_signal_not_observed'),
            scope.get('partition_complete'), scope.get('partition_reason'), scope.get('configuration_status'),
            stereo.get('status'), stereo.get('reason'), stereo.get('enumeration_complete'),
            dp5.get('status'), dp5.get('reason'),
            diagnostics.get('message') or dp5.get('message') or _diagnostic_note(compound, 'dp5_boron_not_supported'),
            assessed.get('score_percent', assessed.get('probability_percent')),
            score_scope.get('scored_environment_count'), score_scope.get('observation_count'),
            score_scope.get('score_scope'), score_scope.get('coincident_observations'), score_scope.get('scope_note'),
            (assessed.get('provenance') or {}).get('selected_isomer_smiles'),
            scope.get('expected_counts'), scope.get('configuration_dependent'),
            scope.get('isomer_scope_dependent'), _diagnostic_note(compound, 'nmr_configuration_dependent'),
            scope.get('selected_isomer_smiles'), _diagnostic_note(compound, 'dp5_configuration_dependent'),
        ))
        for atom in assessed.get('atoms', []):
            _append_values(assignments_sheet, assignment_row, (*identity,
                atom.get('label', atom.get('atom_label')), atom.get('atom_labels'),
                atom.get('observation_index'), atom.get('evidence_weight'),
                atom.get('assigned_shift', atom.get('experimental_shift')), atom.get('calculated_shift'),
                atom.get('error', atom.get('scaled_error')),
                atom.get('score_percent', atom.get('atom_probability_percent')),
            ))
            assignment_row += 1
    for sheet in (scope_sheet, assignments_sheet):
        sheet.auto_filter.ref = sheet.dimensions
