"""Real native signal delivery isolated from the test runner's signal state."""

from __future__ import annotations

import os
from pathlib import Path
import json
import select
import signal
import subprocess
import sys
import textwrap
import unittest


@unittest.skipUnless(os.name == "posix", "Requires POSIX SIGINT delivery")
class NativeSignalTests(unittest.TestCase):
    """Exercise RDKit's C-level handler rather than calling a Python handler."""

    def test_parent_hangup_termination_and_quit_do_not_leave_a_blocked_worker(self) -> None:
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGQUIT, signal.SIGKILL):
            with self.subTest(signum=signum):
                read_fd, write_fd = os.pipe()
                pid = None
                try:
                    completed = subprocess.run([sys.executable, "-c", textwrap.dedent("""\
                        import json, os, resource, signal, sys
                        from unittest.mock import patch
                        from checksi.dp5q import client
                        from checksi.dp5q.contracts import CheckRequest
                        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                        inherited_fd = int(sys.argv[1])
                        parent_signal = int(sys.argv[2])
                        original_start = client.subprocess.Popen
                        def start(*args, **kwargs):
                            kwargs['pass_fds'] = (inherited_fd,)
                            return original_start(*args, **kwargs)
                        def disconnect(event):
                            if event.message.endswith(': geometry'):
                                print(json.dumps({'worker_pid': session._generation.process.pid}), flush=True)
                                os.killpg(os.getpgrp(), parent_signal)
                        with patch.object(client.subprocess, 'Popen', side_effect=start):
                            with client.DP5qSession(_command=[sys.executable, '-u', sys.argv[3], 'native_block'],
                                                   event_dispatcher=disconnect) as session:
                                session.check(CheckRequest(compound_id='orphan', smiles='CCO', carbon_shifts=(18.5, 58.)))
                        """), str(write_fd), str(signum), str(fixture)],
                        capture_output=True, text=True, timeout=25, check=False,
                        start_new_session=True, pass_fds=(write_fd,))
                    os.close(write_fd)
                    write_fd = None
                    self.assertEqual(completed.returncode, -signum, completed.stdout + completed.stderr)
                    pid = json.loads(completed.stdout.strip())["worker_pid"]
                    # Only the known worker retains this write descriptor after
                    # its parent dies; EOF proves its kernel lifetime ended.
                    self.assertTrue(select.select([read_fd], [], [], 2.5)[0], "Detached worker outlived its owner.")
                    self.assertEqual(os.read(read_fd, 1), b"")
                finally:
                    if write_fd is not None:
                        os.close(write_fd)
                    if pid is not None and not select.select([read_fd], [], [], 0)[0]:
                        # A still-open unique pipe proves this PID is our live
                        # worker, rather than a reused/foreign process identity.
                        os.kill(pid, signal.SIGKILL)
                        select.select([read_fd], [], [], 5)
                    os.close(read_fd)

    def test_terminal_sigint_cancels_and_reaps_real_dp5q_worker(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import os
                import signal
                import tempfile
                import unittest
                from contextlib import ExitStack
                from pathlib import Path
                from unittest.mock import patch
                from checksi import main
                from checksi.config import ConfigManager
                from checksi.config_schema import AppConfig
                from checksi.dp5q.client import DP5qSession
                from tests.dp5q_process_observation import ProcessEscalationObservation

                sessions = []
                interrupted = []
                escalations = []
                observations = ExitStack()
                class ObservedSession(DP5qSession):
                    def __init__(self, settings, event_dispatcher, cancel_token):
                        def dispatch(event):
                            if not interrupted and event.message.endswith(': geometry'):
                                escalations.append(observations.enter_context(ProcessEscalationObservation(self._generation.process)))
                                interrupted.append(True)
                                # A terminal sends Ctrl+C to the entire foreground group.
                                os.killpg(os.getpgrp(), signal.SIGINT)
                            event_dispatcher(event)
                        super().__init__(settings, dispatch, cancel_token)
                        sessions.append(self)

                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as settings_tmp:
                    root = Path(tmp)
                    cfg = AppConfig.model_validate({
                        'paths': {'input_path': 'sample.pdf', 'output_dir': str(root)},
                        'modes': {'deep_name_analysis': 'off', 'output_mode': 'detailed', 'open_reports_on_success': False},
                        'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
                        'dp5': {'enabled': True},
                    })
                    analysis = {'total_found': 1, 'error_stats': {}, 'aborted': False, 'compounds': [{
                        'id': 1, 'smiles': 'C' * 20 + 'O', 'dp5_c_signals_list': list(range(10, 30)),
                        'dp5_carbon_groups': [], 'run_dp5': True,
                    }]}
                    args = main._build_parser().parse_args(['run', 'sample.pdf'])
                    manager = ConfigManager(Path(settings_tmp) / 'settings.toml')
                    manager.save(cfg)
                    with observations, patch('checksi.document_parser.paragraphs_from_input', return_value=['Methods.']), patch(
                        'checksi.application_runner.CheckSIController.run_analysis', return_value=analysis,
                    ), patch('checksi.dp5_enrichment.DP5qSession', ObservedSession), patch(
                        'checksi.application_runner.export_reports',
                    ) as export:
                        code = main._handle_run(args, manager, log_callback=print)
                    export.assert_not_called()
                    assert interrupted, 'Actual native geometry progress was not observed'
                    assert analysis['aborted'] and analysis['compounds'][0]['dp5']['reason'] == 'cancelled', analysis
                    assert len(sessions) == 1 and len(sessions[0]._processes) == 1
                    assert len(escalations) == 1, escalations
                    escalations[0].assert_cancelled_exit(unittest.TestCase())
                    assert not list(root.iterdir()), 'Cancellation created report artifacts'
                    raise SystemExit(code)
                """)],
            capture_output=True, text=True, timeout=40, check=False, start_new_session=True,
        )
        self.assertEqual(completed.returncode, 130, completed.stdout + completed.stderr)
        self.assertIn("report_html: not generated", completed.stdout)
        self.assertIn("Run cancelled.", completed.stdout)
        self.assertNotIn("Fatal Python error", completed.stderr)

    def test_child_embedding_preserves_parent_os_sigint_delivery(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import os
                import signal
                import subprocess
                import sys
                import threading
                from checksi.main import _handle_run_sigint

                token = threading.Event()
                native = "from rdkit import Chem; from rdkit.Chem import AllChem; import time; print('STARTING', flush=True); mol = Chem.AddHs(Chem.MolFromSmiles('CCO')); assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0; print('EMBEDDED', flush=True)"
                with _handle_run_sigint(token):
                    with subprocess.Popen([sys.executable, '-c', native], stdout=subprocess.PIPE, text=True) as child:
                        assert child.stdout.readline().strip() == 'STARTING'
                        os.kill(os.getpid(), signal.SIGINT)
                        assert token.is_set(), 'Child native work displaced the parent handler'
                        stdout, _ = child.communicate(timeout=20)
                        assert child.returncode == 0 and 'EMBEDDED' in stdout, stdout
                    token.clear()
                    os.kill(os.getpid(), signal.SIGINT)
                    assert token.is_set(), 'Child teardown displaced the parent handler'
                """)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_background_child_embedding_does_not_install_parent_signal_handlers(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import subprocess
                import sys
                import threading
                from unittest.mock import patch
                from checksi.main import _handle_run_sigint

                results = []
                native = "from rdkit import Chem; from rdkit.Chem import AllChem; mol = Chem.AddHs(Chem.MolFromSmiles('CCO')); assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0"
                def prepare():
                    with _handle_run_sigint(threading.Event()):
                        results.append(subprocess.run([sys.executable, '-c', native], capture_output=True, timeout=20))

                with patch('checksi.main.signal.signal') as install:
                    thread = threading.Thread(target=prepare)
                    thread.start()
                    thread.join(timeout=25)
                    assert not thread.is_alive()
                    install.assert_not_called()
                assert len(results) == 1 and results[0].returncode == 0, results
                """)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_os_sigint_from_native_child_cancels_cli_before_report_export(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import subprocess
                import sys
                import tempfile
                from pathlib import Path
                from unittest.mock import patch
                from checksi import main
                from checksi.config import ConfigManager
                from checksi.config_schema import AppConfig

                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as settings_tmp:
                    root = Path(tmp)
                    cfg = AppConfig.model_validate({
                        'paths': {'input_path': 'sample.pdf', 'output_dir': str(root)},
                        'modes': {'deep_name_analysis': 'off', 'output_mode': 'detailed', 'open_reports_on_success': True},
                        'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
                        'dp5': {'enabled': True},
                    })
                    analysis = {'total_found': 1, 'error_stats': {}, 'aborted': False, 'compounds': [{
                        'id': 1, 'smiles': 'CCO', 'dp5_c_signals_list': [60.0, 20.0],
                        'dp5_carbon_groups': [], 'run_dp5': True,
                    }]}
                    def child_work(_cfg, data, **kwargs):
                        native = "from rdkit import Chem; from rdkit.Chem import AllChem; import os, signal; mol = Chem.AddHs(Chem.MolFromSmiles('CCO')); assert AllChem.EmbedMolecule(mol, randomSeed=7) == 0; os.kill(os.getppid(), signal.SIGINT)"
                        child = subprocess.run([sys.executable, '-c', native], capture_output=True, text=True, timeout=20)
                        assert child.returncode == 0, child.stderr
                        assert kwargs['cancel_token'].is_set()
                        data['compounds'][0]['dp5'] = {'status': 'cancelled'}
                    args = main._build_parser().parse_args(['run', 'sample.pdf'])
                    manager = ConfigManager(Path(settings_tmp) / 'settings.toml')
                    manager.save(cfg)
                    with patch('checksi.application_runner.check_availability'), patch(
                        'checksi.document_parser.paragraphs_from_input', return_value=['Methods.'],
                    ), patch('checksi.application_runner.CheckSIController.run_analysis', return_value=analysis), patch(
                        'checksi.application_runner.enrich_analysis_with_dp5', side_effect=child_work,
                    ), patch('checksi.application_runner.export_reports') as export, patch(
                        'checksi.application_runner.open_system_file',
                    ) as open_file:
                        code = main._handle_run(args, manager, log_callback=print)
                    export.assert_not_called()
                    open_file.assert_not_called()
                    assert analysis['compounds'][0]['dp5'] == {'status': 'cancelled'}
                    raise SystemExit(code)
                """)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(completed.returncode, 130, completed.stdout + completed.stderr)
        self.assertIn("=== Run Summary ===", completed.stdout)
        self.assertIn("compounds_detected: 1", completed.stdout)
        self.assertIn("report_html: not generated", completed.stdout)
        self.assertIn("Run cancelled.", completed.stdout)

    def test_os_sigterm_cancels_cli_before_report_export(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                import os
                import signal
                import tempfile
                from pathlib import Path
                from unittest.mock import patch
                from checksi import main
                from checksi.config import ConfigManager
                from checksi.config_schema import AppConfig

                signal.signal(signal.SIGTERM, signal.SIG_DFL)  # In case the runner ignores it.
                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as settings_tmp:
                    root = Path(tmp)
                    cfg = AppConfig.model_validate({
                        'paths': {'input_path': 'sample.pdf', 'output_dir': str(root)},
                        'modes': {'deep_name_analysis': 'off', 'output_mode': 'detailed', 'open_reports_on_success': False},
                        'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
                    })
                    def terminated(*args, **kwargs):
                        os.kill(os.getpid(), signal.SIGTERM)
                        return {'total_found': 1, 'error_stats': {}, 'aborted': False}
                    args = main._build_parser().parse_args(['run', 'sample.pdf'])
                    manager = ConfigManager(Path(settings_tmp) / 'settings.toml')
                    manager.save(cfg)
                    with patch('checksi.document_parser.paragraphs_from_input', return_value=['Methods.']), patch(
                        'checksi.application_runner.CheckSIController.run_analysis', side_effect=terminated,
                    ), patch('checksi.application_runner.export_reports') as export:
                        code = main._handle_run(args, manager, log_callback=print)
                    export.assert_not_called()
                    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
                    raise SystemExit(code)
                """)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(completed.returncode, 130, completed.stdout + completed.stderr)
        self.assertIn("=== Run Summary ===", completed.stdout)
        self.assertIn("Run cancelled.", completed.stdout)

    def test_ctrl_c_outside_run_ends_the_cli_process_by_sigint(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", textwrap.dedent("""\
                from types import SimpleNamespace
                from unittest.mock import patch
                from checksi import main
                from checksi.config_schema import AppConfig

                cfg = AppConfig.model_validate({
                    'paths': {'input_path': 'sample.pdf', 'output_dir': 'report.docx'},
                    'modes': {'deep_name_analysis': 'off', 'output_mode': 'essential'},
                    'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
                })
                manager = SimpleNamespace(load=lambda: cfg, migration_notices=())
                with patch('checksi.main.ConfigManager', return_value=manager), patch(
                    'checksi.main.doctor_runtime', side_effect=KeyboardInterrupt,
                ):
                    main.main_cli(['dp5', 'doctor'], exit_on_interrupt=True)
                raise SystemExit('the process should have ended by SIGINT')
                """)],
            capture_output=True, text=True, timeout=40, check=False,
        )
        self.assertEqual(completed.returncode, -signal.SIGINT, completed.stdout + completed.stderr)
        self.assertIn("Cancelled.", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_closed_stderr_reader_keeps_valid_reports_and_normal_exit(self) -> None:
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", textwrap.dedent('''\
                    import tempfile
                    from pathlib import Path
                    import fitz
                    import json
                    from openpyxl import load_workbook
                    from checksi import main
                    from checksi.config import ConfigManager
                    from checksi.config_schema import AppConfig

                    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as settings_tmp:
                        root = Path(tmp)
                        source = root / "input.pdf"
                        with fitz.open() as pdf:
                            page = pdf.new_page()
                            page.insert_text((72, 72), "General methods.")
                            pdf.save(source)
                        cfg = AppConfig.model_validate({
                            "paths": {"input_path": str(source), "output_dir": str(root / "reports")},
                            "modes": {"deep_name_analysis": "off", "output_mode": "detailed", "open_reports_on_success": False},
                            "solvents": {"entries": []}, "errors": {"default_severity": "warning"},
                        })
                        args = main._build_parser().parse_args(["run", str(source)])
                        manager = ConfigManager(Path(settings_tmp) / 'settings.toml')
                        manager.save(cfg)
                        code = main._handle_run(args, manager, log_callback=print)
                        assert code == 3, code
                        result_path = next(root.rglob("results.json"))
                        payload = json.loads(result_path.read_text())
                        assert payload["analysis"]["assessment"]["status"] == "none"
                        assert not payload["export"]["partial"]
                        run_dir = result_path.parent
                        assert (run_dir / "findings.html").is_file()
                        load_workbook(run_dir / "statistics.xlsx").close()
                        with fitz.open(run_dir / "findings.pdf") as report:
                            assert report.page_count >= 1
                        raise SystemExit(code)
                    ''')],
                stdout=subprocess.PIPE, stderr=write_fd, text=True, timeout=30, check=False,
            )
        finally:
            os.close(write_fd)
        self.assertEqual(completed.returncode, 3, completed.stdout)
        self.assertIn("=== Run Summary ===", completed.stdout)
        self.assertNotIn("not generated", completed.stdout)


if __name__ == "__main__":
    unittest.main()
