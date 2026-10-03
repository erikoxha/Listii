import json
import os
import sys
import threading
from collections import Counter
from pathlib import Path
from PySide6.QtCore import Qt, QObject, Signal, QRunnable, QThreadPool, QSettings, QSize, QUrl
from PySide6.QtGui import QPixmap, QIcon, QDesktopServices, QColor
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton, QLineEdit,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QFileDialog, QMessageBox, QComboBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QDialog, QDialogButtonBox, QTextEdit)
import requests
from .spotify import Spotify
from .sources import scan_files, load_manifest, assign_sources, match
from .export import Exporter, Options

STYLE = """
* { font-family: 'Inter', 'Segoe UI', 'Helvetica Neue'; font-size: 13px; }
QMainWindow, QDialog { background: #101116; color: #f2f1f7; }
QWidget { color: #eeedf4; }
QFrame#sidebar { background: #15161d; border-right: 1px solid #292b36; }
QFrame#card { background: #191b23; border: 1px solid #2b2e3a; border-radius: 14px; }
QLabel#brand { font-size: 29px; font-weight: 800; color: #b7a4ff; }
QLabel#heading { font-size: 29px; font-weight: 700; }
QLabel#section { font-size: 17px; font-weight: 600; }
QLabel#muted { color: #9296aa; font-size: 12px; }
QLabel#eyebrow { color: #b7a4ff; font-size: 11px; font-weight: 700; }
QLabel#stat { font-size: 22px; font-weight: 700; }
QLineEdit, QComboBox, QTextEdit { background: #11131a; border: 1px solid #343746; border-radius: 7px; padding: 10px; selection-background-color: #6954c5; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #a18bf5; }
QPushButton { background: #272a36; border: 1px solid #3a3e50; border-radius: 7px; padding: 10px 16px; font-weight: 600; }
QPushButton:hover { background: #343848; }
QPushButton#primary { background: #b3a0fa; color: #18112e; border: none; }
QPushButton#primary:hover { background: #c5b5ff; }
QPushButton:disabled { color: #656978; background: #252731; }
QPushButton#nav { text-align: left; background: #2b2540; color: #c7b7ff; border: none; }
QTableWidget { background: #191b23; border: none; gridline-color: #292c37; selection-background-color: #302a46; }
QTableWidget::item { padding: 8px; border-bottom: 1px solid #292c37; }
QHeaderView::section { background: #191b23; color: #9095aa; border: none; border-bottom: 1px solid #343745; padding: 12px 6px; font-size: 11px; }
QProgressBar { border: none; border-radius: 4px; background: #303340; text-align: center; min-height: 8px; max-height: 8px; color: transparent; }
QProgressBar::chunk { background: #b3a0fa; border-radius: 4px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QScrollBar:vertical { background: #191b23; width: 9px; }
QScrollBar::handle:vertical { background: #424557; border-radius: 4px; min-height: 25px; }
"""

class Signals(QObject):
    done = Signal(object)
    error = Signal(str)
    progress = Signal(int, int, str, str)

