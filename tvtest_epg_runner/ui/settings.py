"""The settings dialog.

Everything the configuration file holds is editable here.  Saving writes the
file and loads it straight back, so the same validation the runner uses on
startup decides whether an edit is acceptable.
"""

from __future__ import annotations

import glob
import logging
import os

import requests
from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QRadioButton, QTabWidget,
    QTableWidget, QTableWidgetItem, QTimeEdit, QVBoxLayout, QWidget,
)

from .. import channels
from .. import config as config_module
from ..edcb import EdcbClient, EdcbUnavailable, free_until
from ..notify import STATUS_PATH
from ..util import format_duration, parse_duration

logger = logging.getLogger(__name__)

DRIVER_COLUMNS = ("ドライバ", "制限時間", "最小空き", "同時", "チャンネル", "有効")


class SettingsDialog(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TVTest EPG Runner の設定")
        self.resize(620, 520)

        self._config = config
        self._values = config_module.values_from(config)
        self.saved_config = None

        tabs = QTabWidget()
        tabs.addTab(self._build_general(), "全般")
        tabs.addTab(self._build_drivers(), "チューナー")
        tabs.addTab(self._build_schedule(), "スケジュール")
        tabs.addTab(self._build_edcb(), "EDCB")
        tabs.addTab(self._build_addon(), "通知")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(QLabel(f"設定ファイル: {config.path}"))
        layout.addWidget(buttons)

        self._load_values()

    # -- 全般 --------------------------------------------------------------

    def _build_general(self):
        page = QWidget()
        form = QFormLayout(page)

        self.exe_edit = QLineEdit()
        browse = QPushButton("参照…")
        browse.clicked.connect(self._browse_exe)
        row = QHBoxLayout()
        row.addWidget(self.exe_edit)
        row.addWidget(browse)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("TVTest.exe", holder)
        form.addRow("", QLabel("視聴用とは別の、取得専用のフォルダを指定してください。"))

        self.args_edit = QLineEdit()
        self.args_edit.setPlaceholderText("/log")
        form.addRow("追加の引数", self.args_edit)
        form.addRow("", QLabel("/epgcaptureexit と /epgcapturetimeout は自動で付きます。"))

        self.log_level = QComboBox()
        self.log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        form.addRow("ログの詳しさ", self.log_level)

        self.log_file_edit = QLineEdit()
        self.log_file_edit.setPlaceholderText("既定(設定ファイルと同じ場所の runner.log)")
        form.addRow("ログの保存先", self.log_file_edit)
        return page

    def _browse_exe(self):
        start = os.path.dirname(self.exe_edit.text()) or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "TVTest.exe を選ぶ", start, "TVTest (TVTest.exe);;実行ファイル (*.exe)")
        if path:
            self.exe_edit.setText(os.path.normpath(path))

    # -- チューナー --------------------------------------------------------

    def _build_drivers(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.driver_table = QTableWidget(0, len(DRIVER_COLUMNS))
        self.driver_table.setHorizontalHeaderLabels(DRIVER_COLUMNS)
        self.driver_table.verticalHeader().setVisible(False)
        self.driver_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.driver_table.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.driver_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        layout.addWidget(self.driver_table)
        layout.addWidget(QLabel("上から順に取得します。時間は 40m や 1h30m のように書きます。"))

        buttons = QHBoxLayout()
        for label, slot in (
            ("追加", self._add_driver),
            ("削除", self._remove_driver),
            ("上へ", lambda: self._move_driver(-1)),
            ("下へ", lambda: self._move_driver(1)),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        box = QGroupBox("取得するチャンネルの選び方")
        form = QFormLayout(box)
        self.priority_enabled = QCheckBox("最終取得が古いチャンネルから順に取得する")
        form.addRow(self.priority_enabled)
        self.priority_use_addon = QCheckBox(
            "EPG 共有サーバで既に新しいチャンネルは後回しにする")
        form.addRow(self.priority_use_addon)
        self.priority_reserve = QLineEdit()
        form.addRow("制限時間から差し引く余裕", self.priority_reserve)
        form.addRow("", QLabel(
            "「同時」の本数は EDCB の空きチューナー数まで自動で下がります。\n"
            "「チャンネル」は対象の範囲で、空にすると全チャンネルが対象です。"))
        layout.addWidget(box)

        self.priority_enabled.toggled.connect(self.priority_use_addon.setEnabled)
        self.priority_enabled.toggled.connect(self.priority_reserve.setEnabled)
        return page

    def _driver_names(self):
        """The BonDriver files sitting next to the configured TVTest."""
        directory = os.path.dirname(self.exe_edit.text() or self._values["exe"])
        if not directory or not os.path.isdir(directory):
            return []
        return sorted(
            os.path.basename(path)
            for path in glob.glob(os.path.join(directory, "BonDriver_*.dll"))
        )

    def _add_driver_row(self, driver):
        row = self.driver_table.rowCount()
        self.driver_table.insertRow(row)

        combo = QComboBox()
        combo.setEditable(True)
        names = self._driver_names()
        if driver["name"] and driver["name"] not in names:
            names.insert(0, driver["name"])
        combo.addItems(names)
        combo.setCurrentText(driver["name"])
        self.driver_table.setCellWidget(row, 0, combo)

        self.driver_table.setItem(row, 1, QTableWidgetItem(driver["timeout"]))
        self.driver_table.setItem(row, 2, QTableWidgetItem(driver["min_window"]))
        self.driver_table.setItem(row, 3, QTableWidgetItem(str(driver["instances"])))
        self.driver_table.setItem(row, 4, QTableWidgetItem(driver["channels"]))

        enabled = QTableWidgetItem()
        enabled.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        enabled.setCheckState(Qt.Checked if driver["enabled"] else Qt.Unchecked)
        self.driver_table.setItem(row, 5, enabled)

    def _add_driver(self):
        self._add_driver_row({"name": "", "timeout": "40m", "min_window": "15m",
                              "instances": 1, "channels": "", "enabled": True})
        self.driver_table.selectRow(self.driver_table.rowCount() - 1)

    def _remove_driver(self):
        row = self.driver_table.currentRow()
        if row >= 0:
            self.driver_table.removeRow(row)

    def _move_driver(self, delta):
        row = self.driver_table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < self.driver_table.rowCount():
            return
        drivers = self._read_drivers(validate=False)
        drivers[row], drivers[target] = drivers[target], drivers[row]
        self.driver_table.setRowCount(0)
        for driver in drivers:
            self._add_driver_row(driver)
        self.driver_table.selectRow(target)

    def _read_drivers(self, validate=True):
        drivers = []
        for row in range(self.driver_table.rowCount()):
            combo = self.driver_table.cellWidget(row, 0)
            name = combo.currentText().strip() if combo else ""
            driver = {
                "name": name,
                "timeout": self._cell(row, 1),
                "min_window": self._cell(row, 2),
                "instances": self._cell(row, 3) or "1",
                "channels": self._cell(row, 4),
                "enabled": self.driver_table.item(row, 5).checkState() == Qt.Checked,
            }
            if validate:
                if not name:
                    raise ValueError(f"{row + 1} 行目のドライバ名が空です。")
                for key, label in (("timeout", "制限時間"), ("min_window", "最小空き")):
                    try:
                        parse_duration(driver[key])
                    except ValueError as error:
                        raise ValueError(f"{name} の{label}: {error}") from error
                try:
                    driver["instances"] = max(1, int(driver["instances"]))
                except ValueError as error:
                    raise ValueError(f"{name} の同時本数は数字で指定してください。") from error
                if driver["channels"]:
                    try:
                        channels.parse_spec(driver["channels"])
                    except ValueError as error:
                        raise ValueError(f"{name} のチャンネル: {error}") from error
            else:
                driver["instances"] = driver["instances"] or 1
            drivers.append(driver)
        if validate and not drivers:
            raise ValueError("チューナーを少なくとも1つ設定してください。")
        return drivers

    def _cell(self, row, column):
        item = self.driver_table.item(row, column)
        return item.text().strip() if item is not None else ""

    # -- スケジュール ------------------------------------------------------

    def _build_schedule(self):
        page = QWidget()
        layout = QVBoxLayout(page)

        self.times_radio = QRadioButton("毎日決まった時刻に実行する")
        self.every_radio = QRadioButton("一定の間隔で実行する")
        layout.addWidget(self.times_radio)

        times_box = QGroupBox()
        times_layout = QHBoxLayout(times_box)
        self.times_list = QListWidget()
        times_layout.addWidget(self.times_list)

        side = QVBoxLayout()
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        side.addWidget(self.time_edit)
        add_time = QPushButton("追加")
        add_time.clicked.connect(self._add_time)
        side.addWidget(add_time)
        remove_time = QPushButton("削除")
        remove_time.clicked.connect(self._remove_time)
        side.addWidget(remove_time)
        side.addStretch(1)
        times_layout.addLayout(side)
        layout.addWidget(times_box)

        layout.addWidget(self.every_radio)
        every_box = QGroupBox()
        every_form = QFormLayout(every_box)
        self.every_edit = QLineEdit()
        self.every_edit.setPlaceholderText("6h")
        every_form.addRow("間隔", self.every_edit)
        layout.addWidget(every_box)

        self.run_at_start = QCheckBox("起動した直後にも1回実行する")
        layout.addWidget(self.run_at_start)
        layout.addStretch(1)

        self.times_radio.toggled.connect(times_box.setEnabled)
        self.times_radio.toggled.connect(lambda on: every_box.setEnabled(not on))
        return page

    def _add_time(self):
        text = self.time_edit.time().toString("HH:mm")
        existing = {self.times_list.item(i).text() for i in range(self.times_list.count())}
        if text not in existing:
            self.times_list.addItem(text)
            self.times_list.sortItems()

    def _remove_time(self):
        for item in self.times_list.selectedItems():
            self.times_list.takeItem(self.times_list.row(item))

    # -- EDCB --------------------------------------------------------------

    def _build_edcb(self):
        page = QWidget()
        form = QFormLayout(page)

        self.edcb_url = QLineEdit()
        form.addRow("EpgTimerSrv", self.edcb_url)
        form.addRow("", QLabel("EpgTimerSrv.ini の EnableHttpSrv=1 と HttpPort が必要です。"))

        self.edcb_guard = QLineEdit()
        form.addRow("予約の手前で止める", self.edcb_guard)
        self.edcb_poll = QLineEdit()
        form.addRow("予約を確認する間隔", self.edcb_poll)
        self.edcb_timeout = QLineEdit()
        form.addRow("問い合わせのタイムアウト", self.edcb_timeout)
        self.edcb_start_margin = QLineEdit()
        form.addRow("既定の開始マージン", self.edcb_start_margin)
        self.edcb_end_margin = QLineEdit()
        form.addRow("既定の終了マージン", self.edcb_end_margin)

        self.edcb_required = QCheckBox("EDCB に問い合わせできないときは取得しない")
        form.addRow("", self.edcb_required)

        test = QPushButton("接続を確認")
        test.clicked.connect(self._test_edcb)
        form.addRow("", test)
        return page

    def _test_edcb(self):
        from datetime import datetime

        client = EdcbClient(
            self.edcb_url.text().strip(),
            timeout=parse_duration(self.edcb_timeout.text(), 10),
            default_start_margin=parse_duration(self.edcb_start_margin.text(), 30),
            default_end_margin=parse_duration(self.edcb_end_margin.text(), 30),
        )
        try:
            reservations, counts = client.enum_tuner_reserve()
        except EdcbUnavailable as error:
            QMessageBox.warning(self, "EDCB", f"問い合わせできません。\n\n{error}")
            return

        guard = parse_duration(self.edcb_guard.text(), 0)
        now = datetime.now()
        lines = [f"予約 {len(reservations)} 件 / チューナー {sum(counts.values())} 本"]
        for name, count in sorted(counts.items()):
            window, blocker = free_until(reservations, count, name, now, guard=guard)
            free = "空きなし" if window <= 0 else f"空き {format_duration(window)}"
            lines.append(f"\n{name} ({count} 本) — {free}")
            if blocker is not None:
                lines.append(f"    次の予約: {blocker}")
        QMessageBox.information(self, "EDCB", "\n".join(lines))

    # -- 通知 --------------------------------------------------------------

    def _build_addon(self):
        page = QWidget()
        form = QFormLayout(page)

        self.addon_url = QLineEdit()
        self.addon_url.setPlaceholderText("http://homeassistant.local:8077")
        form.addRow("EPG 共有サーバ", self.addon_url)
        form.addRow("", QLabel("空にすると実行結果を送りません。"))

        self.addon_token = QLineEdit()
        self.addon_token.setEchoMode(QLineEdit.Password)
        form.addRow("トークン", self.addon_token)

        self.addon_name = QLineEdit()
        form.addRow("この PC の名前", self.addon_name)

        self.addon_timeout = QLineEdit()
        form.addRow("タイムアウト", self.addon_timeout)

        test = QPushButton("接続を確認")
        test.clicked.connect(self._test_addon)
        form.addRow("", test)
        return page

    def _test_addon(self):
        url = self.addon_url.text().strip().rstrip("/")
        if not url:
            QMessageBox.information(self, "EPG 共有サーバ", "送信先が設定されていません。")
            return

        timeout = parse_duration(self.addon_timeout.text(), 10)
        headers = {}
        if self.addon_token.text():
            headers["X-EPG-Token"] = self.addon_token.text()

        try:
            health = requests.get(f"{url}/api/health", timeout=timeout)
            health.raise_for_status()
            # 実行結果を汚さないよう、読み出しだけで対応を確かめる
            status = requests.get(
                f"{url}{STATUS_PATH}", headers=headers, timeout=timeout)
        except requests.RequestException as error:
            QMessageBox.warning(self, "EPG 共有サーバ", f"接続できません。\n\n{error}")
            return

        if status.status_code in (404, 405, 501):
            QMessageBox.warning(
                self, "EPG 共有サーバ",
                "サーバには接続できましたが、実行結果の受け口がありません。\n"
                "アドオンを 1.4.0 以降に更新してください。")
            return
        if status.status_code == 401:
            QMessageBox.warning(self, "EPG 共有サーバ", "トークンが一致しません。")
            return
        if not status.ok:
            QMessageBox.warning(
                self, "EPG 共有サーバ", f"応答が異常です: HTTP {status.status_code}")
            return

        runners = status.json().get("runners", [])
        known = ", ".join(runner.get("name", "?") for runner in runners) or "(まだなし)"
        QMessageBox.information(
            self, "EPG 共有サーバ",
            f"接続できました。\n\n登録済みのランナー: {known}")

    # -- 値の出し入れ ------------------------------------------------------

    def _load_values(self):
        values = self._values
        self.exe_edit.setText(values["exe"])
        self.args_edit.setText(" ".join(values["extra_args"]))
        self.log_level.setCurrentText(values["log_level"])
        self.log_file_edit.setText(values["log_file"])

        for driver in values["drivers"]:
            self._add_driver_row(driver)

        priority = values["priority"]
        self.priority_enabled.setChecked(priority["enabled"])
        self.priority_use_addon.setChecked(priority["use_addon"])
        self.priority_reserve.setText(priority["reserve"])
        self.priority_use_addon.setEnabled(priority["enabled"])
        self.priority_reserve.setEnabled(priority["enabled"])

        self.times_list.addItems(values["times"])
        self.every_edit.setText(values["every"])
        use_times = not values["every"]
        self.times_radio.setChecked(use_times)
        self.every_radio.setChecked(not use_times)
        self.run_at_start.setChecked(values["run_at_start"])

        edcb = values["edcb"]
        self.edcb_url.setText(edcb["url"])
        self.edcb_guard.setText(edcb["guard"])
        self.edcb_poll.setText(edcb["poll"])
        self.edcb_timeout.setText(edcb["timeout"])
        self.edcb_start_margin.setText(edcb["default_start_margin"])
        self.edcb_end_margin.setText(edcb["default_end_margin"])
        self.edcb_required.setChecked(edcb["required"])

        addon = values["addon"]
        self.addon_url.setText(addon["url"])
        self.addon_token.setText(addon["token"])
        self.addon_name.setText(addon["name"])
        self.addon_timeout.setText(addon["timeout"])

    def _collect(self):
        times = [self.times_list.item(i).text()
                 for i in range(self.times_list.count())]
        use_times = self.times_radio.isChecked()
        if use_times and not times:
            raise ValueError("実行する時刻を1つ以上追加してください。")
        if not use_times and not self.every_edit.text().strip():
            raise ValueError("実行の間隔を指定してください。")

        return {
            "exe": self.exe_edit.text().strip(),
            "extra_args": self.args_edit.text().split(),
            "drivers": self._read_drivers(),
            "times": times if use_times else [],
            "every": "" if use_times else self.every_edit.text().strip(),
            "run_at_start": self.run_at_start.isChecked(),
            "priority": {
                "enabled": self.priority_enabled.isChecked(),
                "use_addon": self.priority_use_addon.isChecked(),
                "reserve": self.priority_reserve.text().strip(),
            },
            "edcb": {
                "url": self.edcb_url.text().strip(),
                "guard": self.edcb_guard.text().strip(),
                "poll": self.edcb_poll.text().strip(),
                "timeout": self.edcb_timeout.text().strip(),
                "default_start_margin": self.edcb_start_margin.text().strip(),
                "default_end_margin": self.edcb_end_margin.text().strip(),
                "required": self.edcb_required.isChecked(),
            },
            "addon": {
                "url": self.addon_url.text().strip(),
                "token": self.addon_token.text(),
                "name": self.addon_name.text().strip(),
                "timeout": self.addon_timeout.text().strip(),
            },
            "log_file": self.log_file_edit.text().strip(),
            "log_level": self.log_level.currentText(),
        }

    def _save(self):
        try:
            values = self._collect()
        except ValueError as error:
            QMessageBox.warning(self, "設定", str(error))
            return

        try:
            self.saved_config = config_module.save(values, self._config.path)
        except config_module.ConfigError as error:
            QMessageBox.warning(self, "設定", f"保存できません。\n\n{error}")
            return
        except OSError as error:
            QMessageBox.critical(self, "設定", f"書き込みに失敗しました。\n\n{error}")
            return

        logger.info("設定を保存しました: %s", self._config.path)
        self.accept()
