"""Qt worker objects and event adapters for background analysis runs."""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QObject, Signal, Slot

from checksi.application_runner import ApplicationRunner
from checksi.config_schema import AppConfig, DP5Settings
from checksi.dp5q.client import DP5qAvailabilityError, check_availability, doctor_runtime
from checksi.events import ApplicationEvent, LogEvent, ProgressEvent


class WorkerFailureKind(StrEnum):
    """Failure categories that need distinct GUI recovery actions."""

    GENERAL = "general"
    DP5_PREFLIGHT = "dp5_preflight"


@dataclass(slots=True)
class WorkerFailure:
    """Structured worker failure payload for GUI consumers."""

    message: str
    details: str
    kind: WorkerFailureKind = WorkerFailureKind.GENERAL


class QtEventBridge(QObject):
    """Bridge synchronous application events to Qt signals."""

    log_received = Signal(int, str, str)
    progress_received = Signal(int, int, int, str)
    event_received = Signal(object)

    def __init__(self, parent=None, *, operation_id: int = 0) -> None:
        super().__init__(parent)
        self._operation_id = operation_id

    def dispatch(self, event: ApplicationEvent) -> None:
        """Dispatch an application event through thread-safe Qt signals."""
        self.event_received.emit(event)
        if isinstance(event, LogEvent):
            self.log_received.emit(self._operation_id, event.severity.value, event.message)
            return
        if isinstance(event, ProgressEvent):
            self.progress_received.emit(
                self._operation_id, event.current, event.total, event.message
            )

    __call__ = dispatch


class AnalysisWorker(QObject):
    """Run ApplicationRunner inside a QThread-owned worker object."""

    succeeded = Signal(int, object)
    failed = Signal(int, object)
    done = Signal()

    def __init__(
        self,
        *,
        config: AppConfig,
        input_path: str,
        event_dispatcher,
        cancel_token: threading.Event,
        operation_id: int = 0,
    ) -> None:
        super().__init__()
        self._config = config.model_copy(deep=True)
        self._operation_id = operation_id
        self._input_path = input_path
        self._event_dispatcher = event_dispatcher
        self._cancel_token = cancel_token

    @Slot()
    def run(self) -> None:
        """Execute the analysis pipeline and report success or failure."""
        try:
            runner = ApplicationRunner(event_dispatcher=self._event_dispatcher)
            result = runner.execute(
                self._config,
                self._input_path,
                cancel_token=self._cancel_token,
            )
        except Exception as exc:  # noqa: BLE001 - surface worker failures to the GUI
            message = str(exc) or exc.__class__.__name__
            if isinstance(exc, DP5qAvailabilityError):
                message = (
                    "Bundled DP5q is not ready. Use Settings > DP5q > "
                    "Refresh Status for details, or run `dp5 doctor` from the CLI.\n\n"
                    + "\n".join(exc.messages)
                )
            failure = WorkerFailure(
                message=message,
                details=traceback.format_exc(),
                kind=(
                    WorkerFailureKind.DP5_PREFLIGHT
                    if isinstance(exc, DP5qAvailabilityError)
                    else WorkerFailureKind.GENERAL
                ),
            )
            self.failed.emit(self._operation_id, failure)
        else:
            self.succeeded.emit(self._operation_id, result)
        finally:
            self.done.emit()

    def cancel(self) -> None:
        """Signal cooperative cancellation to the running analysis."""
        self._cancel_token.set()


class DP5StatusWorker(QObject):
    """Check DP5q metadata, or explicitly run the CLI's isolated full self-check."""

    succeeded = Signal(int, object)
    failed = Signal(int, object)
    done = Signal()

    def __init__(self, *, settings: DP5Settings, operation_id: int = 0, full_check: bool = False) -> None:
        super().__init__()
        self._settings = settings.model_copy(deep=True)
        self._operation_id = operation_id
        self._full_check = full_check

    @Slot()
    def run(self) -> None:
        """Emit shared diagnostics without loading model arrays on the GUI thread."""
        try:
            if self._full_check:
                result = doctor_runtime(self._settings)
            else:
                metadata = check_availability(self._settings)
                available = bool(metadata.get("available"))
                messages = [
                    "Bundled DP5q assets are available." if available
                    else "Bundled DP5q assets are unavailable."
                ]
                messages.extend(
                    f"{key}: {metadata[key]}"
                    for key in ("model_id", "score_version") if metadata.get(key)
                )
                result = (available, messages)
        except DP5qAvailabilityError as exc:
            self.succeeded.emit(self._operation_id, (False, list(exc.messages)))
        except Exception as exc:  # noqa: BLE001 - surface status failures to the GUI
            failure = WorkerFailure(
                message=str(exc) or exc.__class__.__name__,
                details=traceback.format_exc(),
            )
            self.failed.emit(self._operation_id, failure)
        else:
            self.succeeded.emit(self._operation_id, result)
        finally:
            self.done.emit()