class Task(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.work = work
        self.signals = Signals()
    def run(self):
        try:
            self.signals.done.emit(self.work(self.signals))
        except Exception as exc:
            self.signals.error.emit(str(exc))


def label(text, kind=None):
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    return frame, layout


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Listii · Your playlist, your files")
        self.resize(1240, 890)
        self.setMinimumSize(1020, 760)
        self.settings = QSettings("Listii", "Listii")
        self.pool = QThreadPool.globalInstance()
        self.playlist = None
        self.spotify = None
        self.sources = []
        self.busy = False
        self.cancel_event = threading.Event()
        self.active_tasks = []
        self.percent = {}
        self.report = None
        container = QWidget()
        self.setCentralWidget(container)
        shell = QHBoxLayout(container)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 30, 22, 24)
        side.addWidget(label("◒  listii", "brand"))
        side.addWidget(label("YOUR MUSIC. IN ORDER.", "muted"))
        side.addSpacing(38)
        nav = QPushButton("♫   Playlist export")
        nav.setObjectName("nav")
        side.addWidget(nav)
        side.addWidget(button("↗   Setup guide", self.setup))
        side.addStretch()
        side.addWidget(label("MADE FOR YOUR LIBRARY", "eyebrow"))
        note = label("Spotify supplies the details.\nYour permitted sources\nsupply the audio.", "muted")
        note.setWordWrap(True)
        side.addWidget(note)
        side.addSpacing(14)
        side.addWidget(label("Listii 1.0", "muted"))
        shell.addWidget(sidebar)
        body = QWidget()
        main = QVBoxLayout(body)
        main.setContentsMargins(30, 26, 30, 22)
        main.setSpacing(16)
        shell.addWidget(body, 1)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.addWidget(label("Bring your playlist home", "heading"))
        titles.addWidget(label("Individual MP3s. Original artwork. Every track in its place.", "muted"))
        header.addLayout(titles)
        header.addStretch()
        self.connect_button = button("Connect Spotify", self.connect_spotify)
        header.addWidget(self.connect_button)
        main.addLayout(header)
        link_card, link_layout = card()
        link_layout.addWidget(label("01  /  CHOOSE A PLAYLIST", "eyebrow"))
        link_row = QHBoxLayout()
        self.link = QLineEdit()
        self.link.setAccessibleName("Spotify playlist URL")
        self.link.setPlaceholderText("Paste a Spotify playlist link…")
        self.link.returnPressed.connect(self.import_playlist)
        self.import_button = button("Load playlist  →", self.import_playlist, True)
        link_row.addWidget(self.link, 1)
        link_row.addWidget(self.import_button)
        link_layout.addLayout(link_row)
        main.addWidget(link_card)
        preview_card, preview_layout = card()
        preview_header = QHBoxLayout()
        self.playlist_title = label("Your playlist will appear here", "section")
        preview_header.addWidget(self.playlist_title)
        preview_header.addStretch()
        self.add_files = button("＋ Audio files", self.choose_files)
        self.add_manifest = button("Authorized links", self.choose_manifest)
        self.review_button = button("Review match", self.review)
        preview_header.addWidget(self.add_files)
        preview_header.addWidget(self.add_manifest)
        preview_header.addWidget(self.review_button)
        preview_layout.addLayout(preview_header)
        self.summary = label("Load a playlist, then attach audio you own or have permission to download.", "muted")
        preview_layout.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["#", "", "TITLE / ARTIST", "ALBUM", "TIME", "AVAILABILITY", "PROGRESS"])
        self.table.setAccessibleName("Playlist tracks and source availability")
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setIconSize(QSize(36, 36))
        sizes = [45, 50, 260, 155, 60, 120, 110]
        for i, size in enumerate(sizes):
            self.table.setColumnWidth(i, size)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(self.review)
        preview_layout.addWidget(self.table, 1)
        self.table.hide()
        self.empty = label("♫\n\nA little organization. A lot of music.\n\nNo playlist loaded yet.", "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(100)
        preview_layout.addWidget(self.empty)
        main.addWidget(preview_card, 1)
        settings_card, settings_layout = card()
        settings_layout.addWidget(label("02  /  MAKE IT YOURS", "eyebrow"))
        grid = QGridLayout()
        grid.addWidget(label("Save to", "muted"), 0, 0)
        grid.addWidget(label("MP3 quality", "muted"), 0, 2)
        grid.addWidget(label("Existing files", "muted"), 0, 3)
        self.destination = QLineEdit(str(self.settings.value("destination", str(Path.home() / "Music"))))
        self.browse = button("Browse…", self.choose_destination)
        self.quality = QComboBox()
        self.quality.addItems(["320 kbps · High", "256 kbps", "192 kbps", "128 kbps"])
        self.policy = QComboBox()
        self.policy.addItems(["Skip existing", "Keep both / rename", "Overwrite existing"])
        grid.addWidget(self.destination, 1, 0)
        grid.addWidget(self.browse, 1, 1)
        grid.addWidget(self.quality, 1, 2)
        grid.addWidget(self.policy, 1, 3)
        self.pattern = QLineEdit("{position} - {artist} - {title}")
        self.pattern.setToolTip("Fields: {position}, {artist}, {title}, {album}. A zero-padded playlist position is always included.")
        grid.addWidget(label("Filename format", "muted"), 2, 0)
        grid.addWidget(self.pattern, 3, 0, 1, 2)
        self.subfolder = QCheckBox("Create playlist folder")
        self.subfolder.setChecked(True)
        grid.addWidget(self.subfolder, 3, 2, 1, 2)
        grid.setColumnStretch(0, 1)
        settings_layout.addLayout(grid)
        settings_layout.addWidget(label("Higher bitrates cannot improve source quality. Playlist position stays separate from album track number.", "muted"))
        main.addWidget(settings_card)
        bottom = QHBoxLayout()
        progress_layout = QVBoxLayout()
        self.progress_title = label("Ready when you are", "section")
        self.progress_detail = label("No downloads have started.", "muted")
        self.overall = QProgressBar()
        self.overall.setValue(0)
        progress_layout.addWidget(self.progress_title)
        progress_layout.addWidget(self.progress_detail)
        progress_layout.addWidget(self.overall)
        bottom.addLayout(progress_layout, 1)
        self.report_button = button("Report", self.show_report)
        self.report_button.setEnabled(False)
        self.cancel_button = button("Cancel", self.cancel_export)
        self.cancel_button.setEnabled(False)
        self.download_button = button("Export MP3s  ↓", self.start_export, True)
        self.download_button.setMinimumWidth(170)
        bottom.addWidget(self.report_button)
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.download_button)
        main.addLayout(bottom)

    def task(self, work, done):
        task = Task(work)
        self.active_tasks.append(task)
        def complete(value):
            self.active_tasks.remove(task)
            done(value)
        def error(message):
            self.active_tasks.remove(task)
            self.set_busy(False)
            self.progress_title.setText("Something needs your attention")
            self.progress_detail.setText(message)
            QMessageBox.warning(self, "Listii", message)
        task.signals.done.connect(complete)
        task.signals.error.connect(error)
        task.signals.progress.connect(self.update_progress)
        self.pool.start(task)

    def set_busy(self, value, exporting=False):
        self.busy = value
        for widget in (self.import_button, self.link, self.connect_button, self.add_files, self.add_manifest,
                       self.review_button, self.destination, self.browse, self.quality, self.policy, self.pattern,
                       self.subfolder, self.download_button):
            widget.setEnabled(not value)
        self.cancel_button.setEnabled(value and exporting)

    def setup(self):
        QMessageBox.information(self, "Listii setup", "1. Create a Spotify developer app and copy its Client ID. No client secret is needed.\n\n2. Register this exact redirect URI:\nhttp://127.0.0.1:8765/callback\n\n3. Connect Spotify and grant playlist access. Spotify may limit playlists available to your app's access tier.\n\n4. Install FFmpeg and make it available on your PATH.\n\n5. Attach your own audio files or a JSON manifest of HTTPS sources that explicitly permit downloading and MP3 conversion. Spotify does not supply downloadable full-track audio.\n\nSee README.md for the manifest schema and full setup instructions.")

    def connect_spotify(self):
        from PySide6.QtWidgets import QInputDialog
        client_id, accepted = QInputDialog.getText(self, "Connect Spotify", "Spotify app Client ID (public identifier):", text=str(self.settings.value("client_id", "")))
        if not accepted or not client_id.strip():
            return
        self.settings.setValue("client_id", client_id.strip())
        self.set_busy(True)
        self.progress_title.setText("Finish sign-in in your browser")
        self.progress_detail.setText("Waiting for Spotify authorization; this window will update automatically.")
        def work(signals):
            spotify = Spotify(client_id)
            spotify.login()
            return spotify
        def done(spotify):
            self.spotify = spotify
            self.connect_button.setText("Spotify connected ✓")
            self.progress_title.setText("Spotify connected")
            self.progress_detail.setText("Paste a playlist link to continue.")
            self.set_busy(False)
        self.task(work, done)

    def import_playlist(self):
        if self.busy:
            return
        from .models import playlist_id
        try:
            playlist_id(self.link.text())
            if not self.spotify:
                client_id = str(self.settings.value("client_id", ""))
                if not client_id:
                    raise ValueError("Connect Spotify first so Listii can retrieve playlist information.")
                self.spotify = Spotify(client_id)
        except Exception as exc:
            QMessageBox.warning(self, "Playlist link", str(exc))
            return
        self.set_busy(True)
        self.progress_title.setText("Reading every playlist entry…")
        self.progress_detail.setText("Fetching all pages and checking playlist order.")
        link = self.link.text()
        def done(playlist):
            self.playlist = playlist
            assign_sources(playlist.entries, self.sources)
            self.render()
            self.set_busy(False)
            self.progress_title.setText("Playlist loaded")
            self.progress_detail.setText("Attach permitted audio to make tracks available.")
            self.load_artwork()
        self.task(lambda _: self.spotify.playlist(link), done)

    def render(self):
        if not self.playlist:
            return
        self.empty.hide()
        self.table.show()
        self.playlist_title.setText(self.playlist.name)
        counts = Counter(e.status for e in self.playlist.entries)
        self.summary.setText("%d tracks  ·  %d ready  ·  %d need review  ·  %d unavailable" % (len(self.playlist.entries), counts["ready"], counts["review"], counts["unavailable"]))
        self.table.setRowCount(len(self.playlist.entries))
        for row, entry in enumerate(self.playlist.entries):
            track = entry.track
            values = [str(track.position).zfill(3), "♫", track.title + "\n" + (", ".join(track.artists) or "Unknown artist"), track.album or "—",
                      "%d:%02d" % (track.duration_ms // 60000, track.duration_ms // 1000 % 60), entry.status.capitalize()]
            self.table.setRowHeight(row, 58)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(entry.detail if column == 5 else value)
                if column == 5:
                    item.setForeground(QColor({"ready": "#9ed9b7", "review": "#eac88b"}.get(entry.status, "#9499ae")))
                self.table.setItem(row, column, item)
            bar = QProgressBar()
            bar.setValue(0)
            self.table.setCellWidget(row, 6, bar)

    def load_artwork(self):
        current = self.playlist
        urls = list({e.track.artwork for e in current.entries if e.track.artwork})
        def work(_):
            images = {}
            for url in urls:
                try:
                    with requests.get(url, timeout=8, stream=True) as response:
                        response.raise_for_status()
                        content = bytearray()
                        for chunk in response.iter_content(65536):
                            content.extend(chunk)
                            if len(content) > 5 * 1024 * 1024:
                                break
                        else:
                            images[url] = bytes(content)
                except requests.RequestException:
                    pass
            return images
        def done(images):
            if self.playlist is not current:
                return
            for row, entry in enumerate(current.entries):
                if entry.track.artwork in images:
                    pixmap = QPixmap()
                    pixmap.loadFromData(images[entry.track.artwork])
                    self.table.item(row, 1).setText("")
                    self.table.item(row, 1).setIcon(QIcon(pixmap))
        self.task(work, done)

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select permitted audio", "", "Audio (*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.opus *.aiff)")
        if not paths:
            return
        self.set_busy(True)
        def done(result):
            sources, errors = result
            self.sources.extend(sources)
            self.rematch()
            self.set_busy(False)
            if errors:
                QMessageBox.warning(self, "Some files could not be read", "\n".join(errors))
        self.task(lambda _: scan_files(paths), done)

    def choose_manifest(self):
        path, _ = QFileDialog.getOpenFileName(self, "Authorized source manifest", "", "JSON (*.json)")
        if path:
            try:
                self.sources.extend(load_manifest(path))
                self.rematch()
            except Exception as exc:
                QMessageBox.warning(self, "Manifest", str(exc))

    def rematch(self):
        self.sources = list({source.location: source for source in self.sources}.values())
        if self.playlist:
            assign_sources(self.playlist.entries, self.sources)
            self.render()
            self.load_artwork()
        self.progress_detail.setText("%d audio sources attached. Only confident or approved matches will export." % len(self.sources))

    def review(self, *args):
        if self.busy or not self.playlist:
            return
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Review match", "Select a playlist row first.")
            return
        entry = self.playlist.entries[row]
        dialog = QDialog(self)
        dialog.setWindowTitle("Review recording · %03d" % entry.track.position)
        dialog.resize(620, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(entry.track.title, "section"))
        layout.addWidget(label(", ".join(entry.track.artists) + " · " + entry.track.album, "muted"))
        layout.addWidget(label("Choose the exact recording. Check version, album, duration and permission.", "muted"))
        combo = QComboBox()
        candidates = []
        for source in self.sources:
            score, confident, reason = match(entry.track, source)
            if score >= 35:
                candidates.append(source)
                combo.addItem(source.title + " · " + ", ".join(source.artists))
        layout.addWidget(combo)
        detail = QTextEdit()
        detail.setReadOnly(True)
        layout.addWidget(detail)
        def update(index):
            if index < 0:
                detail.setPlainText("No compatible source. Attach files with matching metadata or an authorized source manifest. Conflicting ISRCs, recording versions and durations cannot be approved.")
                return
            source = candidates[index]
            score, confident, reason = match(entry.track, source)
            detail.setPlainText("SOURCE\n%s\n\nAlbum: %s\nDuration: %.2f s (Spotify: %.2f s)\nISRC: %s (Spotify: %s)\nVersion: %s\nPermission: %s\n\n%s" % (source.location, source.album or "Unknown", source.duration_ms / 1000, entry.track.duration_ms / 1000, source.isrc or "Unknown", entry.track.isrc or "Unknown", source.version or "From title", source.permission, reason))
        combo.currentIndexChanged.connect(update)
        update(combo.currentIndex())
        confirmation = QCheckBox("I verified this is the same recording and may convert this source.")
        layout.addWidget(confirmation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        confirmation.toggled.connect(lambda checked: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(checked and bool(candidates)))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            entry.source = candidates[combo.currentIndex()]
            entry.approved, entry.status, entry.detail = True, "ready", "Recording reviewed and approved by you"
            self.render()
            self.load_artwork()

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "Save MP3 files", self.destination.text())
        if path:
            self.destination.setText(path)

    def start_export(self):
        if not self.playlist or not self.playlist.entries:
            QMessageBox.information(self, "Export", "Load a playlist with tracks first.")
            return
        if not self.destination.text().strip():
            QMessageBox.warning(self, "Export", "Choose a destination folder.")
            return
        self.cancel_event = threading.Event()
        self.percent = {}
        self.settings.setValue("destination", self.destination.text())
        options = Options(self.destination.text(), [320, 256, 192, 128][self.quality.currentIndex()], self.pattern.text(), self.subfolder.isChecked(), ["skip", "rename", "overwrite"][self.policy.currentIndex()])
        self.set_busy(True, True)
        self.progress_title.setText("Building your music library")
        self.progress_detail.setText("Transfers, conversion and metadata are shown for each track.")
        self.task(lambda signals: Exporter(self.playlist, options, signals.progress.emit, self.cancel_event).run(), self.export_done)

    def update_progress(self, position, percent, status, detail):
        row = position - 1
        self.percent[position] = percent
        if row < self.table.rowCount():
            self.table.item(row, 5).setText(status.capitalize())
            self.table.item(row, 5).setToolTip(detail)
            self.table.cellWidget(row, 6).setValue(percent)
        self.overall.setValue(sum(self.percent.values()) // max(1, len(self.playlist.entries)))
        self.progress_detail.setText("Track %03d · %s" % (position, detail))

    def export_done(self, result):
        self.report, tracks = result
        counts = Counter(t["status"] for t in tracks)
        self.progress_title.setText("Export paused" if counts["cancelled"] else "Your export is complete")
        self.progress_detail.setText("%d saved · %d skipped · %d unavailable · %d failed · %d cancelled" % tuple(counts[s] for s in ("successful", "skipped", "unavailable", "failed", "cancelled")))
        self.report_button.setEnabled(True)
        self.download_button.setText("Retry / resume  ↓")
        self.set_busy(False)
        if not counts["cancelled"]:
            self.overall.setValue(100)
        self.show_report()

    def cancel_export(self):
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.progress_title.setText("Stopping safely…")
        self.progress_detail.setText("Completed MP3s are kept. Partial transfers remain available for retry.")

    def show_report(self):
        if not self.report:
            return
        data = json.loads(self.report.read_text())
        dialog = QDialog(self)
        dialog.setWindowTitle("Listii · Completion report")
        dialog.resize(720, 510)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label(data["playlist"], "section"))
        layout.addWidget(label("  ·  ".join("%d %s" % (v, k) for k, v in data["counts"].items()), "muted"))
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join("%03d  %s — %s\n%s%s" % (r["position"], r["title"], r["status"].upper(), r["detail"], "\n" + r["file"] if r["file"] else "") for r in data["tracks"]))
        layout.addWidget(text)
        layout.addWidget(button("Open destination folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report.parent)))))
        dialog.exec()

    def closeEvent(self, event):
        if self.active_tasks:
            self.cancel_event.set()
            self.progress_title.setText("Please wait for the current operation to stop")
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "listii.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
