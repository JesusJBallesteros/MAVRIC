"""Help (basics per window), README viewer and About."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .. import about

BASICS = {
    "acquire": """
<h3>Acquire: the basics</h3>
<ol>
<li><b>Connect.</b>
  <ul>
  <li>Pick the RFID port and press <i>Connect RFID</i> (the Arduino resets, ~2 s).</li>
  <li>Pick each camera and press <i>Connect camera</i>.</li>
  <li><i>Connect audio</i> (optional): microphones, the sync pulse input and the speakers.</li>
  </ul></li>
<li><b>ROIs</b> (once per setup, on the first camera).
  <ul>
  <li><i>Draw / reset</i> one ROI per antenna and fit its corners to the place.</li>
  <li>Draw the <i>sync LED box</i> over the sync LED.</li>
  <li><i>Save ROIs to rig</i>.</li>
  </ul></li>
<li><b>Check.</b>
  <ul>
  <li>A place ROI turns the subject's colour while its antenna reads the tag.</li>
  <li>The sync LED box brightness jumps every 2–6 s.</li>
  <li>Audio meters move; the sync pulses are counted.</li>
  </ul></li>
<li><b>Record.</b> Enter a name and notes, then <i>Start recording</i>.
  <ul>
  <li>Everything goes to <code>sessions/&lt;date&gt;_&lt;name&gt;</code>.</li>
  <li>Video is kept raw (lossless chunks); compress it afterwards.</li>
  </ul></li>
<li><b>Optional.</b>
  <ul>
  <li><i>Video settings…</i>: exposure, gain, image size, recording format.</li>
  <li><i>Outputs…</i>: TTL, Intan, MATLAB, sounds, camera clips. Outputs only fire while <i>armed</i>.</li>
  <li><i>Soundboard</i> (audio connected, speakers configured): play any sound from <code>sounds/</code> by hand.</li>
  <li><i>View → All cameras</i>: monitor every camera at once.</li>
  </ul></li>
</ol>
<p><b>Afterwards</b> (command line):</p>
<ul>
<li><code>mavric review &lt;session&gt;</code>: label and curate.</li>
<li><code>mavric analyze &lt;session&gt;</code>: sync check, RFID vs labels, plots.</li>
<li><code>mavric compress &lt;session&gt;</code>: shareable and DeepLabCut copies.</li>
</ul>
<p>Hover over any control for a short description.</p>""",
    "review": """
