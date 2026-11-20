"""Desktop client for a mounted workspace. Run with python -m client.desktop."""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QPushButton, QPlainTextEdit, QVBoxLayout, QWidget,
)

from client.cli import Client
from client.mounted import MountSettings, MountedSession


class Replies(QObject):
    ready = Signal(str, int, object, object)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Рабочая папка")
        self.resize(780, 690)
        self.tasks = ThreadPoolExecutor(max_workers=3)
        self.replies = Replies()
        self.replies.ready.connect(self.received)
        self.session = self.run_id = None
        self.proposals = []
        self.mount_settings = None
        self.generation = self.cursor = 0
        self.polling = self.exiting = self.releasing = False
        self.url = QLineEdit(os.getenv("RUNTIME_URL", "http://127.0.0.1:8080"))
        self.token = QLineEdit(os.getenv("CLIENT_TOKEN", "local-checkpoint-token"))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.folder = QLineEdit()
        self.browse = QPushButton("Выбрать папку")
        self.browse.clicked.connect(self.choose)
        folders = QHBoxLayout()
        folders.addWidget(self.folder)
        folders.addWidget(self.browse)
        self.readonly = QCheckBox("Только чтение")
        self.mode = QComboBox()
        self.mode.addItem("Демонстрация", "mock")
        self.mode.addItem("DeepSeek V4.1 Flash 1017", "real")
        self.import_connection = QPushButton("Открыть настройки подключения…")
        self.import_connection.clicked.connect(self.choose_connection)
        form = QFormLayout()
        form.addRow("Адрес сервера", self.url)
        form.addRow("Токен доступа", self.token)
        form.addRow("Файловое соединение", self.import_connection)
        form.addRow("Папка на Mac", folders)
        form.addRow("Права", self.readonly)
        form.addRow("Модель", self.mode)
        self.connect_button = QPushButton("Подключить")
        self.connect_button.clicked.connect(self.connect_folder)
        self.close_button = QPushButton("Завершить сеанс")
        self.close_button.clicked.connect(self.release)
        self.close_button.setEnabled(False)
        self.status = QLabel("Выберите рабочую папку.")
        actions = QHBoxLayout()
        for w in (self.connect_button, self.close_button, self.status):
            actions.addWidget(w)
        self.log = QPlainTextEdit(readOnly=True)
        self.save_button = QPushButton("Сохранить предложенную версию…")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_proposal)
        self.instruction = QLineEdit(placeholderText="Задача для агента")
        self.instruction.returnPressed.connect(self.send)
        self.send_button = QPushButton("Отправить")
        self.send_button.clicked.connect(self.send)
        self.cancel_button = QPushButton("Отменить задачу")
        self.cancel_button.clicked.connect(self.cancel)
        footer = QHBoxLayout()
        for w in (self.instruction, self.send_button, self.cancel_button):
            footer.addWidget(w)
        self.send_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addLayout(actions)
        layout.addWidget(self.log, 1)
        layout.addWidget(self.save_button)
        layout.addLayout(footer)
        body = QWidget()
        body.setLayout(layout)
        self.setCentralWidget(body)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.poll)

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, "Рабочая папка", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def choose_connection(self):
        path, _ = QFileDialog.getOpenFileName(self, "Настройки файлового соединения", "", "JSON (*.json)")
        if path:
            try:
                settings = MountSettings.from_file(path)
            except (OSError, ValueError, TypeError) as error:
                self.status.setText(str(error))
                return
            self.mount_settings = settings
            self.import_connection.setText(f"{settings.ssh_host} · {settings.ssh_port}")

    def save_proposal(self):
        if not self.session or not self.proposals:
            return
        identifier, name = self.proposals[0]
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить отдельную копию", Path(name).name + ".proposed")
        if path:
            try:
                target = self.session.save_proposal(identifier, path)
            except FileExistsError:
                self.status.setText("Выберите новое имя для отдельной копии.")
                return
            except (OSError, ValueError) as error:
                self.status.setText(str(error))
                return
            self.log.appendPlainText("Сохранена отдельная копия · " + str(target))
            self.proposals.pop(0)
            self.save_button.setEnabled(bool(self.proposals))

    def work(self, kind, fn, *args, **kwargs):
        generation = self.generation
        task = self.tasks.submit(fn, *args, **kwargs)

        def finished(future):
            error = future.exception()
            self.replies.ready.emit(kind, generation, None if error else future.result(), error)

        task.add_done_callback(finished)

    def connect_folder(self):
        if not self.folder.text():
            self.status.setText("Сначала выберите папку.")
            return
        self.connect_button.setEnabled(False)
        self.generation += 1
        client = Client(self.url.text().strip(), self.token.text())
        mode, model = self.model()
        directory, readonly = Path(self.folder.text()), self.readonly.isChecked()
        settings = self.mount_settings

        def connect():
            client.preflight(mode, model)
            return MountedSession.open(client, directory, readonly=readonly, settings=settings)

        self.status.setText("Подключение…")
        self.work("connect", connect)

    def model(self):
        mode = self.mode.currentData()
        return mode, "deepseek/deepseek-v4.1-flash-1017" if mode == "real" else None

    def send(self):
        if not self.session or not self.send_button.isEnabled():
            return
        message = self.instruction.text().strip()
        if not message:
            return
        self.log.appendPlainText("Вы · " + message)
        self.instruction.clear()
        self.send_button.setEnabled(False)
        mode, model = self.model()
        self.work("submit", self.session.submit, message, expected_mode=mode, expected_model=model)

    def poll(self):
        if self.run_id and not self.polling and not self.releasing:
            self.polling = True
            self.work("events", self.session.events, self.run_id, self.cursor)

    def cancel(self):
        if self.run_id:
            self.cancel_button.setEnabled(False)
            self.work("cancel", self.session.cancel, self.run_id)

    def release(self):
        if self.session is None or self.releasing:
            return
        self.releasing = True
        self.timer.stop()
        self.status.setText("Завершение сеанса…")
        for w in (self.close_button, self.cancel_button, self.send_button):
            w.setEnabled(False)
        session, run_id = self.session, self.run_id

        def close():
            try:
                if run_id:
                    session.cancel(run_id)
            finally:
                session.close()

        self.work("release", close)

    def received(self, kind, generation, value, error):
        if generation != self.generation:
            return
        if kind == "events":
            self.polling = False
        if self.releasing and kind != "release":
            return
        if kind == "release":
            self.session = self.run_id = None
            self.proposals.clear()
            self.save_button.setEnabled(False)
            self.releasing = self.polling = False
            self.generation += 1
            self.connect_button.setEnabled(True)
            self.status.setText("Сеанс завершён." if not error else "Нет подтверждения завершения от сервера.")
            if self.exiting:
                self.close()
            return
        if error:
            if kind == "events":
                self.status.setText("Восстанавливаем связь с последнего события…")
            else:
                self.status.setText(str(error))
                self.connect_button.setEnabled(self.session is None)
                self.send_button.setEnabled(self.session is not None and not self.run_id)
                if self.exiting and kind == "connect":
                    self.close()
            return
        if kind == "connect":
            self.session = value
            self.status.setText("Папка подключена.")
            self.close_button.setEnabled(True)
            self.send_button.setEnabled(True)
            if self.exiting:
                self.release()
        elif kind == "submit":
            self.run_id, self.cursor = value, 0
            self.status.setText("Задача выполняется.")
            self.cancel_button.setEnabled(True)
            self.timer.start()
            self.poll()
        elif kind == "events":
            for event in value:
                if event["seq"] <= self.cursor:
                    continue
                self.cursor = event["seq"]
                if event["type"] == "tool_call":
                    self.log.appendPlainText("Инструмент · " + event.get("tool", ""))
                elif event["type"] == "file_changed":
                    self.log.appendPlainText("Файл изменён на Mac · " + event.get("path", ""))
                elif event["type"] == "write_decision":
                    decision = event.get("decision", {})
                    self.log.appendPlainText("Запись · " + decision.get("path", "") + " · " + decision.get("status", ""))
                    if decision.get("proposal_id"):
                        self.proposals.append((decision["proposal_id"], decision["path"]))
                        self.save_button.setEnabled(True)
                if event["type"] == "result":
                    state = event["result"]
                    self.log.appendPlainText(state.get("answer") or state.get("error") or state["status"])
                    self.run_id = None
                    self.timer.stop()
                    self.status.setText("Задача завершена.")
                    self.send_button.setEnabled(True)
                    self.cancel_button.setEnabled(False)
                else:
                    self.status.setText("Задача выполняется.")

    def closeEvent(self, event):
        if self.session is not None or not self.connect_button.isEnabled():
            self.exiting = True
            self.release()
            event.ignore()
        else:
            self.tasks.shutdown(wait=False)
            event.accept()


def main():
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
