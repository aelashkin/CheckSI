"""Settings parity checks using real configuration and Qt worker objects."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from checksi.config import ConfigManager
from checksi.config_schema import AppConfig
from checksi.gui.config_editor import ConfigEditorPage


class SettingsParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.editor = ConfigEditorPage()
        self.addCleanup(self.editor.deleteLater)
        self.config = ConfigManager.load_factory()

    def test_common_settings_precede_collapsed_specialist_controls(self) -> None:
        self.editor.load_from_config(self.config)
        for name in ("conversion_section", "solvents_section", "errors_section", "dp5_tuning_section"):
            section = getattr(self.editor, name)
            self.assertFalse(section.is_expanded(), name)
        self.assertFalse(self.editor.dp5_enabled_checkbox.isHidden())
        self.assertLess(
            self.editor.content_layout.indexOf(self.editor.dp5_card),
            self.editor.content_layout.indexOf(self.editor.conversion_section),
        )

    def test_valid_session_values_round_trip_exactly_after_unrelated_edit(self) -> None:
        self.config.paths.input_path = Path(" input with spaces ")
        self.config.conversion.libreoffice_path = " converter with spaces "
        self.config.dp5.score_threshold_percent = 2.25123456789
        self.config.dp5.timeout_seconds = 2**31
        self.config.modes.header_margin = 0.123456789
        self.config.solvents.entries[0].name = ""
        self.config.solvents.entries[0].aliases = ["one,two", "", " spaced "]
        self.editor.load_from_config(self.config)
        self.editor.open_reports_checkbox.setChecked(not self.config.modes.open_reports_on_success)
        expected = self.config.model_copy(deep=True)
        expected.modes.open_reports_on_success = self.editor.open_reports_checkbox.isChecked()
        self.assertEqual(self.editor.build_config(), expected)

    def test_editing_one_solvent_cell_keeps_other_exact_values(self) -> None:
        self.config.solvents.entries[0].aliases = ["one,two", " spaced "]
        self.editor.load_from_config(self.config)
        self.editor.solvents_table.item(0, 2).setText("1.25, 2.5")
        actual = self.editor.build_config()
        self.assertEqual(actual.solvents.entries[0].aliases, ["one,two", " spaced "])
        self.assertEqual(actual.solvents.entries[0].proton_shifts, [1.25, 2.5])
        self.editor.solvents_table.item(0, 1).setText("new, aliases")
        self.assertEqual(self.editor.build_config().solvents.entries[0].aliases, ["new", "aliases"])

    def test_loaded_text_beyond_widget_limit_is_kept_until_edited(self) -> None:
        self.config.conversion.libreoffice_path = "x" * 40000
        self.editor.load_from_config(self.config)
        self.assertLess(len(self.editor.libreoffice_path_edit.text()), 40000)
        self.assertEqual(self.editor.build_config().conversion.libreoffice_path, "x" * 40000)
        self.editor.libreoffice_path_edit.setText("edited")
        self.assertEqual(self.editor.build_config().conversion.libreoffice_path, "edited")

    def test_advanced_dialog_validates_without_persisting_and_factory_is_read_only(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        dialog = AdvancedConfigDialog(self.config, ConfigManager.load_factory())
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.factory_edit.isReadOnly())
        original_factory = dialog.factory_edit.toPlainText()
        dialog.session_edit.setPlainText('{"unknown": true}')
        dialog._apply_session()
        self.assertIsNone(dialog.validated_config)
        self.assertTrue(dialog.validation_label.text())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        changed = self.config.model_copy(deep=True)
        changed.dp5.timeout_seconds = 999999
        changed.solvents.entries[0].aliases = ["with,a,comma"]
        dialog.session_edit.setPlainText(changed.model_dump_json(indent=2))
        with patch.object(ConfigManager, "save") as save:
            dialog._apply_session()
        save.assert_not_called()
        self.assertEqual(dialog.validated_config, changed)
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.factory_edit.toPlainText(), original_factory)
        dialog.tabs.setCurrentWidget(dialog.factory_edit)
        dialog._copy_current()
        self.assertEqual(self.app.clipboard().text(), original_factory)
        self.assertEqual(AppConfig.model_validate(json.loads(original_factory)), ConfigManager.load_factory())

    def test_source_and_persistence_guidance_remains_visible(self) -> None:
        self.editor.load_from_config(self.config)
        self.editor.set_config_source(ConfigManager("/tmp/selected-settings.toml"))
        self.assertIn("/tmp/selected-settings.toml", self.editor.config_source_label.text())
        self.assertIn("session", self.editor.config_persistence_label.text().lower())
        self.assertIn("Save", self.editor.config_persistence_label.text())

    def test_factory_source_is_identified_without_creating_user_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "user-settings.toml"
            with patch("checksi.config.user_config_path", return_value=destination):
                manager = ConfigManager()
                self.editor.load_from_config(manager.load())
                self.editor.set_config_source(manager)
            self.assertIn("factory", self.editor.config_source_label.text().lower())
            self.assertFalse(destination.exists())

    def test_advanced_view_keeps_schema_valid_nonfinite_shifts(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        self.config.solvents.entries[0].proton_shifts = [float("inf")]
        dialog = AdvancedConfigDialog(self.config, ConfigManager.load_factory())
        self.addCleanup(dialog.deleteLater)
        self.assertIn("Infinity", dialog.session_edit.toPlainText())
        dialog._apply_session()
        self.assertEqual(dialog.validated_config.solvents.entries[0].proton_shifts, [float("inf")])

    def test_many_validation_errors_stay_scrollable_within_the_dialog(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        dialog = AdvancedConfigDialog(self.config, ConfigManager.load_factory())
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        dialog.session_edit.setPlainText(json.dumps({f"unknown_{index}": True for index in range(100)}))
        dialog._apply_session()
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), 700)
        self.assertGreater(dialog.validation_scroll.verticalScrollBar().maximum(), 0)
        dialog.reject()

    def test_advanced_apply_stages_settings_without_saving(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        self.editor.load_from_config(self.config)
        changed = self.config.model_copy(deep=True)
        changed.dp5.timeout_seconds = 123456789
        changed.solvents.entries[0].aliases = ["one,two"]
        loaded = []
        self.editor.config_loaded.connect(loaded.append)

        def apply(dialog):
            dialog.session_edit.setPlainText(changed.model_dump_json())
            dialog._apply_session()
            return dialog.result()

        with patch.object(AdvancedConfigDialog, "exec", new=apply), \
             patch.object(ConfigManager, "save") as save:
            self.editor._show_advanced_configuration()
        save.assert_not_called()
        self.assertEqual(loaded, [changed])
        self.assertEqual(self.editor.build_config(), changed)
        self.assertEqual(self.editor.confirmed_config(), self.config)
        self.assertTrue(self.editor.has_unconfirmed_changes())
        self.assertTrue(self.editor.confirm_changes())
        self.assertEqual(self.editor.confirmed_config(), changed)

    def test_rejected_advanced_legacy_values_do_not_leak_migration_notices(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        dialog = AdvancedConfigDialog(self.config, ConfigManager.load_factory())
        self.addCleanup(dialog.deleteLater)
        invalid = self.config.model_dump(mode="json")
        del invalid["dp5"]["score_threshold_percent"]
        invalid["dp5"]["probability_threshold_percent"] = 17
        invalid["unknown"] = True
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            dialog.session_edit.setPlainText(json.dumps(invalid))
            dialog._apply_session()
        self.assertIsNone(dialog.validated_config)
        self.assertEqual(dialog.migration_notices, ())
        self.assertEqual(captured, [])
        dialog.session_edit.setPlainText(self.config.model_dump_json())
        dialog._apply_session()
        self.assertEqual(dialog.validated_config, self.config)
        self.assertEqual(dialog.migration_notices, ())

    def test_default_refresh_uses_metadata_and_full_check_matches_shared_doctor(self) -> None:
        from checksi.gui.worker import DP5StatusWorker
        from checksi.dp5q.client import doctor_runtime

        settings = self.config.dp5
        settings.timeout_seconds = 123456
        with patch("checksi.gui.worker.check_availability", return_value={"available": True}) as metadata, \
             patch("checksi.dp5q.client.check_availability", return_value={"model_id": "same-model"}) as full_metadata, \
             patch("checksi.dp5q.client._run_diagnostics", return_value=(0, '{"ok": true, "messages": ["Exact CLI diagnostic"]}', "")) as process:
            lightweight = DP5StatusWorker(settings=settings)
            lightweight.run()
            process.assert_not_called()
            metadata.assert_called_once_with(settings)
            results = []
            full = DP5StatusWorker(settings=settings, operation_id=9, full_check=True)
            full.succeeded.connect(lambda operation_id, result: results.append((operation_id, result)))
            full.run()
            expected = doctor_runtime(settings)
            self.assertEqual(process.call_args_list[0], process.call_args_list[1])
            self.assertEqual(full_metadata.call_args_list, [unittest.mock.call(settings)] * 2)
        self.assertEqual(results, [(9, expected)])


if __name__ == "__main__":
    unittest.main()