<h3>Review: the basics</h3>
<p><b>Start.</b> Enter your initials and choose what to review: <i>Video</i> (place labels, RFID curation) and/or
<i>Audio</i> (sound events). Without video the playhead runs on its own clock (← → step 10 ms), the RFID lanes
show as context, and Save writes only the sound events.</p>
<p><b>Place labels.</b> Watch, and mark who is at the selected place from now on.</p>
<table cellpadding="3">
<tr><td><b>↑ ↓</b></td><td>choose the place</td></tr>
<tr><td><b>1–9 / 0</b></td><td>subject 1–9 is at the place from now on / the place is empty</td></tr>
<tr><td><b>Del</b></td><td>remove the change point in effect</td></tr>
<tr><td><b>T</b></td><td>flag "two subjects" here</td></tr>
<tr><td><b>Space</b></td><td>play / pause</td></tr>
<tr><td><b>← →</b></td><td>one frame (Shift: 1 s, Ctrl: 10 s)</td></tr>
<tr><td><b>[ ]</b></td><td>slower / faster</td></tr>
<tr><td><b>N P</b></td><td>next / previous change at this place</td></tr>
</table>
<p>Only time you actually watched (grey band) counts as ground truth.</p>
<p><b>Curate mode</b> shows the RFID lanes.</p>
<table cellpadding="3">
<tr><td><b>D</b></td><td>next disagreement between RFID and labels</td></tr>
<tr><td><b>X / V</b></td><td>mark the RFID bout spurious / valid</td></tr>
</table>
<p><b>Sound events</b> (sessions with audio): waveform and spectrograms under the timeline.</p>
<table cellpadding="3">
<tr><td><b>S / E</b></td><td>start / end an event at the playhead</td></tr>
<tr><td><b>F1–F9</b></td><td>event type</td></tr>
<tr><td><b>Alt+1–9, Alt+0</b></td><td>emitter: subject 1–9 / unknown</td></tr>
<tr><td><b>A</b></td><td>use the suggested emitter</td></tr>
<tr><td><b>Y / R</b></td><td>accept / reject an automatic proposal</td></tr>
<tr><td><b>, .</b></td><td>previous / next event</td></tr>
<tr><td><b>L</b></td><td>listen</td></tr>
<tr><td><b>Shift+Del</b></td><td>delete the selected event</td></tr>
</table>
<p>Sounds the rig played (soundboard, rules) appear by themselves as <i>playback</i> events from
<i>system:&lt;speaker&gt;</i>.</p>
<p><b>Session.</b> <i>Save</i> (Ctrl+S, also every minute), <i>Analyze</i>, and with video <i>Render annotated
video</i> and <i>Compress videos…</i></p>""",
}


def _fit_images(view: QtWidgets.QTextBrowser, base, max_w: int) -> None:
    """Shrink images wider than `max_w` (Qt shows Markdown images at their full size)."""
    doc = view.document()
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                f = frag.charFormat().toImageFormat()
                img = QtGui.QImage(str(base / f.name()))
                if img.width() > max_w:
                    f.setWidth(max_w)
                    f.setHeight(img.height() * max_w / img.width())
                    cur = QtGui.QTextCursor(doc)
                    cur.setPosition(frag.position())
                    cur.setPosition(frag.position() + frag.length(), QtGui.QTextCursor.MoveMode.KeepAnchor)
                    cur.setCharFormat(f)
            it += 1
        block = block.next()


class ReadmeViewer(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        path = about.SOURCE_ROOT / "README.md"
        self.setWindowTitle("README")
        self.resize(1000, 800)
        lay = QtWidgets.QVBoxLayout(self)
        view = QtWidgets.QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setSearchPaths([str(path.parent)])  # images referenced relative to the README
        if path.exists():
            view.setMarkdown(path.read_text(encoding="utf-8"))
            _fit_images(view, path.parent, 920)
        else:
            view.setPlainText(f"README not found at {path}")
        lay.addWidget(view)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Open the README file")
        b.setToolTip("Open README.md with the program your system uses for Markdown files")
        b.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))
        row.addWidget(b)
        b2 = QtWidgets.QPushButton("Open the docs folder")
        b2.setToolTip("Wiring guide, plans, datasheets")
        b2.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(about.SOURCE_ROOT / "docs"))))
        row.addWidget(b2)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)


class HelpDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, context: str = "acquire"):
        super().__init__(parent)
        self.setWindowTitle(f"{about.NAME} help")
        self.resize(620, 640)
        lay = QtWidgets.QVBoxLayout(self)
        text = QtWidgets.QTextBrowser()
        text.setHtml(BASICS.get(context, BASICS["acquire"]))
        lay.addWidget(text)
        row = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Open the README (full manual)")
        b.setToolTip("The user manual with screenshots, installation and all commands")
        b.clicked.connect(lambda: ReadmeViewer(self).exec())
        row.addWidget(b)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)


class AboutDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {about.NAME}")
        self.resize(520, 380)
        b = about.build_info()
        commit = (f"commit <code>{b['commit']}</code>" + (f" ({b['date']})" if b["date"] else "")
                  + (" · with local changes" if b["modified"] else "")) if b["commit"] else "commit unknown"
        lay = QtWidgets.QVBoxLayout(self)
        lab = QtWidgets.QLabel(
            f"<h2>{about.NAME} {b['version']}</h2><p><i>{about.FULL_NAME}</i></p>"
            f"<p>{about.DESCRIPTION}.</p>"
            f"<p>{commit}<br><small>{about.environment()}</small></p>"
            f"<p>© {about.COPYRIGHT_YEAR} {about.AUTHOR}<br>{about.AFFILIATION}</p>"
            f"<p>Released under the <b>{about.LICENSE} licence</b>.<br>"
            f"<small>The RFID firmware derived from MultiRFID is GPL-3.0-or-later. Dependencies keep their own "
            f"licences (Qt/PySide6 LGPL-3.0; FFmpeg with x264 in PyAV; FLIR and Basler SDKs proprietary). "
            f"See THIRD_PARTY.md.</small></p>")
        lab.setWordWrap(True)
        lab.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lab.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        lay.addWidget(lab)
        lay.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        for label, fname, tip in (("Licence", "LICENSE", "The MIT licence text"),
                                  ("Third-party", "THIRD_PARTY.md", "Dependencies and their licences")):
            btn = QtWidgets.QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, f=fname: self._show_file(f))
            row.addWidget(btn)
        copy = QtWidgets.QPushButton("Copy version info")
        copy.setToolTip("Copy version, commit and environment, e.g. for a bug report or a methods section")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(
            f"{about.version_string()}\n{about.environment()}"))
        row.addWidget(copy)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)

    def _show_file(self, name: str) -> None:
        p = about.SOURCE_ROOT / name
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(name)
        dlg.resize(640, 520)
        lay = QtWidgets.QVBoxLayout(dlg)
        t = QtWidgets.QTextBrowser()
        txt = p.read_text(encoding="utf-8") if p.exists() else f"{p} not found"
        t.setMarkdown(txt) if name.endswith(".md") else t.setPlainText(txt)
        lay.addWidget(t)
        dlg.exec()


def help_about_row(parent, context: str) -> QtWidgets.QHBoxLayout:
    """'Help' and 'About' buttons for a window's side panel."""
    row = QtWidgets.QHBoxLayout()
    h = QtWidgets.QPushButton("Help")
    h.setToolTip("The basics of this window, and the full manual (README)")
    h.clicked.connect(lambda: HelpDialog(parent, context).exec())
    a = QtWidgets.QPushButton("About")
    a.setToolTip("Version, commit, author and licence")
    a.clicked.connect(lambda: AboutDialog(parent).exec())
    for b in (h, a):
        b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        row.addWidget(b)
    row.addStretch(1)
    return row
