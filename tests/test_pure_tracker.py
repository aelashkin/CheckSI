"""Tracking imports do not load report writers or desktop integrations."""

import subprocess
import sys
import textwrap
import unittest


class PureTrackerTests(unittest.TestCase):
    def test_both_tracker_imports_work_without_export_dependencies(self):
        code = textwrap.dedent('''
            import importlib.abc
            import sys
            forbidden = {'openpyxl', 'checksi.artifacts',
                         'checksi.statistics_export', 'PySide6',
                         'checksi.utils.platform_helpers'}
            class BlockExportImports(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if any(fullname == name or fullname.startswith(name + '.')
                           for name in forbidden):
                        raise AssertionError('unexpected export import: ' + fullname)
            sys.meta_path.insert(0, BlockExportImports())
            from checksi.utils.error_handler import ErrorTracker, add_error, ERROR_NAMES
            from checksi.errors.tracker import ErrorTracker as PureTracker
            from checksi.errors.manager import emit_error
            assert PureTracker is ErrorTracker
            first, second = ErrorTracker(), ErrorTracker()
            first.add('name_not_found')
            add_error(first, 'name_not_found')
            assert first.get_count('name_not_found') == 2
            assert second.get_count('name_not_found') == 0
            copied = first.to_dict()
            copied['name_not_found'] = 99
            assert first.get_count('name_not_found') == 2
            assert list(first.to_dict()) == ERROR_NAMES
            assert len(ERROR_NAMES) == 80
            assert ERROR_NAMES[-7:] == ['formula_source_mismatch', 'formula_source_review_required',
                                        'ea_isotope_calcd_mismatch', 'dp5_boron_not_supported', 'c13_boron_signal_not_observed',
                                        'nmr_configuration_dependent', 'dp5_configuration_dependent']
            for name in forbidden:
                assert name not in sys.modules, name
        ''')
        completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_legacy_export_remains_lazy_and_keeps_all_rows(self):
        code = textwrap.dedent('''
            from pathlib import Path
            from tempfile import TemporaryDirectory
            import sys
            from checksi.utils.error_handler import ErrorTracker, ERROR_NAMES, export_error_stats
            assert 'checksi.statistics_export' not in sys.modules
            tracker = ErrorTracker()
            tracker.add('formula_source_review_required')
            with TemporaryDirectory() as directory:
                output = export_error_stats(tracker.to_dict(), '=input.pdf',
                                           str(Path(directory) / 'statistics.xlsx'), open_file=False)
                assert 'checksi.statistics_export' in sys.modules
                from openpyxl import load_workbook
                workbook = load_workbook(output)
                try:
                    rows = list(workbook.active.values)
                    assert rows[0] == ('ERROR', '=input.pdf')
                    assert workbook.active['B1'].data_type == 's'
                    assert [row[0] for row in rows[1:]] == ERROR_NAMES
                    keyed = dict(rows[1:])
                    assert keyed['formula_source_review_required'] == 1
                    assert keyed['ea_isotope_calcd_mismatch'] == 0
                    assert keyed['dp5_boron_not_supported'] == keyed['c13_boron_signal_not_observed'] == 0
                finally:
                    workbook.close()
        ''')
        completed = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
