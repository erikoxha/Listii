"""Native UI smoke checks; no Spotify credentials or mock exports."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from listii.app import Window, STYLE
from listii.models import Playlist, Entry, Track


def test_empty_and_preview_states():
    application = QApplication.instance() or QApplication([])
    application.setStyleSheet(STYLE)
    window = Window()
    window.show()
    application.processEvents()
    assert window.empty.isVisible()
    assert not window.table.isVisible()
    window.playlist = Playlist("test", "Preview check", [Entry(Track(1, "Test song", ["Artist"], duration_ms=61000))])
    window.render()
    application.processEvents()
    assert window.table.isVisible()
    assert not window.empty.isVisible()
    assert window.table.item(0, 0).text() == "001"
    assert window.table.item(0, 4).text() == "1:01"
    assert window.table.item(0, 5).text() == "Unavailable"
    window.set_busy(True, True)
    assert not window.download_button.isEnabled()
    assert window.cancel_button.isEnabled()
    window.set_busy(False)
    window.close()
