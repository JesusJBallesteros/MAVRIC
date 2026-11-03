"""Side panels as docks: each with an icon, collapsible to its title bar, movable and floatable. The arrangement is
saved per rig (paths.gui_layout_file)."""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

LAYOUT_VERSION = 1  # bump when the panels change, so an old saved arrangement is not applied
MAX_SIZE = 16777215  # QWIDGETSIZE_MAX


def glyph_icon(glyph: str, color: str, size: int = 32) -> QtGui.QIcon:
    """A white symbol on a coloured rounded square: the panels tell apart at a glance, also when collapsed."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(color))
    p.drawRoundedRect(QtCore.QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    f = QtGui.QFont()
    f.setFamilies(["Segoe UI Symbol", "Segoe UI", "DejaVu Sans"])
    f.setPixelSize(int(size * 0.62))
    p.setFont(f)
    p.setPen(QtGui.QColor("white"))
    p.drawText(QtCore.QRectF(0, 0, size, size), QtCore.Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    return QtGui.QIcon(pm)


class _TitleBar(QtWidgets.QWidget):
    """Collapse arrow, icon and title; float and hide buttons. Dragging the title moves the panel; a double click
    floats it or docks it back."""

    def __init__(self, dock: Panel, tip: str):
        super().__init__(dock)
        self.setObjectName("panelTitle")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("#panelTitle { background: palette(midlight); border-radius: 3px; }")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(2, 1, 2, 1)
        h.setSpacing(3)
        self.arrow = self._button("▾", "Collapse the panel to its title bar, or open it again", dock.toggle_collapsed)
        self.icon = self._button("", f"{dock.windowTitle()}: collapse or open (or the arrow)", dock.toggle_collapsed)
        self.icon.setIcon(dock.icon)
        self.icon.setIconSize(QtCore.QSize(18, 18))
        self.title = QtWidgets.QLabel(f"<b>{dock.windowTitle()}</b>")
        self.title.setToolTip(tip + "\n\nDrag the title to move the panel; double-click it to float or dock it.")
        st = self.style()
        self.float_b = self._button("", "Float the panel in its own window, or dock it back", dock.toggle_floating)
        self.float_b.setIcon(st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TitleBarNormalButton))
        self.close_b = self._button("", "Hide the panel (the Panels menu shows it again)", dock.close)
        self.close_b.setIcon(st.standardIcon(QtWidgets.QStyle.StandardPixmap.SP_TitleBarCloseButton))
        for w in (self.arrow, self.icon, self.title):
            h.addWidget(w)
        h.addStretch(1)
        h.addWidget(self.float_b)
        h.addWidget(self.close_b)

    @staticmethod
    def _button(text: str, tip: str, fn) -> QtWidgets.QToolButton:
        b = QtWidgets.QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        b.clicked.connect(fn)
        return b


class Panel(QtWidgets.QDockWidget):
    """A side panel: icon and title, collapsible to its title bar; drag the title to move it, or float it."""

    def __init__(self, name: str, title: str, glyph: str, color: str, body: QtWidgets.QWidget, tip: str = ""):
        super().__init__(title)
        self.setObjectName(name)  # the key in the saved arrangement
        self.icon = glyph_icon(glyph, color)
        self.setWindowIcon(self.icon)  # on its tab when panels are stacked on each other
        self.body = body
        self._holder = QtWidgets.QWidget()  # stays visible when collapsed (0 high), so the panel keeps its width
        lay = QtWidgets.QVBoxLayout(self._holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(body)
        self.setWidget(self._holder)
        self.collapsed = False
        self.bar = _TitleBar(self, tip)
        self.setTitleBarWidget(self.bar)
        self.toggleViewAction().setIcon(self.icon)
        self.toggleViewAction().setToolTip(tip)
        self.topLevelChanged.connect(self._fit)

    def set_collapsed(self, on: bool) -> None:
        """Collapsed: only the title bar; the panels around it take the space."""
        self.collapsed = on
        self.body.setVisible(not on)
        self._holder.setMaximumHeight(0 if on else MAX_SIZE)
        self.bar.arrow.setText("▸" if on else "▾")
        self._fit()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self.collapsed)

    def toggle_floating(self) -> None:
        self.setFloating(not self.isFloating())

    def _fit(self, *_) -> None:
        if self.isFloating():
            self.resize(self.width(), self.minimumSizeHint().height() if self.collapsed else
                        max(self.height(), self.sizeHint().height()))


class PanelLayout:
    """Where a window's panels are, which are collapsed, and the window's size: saved per rig, restored at the next
    start. Holds no reference to the window (a cycle would leave a closed window for the garbage collector)."""

    def __init__(self, path: str | Path | None, key: str = "acquire"):
        self.path = Path(path) if path else None
        self.key = key

    def _settings(self) -> QtCore.QSettings:
        return QtCore.QSettings(str(self.path), QtCore.QSettings.Format.IniFormat)

    def save(self, win: QtWidgets.QMainWindow, panels: dict[str, Panel]) -> None:
        if self.path is None or not self.path.parent.is_dir():
            return
        s = self._settings()
        s.setValue(f"{self.key}/geometry", win.saveGeometry())
        s.setValue(f"{self.key}/state", win.saveState(LAYOUT_VERSION))
        s.setValue(f"{self.key}/collapsed", ",".join(n for n, p in panels.items() if p.collapsed))
        s.sync()

    def restore(self, win: QtWidgets.QMainWindow, panels: dict[str, Panel]) -> bool:
        """The saved arrangement, if there is one for this version of the panels."""
        if self.path is None or not self.path.is_file():
            return False
        s = self._settings()
        geo, state = s.value(f"{self.key}/geometry"), s.value(f"{self.key}/state")
        if state is None or not win.restoreState(state, LAYOUT_VERSION):
            return False
        if geo is not None:
            win.restoreGeometry(geo)
        collapsed = set(str(s.value(f"{self.key}/collapsed", "") or "").split(","))
        for n, p in panels.items():
            p.set_collapsed(n in collapsed)
        return True
