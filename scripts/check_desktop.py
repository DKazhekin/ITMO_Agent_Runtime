"""Exercise the real Qt desktop widgets against a running mock gateway."""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
from time import monotonic, sleep

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit

from client.desktop import Window


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "baf/B1-read/B1.1-service-port/fixture"
TASK = "На каком порту слушает сервис? Запиши только число в файл answer.txt в корне рабочей папки."


def wait_for(condition, label, timeout=80):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if condition():
            return
        QApplication.processEvents()
        sleep(0.025)
    raise TimeoutError(label)


def run(checks):
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)

    def check(name, actual, expected=True):
        passed = actual == expected
        checks.append({"name": name, "actual": actual, "expected": expected, "passed": passed})
        print(f"{name}: {actual!r}", flush=True)
        assert passed, name

    with tempfile.TemporaryDirectory(prefix="itmo-desktop-", dir="/private/tmp") as directory:
        folder = Path(directory) / "workspace"
        shutil.copytree(FIXTURE, folder)
        window = Window()
        window.show()
        try:
            check("offscreen_qt_platform", app.platformName(), "offscreen")
            check("mock_model_selected", window.mode.currentData(), "mock")
            dialog_found = []

            def select_folder():
                dialog = app.activeModalWidget()
                if not isinstance(dialog, QFileDialog):
                    QTimer.singleShot(30, select_folder)
                    return
                dialog_found.append(True)
                dialog.setDirectory(str(folder.parent))
                filename = dialog.findChild(QLineEdit, "fileNameEdit")
                if filename is not None:
                    filename.setText(folder.name)
                dialog.accept()

            QTimer.singleShot(30, select_folder)
            QTest.mouseClick(window.browse, Qt.MouseButton.LeftButton)
            check("directory_dialog_accepted", bool(dialog_found))
            check("synthetic_folder_in_widget", Path(window.folder.text()).resolve() == folder.resolve())
            QTest.mouseClick(window.connect_button, Qt.MouseButton.LeftButton)
            wait_for(lambda: window.send_button.isEnabled() or window.connect_button.isEnabled(),
                     "Gateway did not finish connecting")
            check("session_connected", window.status.text(), "Папка подключена.")
            window.instruction.setText(TASK)
            QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)
            wait_for(lambda: window.status.text() == "Задача завершена."
                     or (window.send_button.isEnabled() and window.run_id is None),
                     "Desktop task did not reach a final status", timeout=160)
            check("desktop_reports_terminal_result", window.status.text(), "Задача завершена.")
            check("mock_completion_visible_in_chat", window.log.toPlainText().splitlines()[-1], "Finished")
            check("answer_written_to_mac", (folder / "answer.txt").read_text().strip(), "8137")
            QTest.mouseClick(window.close_button, Qt.MouseButton.LeftButton)
            wait_for(lambda: window.session is None, "Desktop session release timed out")
            check("gateway_confirmed_session_release", window.status.text(), "Сеанс завершён.")
            window.close()
            check("window_closed", not window.isVisible())
        finally:
            if window.isVisible():
                window.close()
                wait_for(lambda: not window.isVisible(), "Desktop cleanup timed out")
    check("synthetic_folder_cleaned", not Path(directory).exists())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    checks = []
    try:
        run(checks)
    finally:
        args.report.write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
