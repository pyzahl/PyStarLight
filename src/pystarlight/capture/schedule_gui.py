"""The DSLR window's Program tab: edit, check and run photo programs (capture/schedule.py) on the
DSLR (capture/dslr.py), on the GPS UTC clock.

NEW CODE, the editor ported from the owner's SoFiMagic Sony app (SettingsActivity) and laid out
as its camera screen: the reference times side by side (CT1 .. CT4), a phase slider, the phase on
one line (name, start reference + offset "to" end reference + offset, # shots), and its exposures
as a grid, one column per exposure and the rows CF (drive), BT (burst s / bracket code), ISO, F,
t (shutter). What a computer adds: any reference times (not only the contacts) and NOW, phases
added / removed / moved, `every` for a fixed interval, the connected camera's choices (else the
app's lists) in the grid's drop-downs, the timeline (start, end, blocks, photos, a block's time
against the interval, warnings) as you type, SoFiMagic XML and TOML read and written, a test run
(the whole program moved to start in 15 s) and the run's status.
"""
import html
import os
import re
from datetime import datetime, timezone

from . import schedule as sc
from .qt import QtCore, QtGui, QtWidgets

TEST_LEAD_S = 15.0
BAD = QtGui.QColor("#ffcdd2")
FILES = (("files + FITS", "fits"), ("camera files", "files"), ("on the card", "card"))
ROWS = ("CF", "BT", "ISO", "F", "t")          # the exposure grid's rows, as on the camera
ROW_TIPS = ("drive: S single, C / M / L burst, B bracket", "C / M / L: burst seconds; B: bracket code "
            "(10: 1 EV x 3, 11: 1 EV x 5, 20: 2 EV x 3 ...)", "ISO", "f-number ('-': as the lens is)",
            "shutter: 1/4000, 0.5, 2, 30 (bulb beyond the camera's longest)")
COL_W = 66
EDITOR_MIN_W = 92            # an exposure cell being edited: wider than its column


def qdate(d):
    """A QDate as a datetime.date (PySide6: toPython, PyQt5: toPyDate)."""
    return d.toPython() if hasattr(d, "toPython") else d.toPyDate()


def time_text(ns, local):
    t = datetime.fromtimestamp(ns / sc.NS, timezone.utc)
    t = t.astimezone() if local else t
    return t.strftime("%H:%M:%S") + (f".{t.microsecond // 1000:03d}" if t.microsecond else "")


def tool(text, slot, tip=""):
    b = QtWidgets.QToolButton()
    b.setText(text)
    b.setToolTip(tip)
    b.setAutoRaise(True)
    b.clicked.connect(slot)
    return b


def cell_texts(e):
    """The grid's five cells of an exposure."""
    shutter = sc.shutter_text(e.shutter, "") if e.shutter else ""
    return [e.mode, f"{e.burst:g}" if e.mode != "S" else "", str(e.iso or ""),
            f"{e.fnumber:g}" if e.fnumber else "-", shutter]


def camera_list(meaning, choices):
    """A camera's choices for the grid's drop-down: as the SoFiMagic app lists them, by value,
    fastest / smallest first, one per value. Shutter speeds in one notation (Sony's '32/10' ->
    '3.2', '1/1600' stays), no Bulb; plain ISOs only (no Auto, no multi-frame modes); f-numbers
    without 'f/' (no 'f/0': a lens without contacts)."""
    out = {}
    for c in choices:
        text = str(c).strip()
        try:
            if meaning == "shutter":
                value = sc.shutter_seconds(text)
                out.setdefault(round(value, 9), sc.shutter_text(value, ""))
            elif meaning == "iso" and re.fullmatch(r"\d+", text):
                out.setdefault(int(text), text)
            elif meaning == "aperture":
                value = float(re.sub(r"^[fF]/?", "", text))
                if value > 0:
                    out.setdefault(value, f"{value:g}")
        except ValueError:
            continue                        # Bulb, Auto, 'implicit auto' ...
    return [out[k] for k in sorted(out)]


class ChoiceDelegate(QtWidgets.QStyledItemDelegate):
    """The grid's editor: a drop-down of the row's choices (typing allowed but in CF)."""

    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel

    def createEditor(self, parent, option, index):
        row = index.row()
        if row == 1:
            return QtWidgets.QLineEdit(parent)
        c = QtWidgets.QComboBox(parent)
        c.setEditable(row != 0)
        c.addItems({0: list(sc.MODES), 2: self.panel.choices("iso"),
                    3: ["-"] + self.panel.choices("aperture"), 4: self.panel.choices("shutter")}[row])
        view = c.view()                     # the list as wide as its longest entry, never '1…0'
        view.setTextElideMode(QtCore.Qt.ElideNone)
        view.setMinimumWidth(view.sizeHintForColumn(0) + 2 * view.frameWidth()
                             + view.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent) + 8)
        return c

    def updateEditorGeometry(self, editor, option, index):
        """The editor over its cell, widened to EDITOR_MIN_W (a column is narrower)."""
        r = QtCore.QRect(option.rect)
        if r.width() < EDITOR_MIN_W:
            r.setWidth(EDITOR_MIN_W)
        editor.setGeometry(r)

    def setEditorData(self, editor, index):
        text = index.data() or ""
        if isinstance(editor, QtWidgets.QComboBox):
            i = editor.findText(text)
            if i >= 0:
                editor.setCurrentIndex(i)
            elif editor.isEditable():
                editor.setEditText(text)
        else:
            editor.setText(text)

    def setModelData(self, editor, model, index):
        combo = isinstance(editor, QtWidgets.QComboBox)
        model.setData(index, editor.currentText() if combo else editor.text())


class ProgramPanel(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.win = window                    # the DSLR window: thread, clock, folder, FITS header
        self.prog = sc.load_sofimagic(sc.DEFAULT_SOFIMAGIC)
        self.path = ""
        self.runner = None
        self.loading = False
        self.error = ""
        self.time_cols = []                  # [(name edit, time edit)] of the reference times
        self.index = -1                      # the phase shown

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(6)

        # ---- file ----
        top = QtWidgets.QHBoxLayout()
        new = QtWidgets.QToolButton()
        new.setText("New")
        new.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        menu = QtWidgets.QMenu(new)
        menu.addAction("Eclipse (SoFiMagic's default program)", self.on_new_eclipse)
        menu.addAction("Sequence from now", self.on_new_sequence)
        new.setMenu(menu)
        top.addWidget(new)
        top.addWidget(tool("Open…", self.on_open, "SoFiMagic XML (the Sony app's SOFIPRG.XML) or TOML"))
        top.addWidget(tool("Save", self.on_save, "XML when it was XML and fits SoFiMagic, else TOML"))
        top.addWidget(tool("Save as…", lambda: self.on_save(ask=True)))
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("program name")
        self.name_edit.textEdited.connect(self.changed)
        top.addWidget(self.name_edit, 1)
        v.addLayout(top)
        self.file_label = QtWidgets.QLabel("")
        self.file_label.setStyleSheet("color: gray;")
        self.file_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        v.addWidget(self.file_label)

        # ---- times: date, base, the references side by side ----
        self.times_box = QtWidgets.QGridLayout()
        self.times_box.setHorizontalSpacing(6)
        self.times_box.setVerticalSpacing(1)
        self.date_edit = QtWidgets.QDateEdit(calendarPopup=True, displayFormat="yyyy-MM-dd")
        self.date_edit.setToolTip("the date of the times (SoFiMagic XML has none)")
        self.date_edit.dateChanged.connect(self.changed)
        self.base_combo = QtWidgets.QComboBox()
        self.base_combo.addItems(["UTC", "local"])
        self.base_combo.setToolTip("the times as typed: UTC, or the computer's time zone (the Sony app "
                                   "used the camera's local clock)")
        self.base_combo.currentIndexChanged.connect(self.on_base)
        self.times_box.addWidget(self.date_edit, 0, 0)
        self.times_box.addWidget(self.base_combo, 1, 0)
        self.c0_label = QtWidgets.QLabel("")
        self.c0_label.setStyleSheet("color: gray;")
        self.c0_label.setToolTip("C0: the maximum, (C2 + C3) / 2")
        self.time_add = tool("+", self.on_add_time, "another reference time (e.g. T0, an event's time)")
        self.time_del = tool("−", self.on_remove_time,
                             "remove the reference time being edited (else the last)")
        tw = QtWidgets.QWidget()
        tw.setLayout(self.times_box)
        v.addWidget(tw)

        # ---- the phase: slider, then one line ----
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("Phase"))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.valueChanged.connect(self.show_phase)
        prow.addWidget(self.slider, 1)
        self.phase_count = QtWidgets.QLabel("")
        self.phase_count.setMinimumWidth(44)
        prow.addWidget(self.phase_count)
        for text, slot, tip in (("+", self.on_add_phase, "a phase after this one"),
                                ("−", self.on_remove_phase, "remove this phase"),
                                ("◀", lambda: self.on_move_phase(-1), "move this phase earlier"),
                                ("▶", lambda: self.on_move_phase(1), "move this phase later")):
            prow.addWidget(tool(text, slot, tip))
        v.addLayout(prow)

        line = QtWidgets.QHBoxLayout()
        line.setSpacing(4)
        self.phase_name = QtWidgets.QLineEdit()
        self.phase_name.setStyleSheet("font-weight: bold;")
        self.phase_name.setMinimumWidth(64)
        self.phase_name.textEdited.connect(self.phase_changed)
        self.start_ref, self.end_ref = QtWidgets.QComboBox(), QtWidgets.QComboBox()
        self.start_off, self.end_off = (QtWidgets.QDoubleSpinBox(minimum=-864000, maximum=864000, decimals=1,
                                                                 suffix=" s") for _ in range(2))
        self.shots_spin = QtWidgets.QSpinBox(minimum=-1, maximum=99999, prefix="# ")
        self.shots_spin.setToolTip("N: the exposures N times, spread evenly from start to end (with every: "
                                   "every that many s); -1: back to back until the end")
        self.every_spin = QtWidgets.QDoubleSpinBox(minimum=0, maximum=86400, decimals=1, suffix=" s",
                                                   prefix="every ", specialValueText="spread")
        self.every_spin.setToolTip("seconds from one set of exposures to the next; 'spread': N spread "
                                   "from start to end")
        for w in (self.start_ref, self.end_ref):
            w.activated.connect(self.phase_changed)
        for w in (self.start_off, self.end_off, self.every_spin):
            w.valueChanged.connect(self.phase_changed)
        self.shots_spin.valueChanged.connect(self.phase_changed)
        for w in (self.start_ref, self.end_ref):
            w.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            w.setMinimumContentsLength(4)
        widths = ((self.start_off, 78), (self.end_off, 78), (self.shots_spin, 66), (self.every_spin, 96))
        for w, width in widths:
            w.setMaximumWidth(width)
        for w in (self.phase_name, self.start_ref, self.start_off, QtWidgets.QLabel("to"), self.end_ref,
                  self.end_off, self.shots_spin, self.every_spin):
            line.addWidget(w)
        line.setStretch(0, 1)
        v.addLayout(line)

        # ---- the exposures: a grid, one column each ----
        grid = QtWidgets.QHBoxLayout()
        self.grid = QtWidgets.QTableWidget(len(ROWS), 0)
        self.grid.setVerticalHeaderLabels(list(ROWS))
        for r, tip in enumerate(ROW_TIPS):
            self.grid.verticalHeaderItem(r).setToolTip(tip)
        self.grid.horizontalHeader().setDefaultSectionSize(COL_W)
        self.grid.horizontalHeader().setMinimumSectionSize(40)
        self.grid.verticalHeader().setDefaultSectionSize(22)
        self.grid.horizontalHeader().setVisible(False)
        self.grid.setItemDelegate(ChoiceDelegate(self))
        self.grid.setEditTriggers(QtWidgets.QAbstractItemView.AllEditTriggers)
        bar = self.grid.style().pixelMetric(QtWidgets.QStyle.PM_ScrollBarExtent)
        self.grid.setFixedHeight(22 * len(ROWS) + bar + 2 * self.grid.frameWidth() + 2)
        self.grid.itemChanged.connect(self.exposures_changed)
        grid.addWidget(self.grid, 1)
        gb = QtWidgets.QVBoxLayout()
        gb.setSpacing(0)
        for text, slot, tip in (("+", self.on_add_exposure, "an exposure after the one selected (a copy)"),
                                ("−", self.on_remove_exposure, "remove the exposure selected"),
                                ("◀", lambda: self.on_move_exposure(-1), "move it earlier"),
                                ("▶", lambda: self.on_move_exposure(1), "move it later")):
            gb.addWidget(tool(text, slot, tip))
        gb.addStretch(1)
        grid.addLayout(gb)
        v.addLayout(grid)

        # ---- timeline ----
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Phase", "Start", "End", "Blocks", "Photos", "Block", "Notes"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(110)
        self.tree.currentItemChanged.connect(
            lambda item, _: item is not None and self.slider.setValue(self.tree.indexOfTopLevelItem(item)))
        v.addWidget(self.tree, 1)

        # ---- run ----
        rrow = QtWidgets.QHBoxLayout()
        self.files_combo = QtWidgets.QComboBox()
        for text, data in FILES:
            self.files_combo.addItem(text, data)
        self.files_combo.setToolTip("files + FITS: downloaded, a FITS beside them (in the background); "
                                    "camera "
                                    "files: downloaded only (faster); on the card: the release only, where "
                                    "the camera has a card target (a Sony in PC Remote mode has none)")
        self.files_combo.activated.connect(self.changed)
        self.mf_check = QtWidgets.QCheckBox("MF")
        self.mf_check.setChecked(True)
        self.mf_check.setToolTip("manual focus at the start (as SoFiMagic's MF): focus beforehand")
        self.start_combo = QtWidgets.QComboBox()
        self.start_combo.setToolTip(f"when Run (and the dry runs) start: as programmed, or the whole program "
                                    f"moved so that it (or a reference time, e.g. C1, C2) comes in "
                                    f"{TEST_LEAD_S:g} s; phases before that reference are over and skipped")
        self.dry_button = QtWidgets.QToolButton()
        self.dry_button.setText("Dry run")
        self.dry_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.dry_button.setToolTip("the program without the camera: every photo with its time, as a list "
                                   "(Markdown) and CSV in the recording folder")
        dry = QtWidgets.QMenu(self.dry_button)
        dry.addAction("Fast-forward (instant)", lambda: self.on_dry_run(fast=True))
        dry.addAction("On time (simulated camera)", lambda: self.on_dry_run(fast=False))
        self.dry_button.setMenu(dry)
        self.run_button = QtWidgets.QPushButton("Run")
        self.run_button.setStyleSheet("font-weight: bold;")
        self.run_button.clicked.connect(self.on_run)
        rrow.addWidget(self.files_combo)
        rrow.addWidget(self.mf_check)
        rrow.addStretch(1)
        rrow.addWidget(QtWidgets.QLabel("Start"))
        rrow.addWidget(self.start_combo)
        rrow.addWidget(self.dry_button)
        rrow.addWidget(self.run_button)
        v.addLayout(rrow)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(QtCore.Qt.RichText)
        self.status_label.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        self.status_label.setOpenExternalLinks(True)
        v.addWidget(self.status_label)
        self.dry_path = ""

        self.refresh = QtCore.QTimer(self, singleShot=True, interval=300, timeout=self.update_timeline)
        self.set_program(self.prog, "SoFiMagic's default eclipse program: set the contact times and the date")

    # ---- the program into the widgets ----
    def local(self):
        return self.base_combo.currentIndex() == 1

    def set_program(self, prog, label=""):
        self.loading = True
        self.prog = prog
        self.name_edit.setText(prog.name)
        times = [(k, v) for k, v in prog.times.items() if k not in ("NOW", "C0")]
        if times:
            first = datetime.fromtimestamp(times[0][1] / sc.NS, timezone.utc)
            first = first.astimezone() if self.local() else first
            self.date_edit.setDate(QtCore.QDate(first.year, first.month, first.day))
        else:
            self.date_edit.setDate(QtCore.QDate.currentDate())
        self.set_times([(k, time_text(ns, self.local())) for k, ns in times])
        self.files_combo.setCurrentIndex(max(0, self.files_combo.findData(prog.files)))
        self.slider.setRange(0, max(0, len(prog.phases) - 1))
        self.loading = False
        self.file_label.setText(label or prog.source)
        self.error = "; ".join(prog.notes) if prog.notes else ""
        self.index = -1
        self.slider.setValue(0)
        self.show_phase(0)
        self.update_timeline()

    def set_times(self, items):
        """The reference times as columns: name above, time below; C0 after them when known."""
        for name_edit, time_edit in self.time_cols:
            name_edit.deleteLater()
            time_edit.deleteLater()
        self.time_cols = []
        for k, text in items:
            self.add_time_col(k, text)
        self.place_time_tools()

    def add_time_col(self, name, text):
        name_edit, time_edit = QtWidgets.QLineEdit(name), QtWidgets.QLineEdit(text)
        name_edit.setAlignment(QtCore.Qt.AlignCenter)
        name_edit.setFrame(False)
        name_edit.setStyleSheet("font-weight: bold; background: transparent;")
        name_edit.setMaximumWidth(92)
        time_edit.setAlignment(QtCore.Qt.AlignCenter)
        time_edit.setMaximumWidth(92)
        time_edit.setMinimumWidth(70)
        time_edit.setToolTip("HH:MM:SS[.fff] on the date, or ISO (2026-08-12T17:46:20Z)")
        for e in (name_edit, time_edit):
            e.textEdited.connect(self.changed)
        col = len(self.time_cols) + 1
        self.times_box.addWidget(name_edit, 0, col)
        self.times_box.addWidget(time_edit, 1, col)
        self.time_cols.append((name_edit, time_edit))

    def place_time_tools(self):
        n = len(self.time_cols) + 1
        self.times_box.addWidget(self.c0_label, 1, n)
        self.times_box.addWidget(self.time_add, 0, n + 1)
        self.times_box.addWidget(self.time_del, 1, n + 1)
        self.times_box.setColumnStretch(n + 2, 1)

    def references(self):
        names = [n.text().strip() for n, _ in self.time_cols if n.text().strip()]
        if "C2" in names and "C3" in names and "C0" not in names:
            names.append("C0")
        return names + ["NOW"]

    def read_times(self):
        """{name: UTC ns} of the columns (+ C0 from C2, C3); cells that do not read in red."""
        day = qdate(self.date_edit.date())
        times, bad = {}, []
        for name_edit, time_edit in self.time_cols:
            name = name_edit.text().strip()
            try:
                times[name] = sc.parse_time(time_edit.text(), day, self.local())
                time_edit.setStyleSheet("")
            except Exception:
                time_edit.setStyleSheet(f"background: {BAD.name()};")
                bad.append(name or "?")
        if "C2" in times and "C3" in times and "C0" not in times:
            times["C0"] = (times["C2"] + times["C3"]) // 2
            self.c0_label.setText(f"C0 {time_text(times['C0'], self.local())}")
        else:
            self.c0_label.setText("")
        times["NOW"] = 0
        return times, bad

    # ---- one phase ----
    def phase(self):
        return self.prog.phases[self.index] if 0 <= self.index < len(self.prog.phases) else None

    def show_phase(self, index):
        self.index = index if 0 <= index < len(self.prog.phases) else -1
        p = self.phase()
        self.phase_count.setText(f"{self.index + 1} / {len(self.prog.phases)}" if p else "0 / 0")
        self.loading = True
        refs = self.references()
        for combo, ref, none_ok in ((self.start_ref, p.start if p else None, False),
                                    (self.end_ref, p.end if p else None, True)):
            combo.clear()
            if none_ok:
                combo.addItem("—", None)
            for n in refs:
                combo.addItem(n, n)
            if ref is not None and combo.findData(ref[0]) < 0:
                combo.addItem(ref[0], ref[0])
            combo.setCurrentIndex(max(0, combo.findData(ref[0] if ref else None)))
        if p is not None:
            self.phase_name.setText(p.name)
            self.start_off.setValue(p.start[1])
            self.end_off.setValue(p.end[1] if p.end else 0.0)
            self.end_off.setEnabled(p.end is not None)
            self.shots_spin.setValue(p.shots)
            self.every_spin.setValue(p.every or 0.0)
        self.grid.setColumnCount(0)
        for e in (p.exposures if p else []):
            self.add_exposure_col(e)
        for w in (self.phase_name, self.start_ref, self.end_ref, self.start_off, self.shots_spin,
                  self.every_spin, self.grid):
            w.setEnabled(p is not None)
        self.loading = False
        if p is not None and self.tree.topLevelItemCount() > self.index:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(self.tree.topLevelItem(self.index))
            self.tree.blockSignals(False)

    def phase_changed(self, *_):
        p = self.phase()
        if p is None or self.loading:
            return
        p.name = self.phase_name.text().strip() or p.name
        p.start = (self.start_ref.currentData() or "NOW", self.start_off.value())
        end = self.end_ref.currentData()
        p.end = (end, self.end_off.value()) if end else None
        p.shots = self.shots_spin.value()
        p.every = self.every_spin.value() or None
        self.end_off.setEnabled(end is not None)
        self.changed()

    # ---- exposures ----
    def choices(self, meaning):
        """The camera's choices when one is connected, else the SoFiMagic app's lists."""
        t = self.win.thread
        cam = (t.settings.get("choices") or {}).get(meaning) if t is not None and t.dslr is not None else None
        if cam:
            return camera_list(meaning, cam)
        return {"iso": [str(i) for i in sc.STD_ISOS], "aperture": [f"{a:g}" for a in sc.STD_APERTURES],
                "shutter": list(sc.STD_SHUTTERS)}[meaning]

    def add_exposure_col(self, e, at=None):
        g = self.grid
        c = g.columnCount() if at is None else at
        was = self.loading
        self.loading = True
        g.insertColumn(c)
        for r, text in enumerate(cell_texts(e)):
            item = QtWidgets.QTableWidgetItem(text)
            item.setTextAlignment(QtCore.Qt.AlignCenter)
            g.setItem(r, c, item)
        self.loading = was

    def read_exposures(self):
        """[Exposure] of the grid; columns that do not read are marked red (and left out)."""
        out, g = [], self.grid
        self.loading = True
        for c in range(g.columnCount()):
            cells = [g.item(r, c) for r in range(len(ROWS))]
            mode, burst, iso, f, shutter = ((x.text() if x else "").strip() for x in cells)
            try:
                mode = (mode or "S").upper()[:1]
                if mode not in sc.MODES:
                    raise ValueError(f"drive {mode}: one of {', '.join(sc.MODES)}")
                b = float(burst or 0) if mode != "S" else 0.0
                if mode == "B" and int(b) not in sc.BRACKETS:
                    raise ValueError(f"bracket code {b:g}: one of {sorted(sc.BRACKETS)}")
                fv = f.replace("f/", "")
                out.append(sc.Exposure(sc.shutter_seconds(shutter) if shutter else None,
                                       int(iso) if iso else None, float(fv) if fv and fv != "-" else None,
                                       mode, b))
                for x in cells:
                    if x:
                        x.setBackground(QtGui.QBrush())
            except Exception as ex:
                for x in cells:
                    if x:
                        x.setBackground(BAD)
                self.error = f"exposure {c + 1}: {ex}"
        self.loading = False
        return out

    def exposures_changed(self, *_):
        p = self.phase()
        if p is None or self.loading:
            return
        self.error = ""
        p.exposures = self.read_exposures()
        self.changed()

    def on_add_exposure(self):
        p = self.phase()
        if p is None:
            return
        c = self.grid.currentColumn()
        src = p.exposures[c] if 0 <= c < len(p.exposures) else (p.exposures[-1] if p.exposures else None)
        at = len(p.exposures) if c < 0 else c + 1
        self.add_exposure_col(src or sc.Exposure(1 / 1000, 100), at)
        self.grid.setCurrentCell(0, at)
        self.exposures_changed()

    def on_remove_exposure(self):
        c = self.grid.currentColumn()
        if c >= 0:
            self.grid.removeColumn(c)
            self.exposures_changed()

    def on_move_exposure(self, d):
        p, c = self.phase(), self.grid.currentColumn()
        if p is None or not (0 <= c < len(p.exposures)) or not (0 <= c + d < len(p.exposures)):
            return
        p.exposures[c], p.exposures[c + d] = p.exposures[c + d], p.exposures[c]
        self.show_phase(self.index)
        self.grid.setCurrentCell(0, c + d)
        self.changed()

    # ---- phases and times: add, remove, move ----
    def on_add_phase(self):
        base = self.phase()
        new = sc.Phase("new phase", base.end if base and base.end else ("NOW", 10.0), None, 1, None,
                       [sc.Exposure(1 / 1000, 100)])
        at = self.index + 1
        self.prog.phases.insert(at, new)
        self.slider.setRange(0, len(self.prog.phases) - 1)
        self.slider.setValue(at)
        self.show_phase(at)
        self.changed()

    def on_remove_phase(self):
        if self.phase() is not None:
            del self.prog.phases[self.index]
            self.slider.setRange(0, max(0, len(self.prog.phases) - 1))
            self.show_phase(min(self.index, len(self.prog.phases) - 1))
            self.changed()

    def on_move_phase(self, d):
        i, ph = self.index, self.prog.phases
        if 0 <= i < len(ph) and 0 <= i + d < len(ph):
            ph[i], ph[i + d] = ph[i + d], ph[i]
            self.slider.setValue(i + d)
            self.show_phase(i + d)
            self.changed()

    def on_add_time(self):
        names, n = set(self.references()), 0
        while f"T{n}" in names:
            n += 1
        self.add_time_col(f"T{n}", datetime.now(timezone.utc).strftime("%H:%M:%S"))
        self.place_time_tools()
        self.changed()

    def on_remove_time(self):
        if not self.time_cols:
            return
        focus = QtWidgets.QApplication.focusWidget()
        k = next((i for i, (a, b) in enumerate(self.time_cols) if focus in (a, b)), len(self.time_cols) - 1)
        items = [(a.text(), b.text()) for i, (a, b) in enumerate(self.time_cols) if i != k]
        self.set_times(items)
        self.changed()

    def on_base(self):
        """UTC <-> local: the times keep their instant, shown in the other base."""
        if self.loading:
            return
        self.loading = True
        self.base_combo.blockSignals(True)
        self.base_combo.setCurrentIndex(1 - self.base_combo.currentIndex())
        prog = self.collect(quiet=True)                         # read in the old base
        self.base_combo.setCurrentIndex(1 - self.base_combo.currentIndex())
        self.base_combo.blockSignals(False)
        self.loading = False
        index = self.index
        self.set_program(prog, self.file_label.text())
        self.slider.setValue(max(0, index))

    # ---- the whole program ----
    def changed(self, *_):
        if not self.loading:
            self.refresh.start()

    def collect(self, quiet=False):
        """The program as edited (times re-read)."""
        times, bad = self.read_times()
        if bad and not quiet:
            self.error = f"times not read: {', '.join(bad)}"
        return sc.Program(self.name_edit.text().strip() or "photo program", times, self.prog.phases,
                          self.files_combo.currentData(), self.prog.source, list(self.prog.notes))

    def update_timeline(self):
        prog = self.collect()
        now = self.win.clock()
        local = self.local()
        self.tree.blockSignals(True)
        self.tree.clear()
        for row in sc.timeline(prog.at(now), now):
            item = QtWidgets.QTreeWidgetItem([
                row.phase.name, time_text(row.start_ns, local) if row.start_ns else "?",
                time_text(row.end_ns, local) if row.end_ns else "-",
                "back to back" if row.blocks is None else str(row.blocks), str(row.photos),
                f"{row.block_s:.1f} s", "; ".join(row.warnings)])
            if row.warnings:
                item.setForeground(6, QtGui.QBrush(QtGui.QColor("#e65100")))
            self.tree.addTopLevelItem(item)
        for c in range(6):
            self.tree.resizeColumnToContents(c)
        if 0 <= self.index < self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(self.index))
        self.tree.blockSignals(False)
        self.prog = prog
        self.phase_count.setText(f"{self.index + 1} / {len(prog.phases)}" if self.phase() else "0 / 0")
        self.fill_start()

    def fill_start(self):
        """The Start choices: as programmed, the program, or a reference time in TEST_LEAD_S."""
        items = [("as programmed", None), (f"program in {TEST_LEAD_S:g} s", "")]
        items += [(f"{r} in {TEST_LEAD_S:g} s", r) for r in self.references() if r != "NOW"]
        if [self.start_combo.itemData(i) for i in range(self.start_combo.count())] == [d for _, d in items]:
            return
        keep = self.start_combo.currentData()
        self.start_combo.clear()
        for text, data in items:
            self.start_combo.addItem(text, data)
        self.start_combo.setCurrentIndex(max(0, self.start_combo.findData(keep)))

    def timed(self, prog, now):
        """The program as Start says: (program, shift ns)."""
        ref = self.start_combo.currentData()
        if ref is None:
            return prog, 0
        return prog.start_now(now, ref or None, TEST_LEAD_S)

    # ---- files ----
    def on_new_eclipse(self):
        self.path = ""
        self.set_program(sc.load_sofimagic(sc.DEFAULT_SOFIMAGIC), "SoFiMagic's default eclipse program: "
                         "set the contact times and the date")

    def on_new_sequence(self):
        self.path = ""
        self.set_program(sc.sequence(10, 30, [sc.Exposure(2.0, 800)], start_in_s=10, files="files"),
                         "a sequence from NOW (when Run is pressed)")

    def on_open(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open a photo program", self.path or "",
                                                        "Programs (*.xml *.XML *.toml);;All files (*)")
        if path:
            self.open(path)

    def open(self, path):
        try:
            prog = sc.load(path, day=qdate(self.date_edit.date()), local=self.local())
        except Exception as e:
            self.error = f"{os.path.basename(path)}: {e}"
            self.status_label.setText(html.escape(self.error))
            return
        self.path = path
        self.set_program(prog, path)

    def on_save(self, ask=False):
        prog = self.collect()
        path = self.path
        if ask or not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save the photo program", path or os.path.join(self.win.out_dir(), "program.toml"),
                "TOML (*.toml);;SoFiMagic XML (*.xml)")
            if not path:
                return
        try:
            text = (sc.to_sofimagic(prog, self.local()) if path.lower().endswith(".xml")
                    else sc.to_toml(prog))
        except ValueError as e:
            self.error = f"not as SoFiMagic XML: {e} (save as .toml)"
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.path = path
        self.file_label.setText(f"saved {path}")

    # ---- running ----
    def running(self):
        return self.runner is not None and self.runner.state in ("waiting", "shooting")

    def on_dry_run(self, fast=True):
        """The program without a camera (schedule.dry_run): fast-forward or on time, as Start says;
        the list (Markdown) and CSV into the recording folder."""
        if self.running():
            self.error = "a run is going on"
            return
        prog = self.collect()
        if not prog.phases:
            self.error = "no phases"
            return
        now = self.win.clock()
        ref = self.start_combo.currentData()
        name, out_dir, local = self.win.file_name(), self.win.out_dir(), self.local()
        overhead = sc.OVERHEAD_S[prog.files]
        runner, _ = sc.dry_run(prog, now, fast=fast, shift_to_now=ref is not None, lead_s=TEST_LEAD_S,
                               overhead_s=overhead, out_dir=out_dir, name=name, real_clock=self.win.clock,
                               ref=ref or None)
        self.error, self.dry_path = "", ""

        def work():
            runner.run()
            self.dry_path = sc.write_dry_run(runner, local)
        self.runner = runner
        if fast:
            work()
        else:
            import threading
            threading.Thread(target=work, daemon=True).start()

    def on_run(self, test=False):
        if self.running():
            self.runner.stop()
            return
        t = self.win.thread
        if t is None or t.dslr is None:
            self.error = "connect the DSLR first (Camera tab)"
            return
        prog = self.collect()
        if not prog.phases:
            self.error = "no phases"
            return
        now = self.win.clock()
        prog = prog.at(now)
        if test:
            prog, _ = prog.start_now(now, None, TEST_LEAD_S)
        else:
            prog, _ = self.timed(prog, now)
        out_dir, mf = self.win.out_dir(), self.mf_check.isChecked()
        header, name, clock = self.win.photo_header(), self.win.file_name(), self.win.clock

        def make(dslr):                       # in the DSLR thread
            notes = [dslr.single_drive()] + ([dslr.manual_focus()] if mf else [])
            cam = sc_camera(dslr, out_dir, name, header)
            runner = sc.Runner(prog, cam, clock, log_dir=out_dir, name=name)
            runner.notes = [n for n in notes if n] + cam.notes
            self.runner = runner
            return runner
        self.win.live_check.setChecked(False)
        self.runner = None
        self.error = ""
        t.ask("program", make)

    def poll(self):
        r = self.runner
        busy = self.running()
        self.run_button.setText("Stop" if busy else "Run")
        self.dry_button.setEnabled(not busy)
        if r is None:
            self.status_label.setText(html.escape(self.error))
            return
        now = self.win.clock()
        dry = hasattr(r, "dry")
        text = ("dry run, " if dry else "") + f"{r.state}"
        if r.phase:
            text += f" · {r.phase}"
        if busy and r.next_ns is not None and r.state == "waiting":
            text += f" · next in {sc.duration_text((r.next_ns - now) / sc.NS)}"
        text += f" · {r.done} photos"
        if r.skipped:
            text += f", {r.skipped} blocks skipped"
        if r.cut:
            text += f", {r.cut} cut (next phase)"
        saver = getattr(r.camera, "saver", None)
        if saver is not None:
            text += f", {saver.written} FITS"
        escape = html.escape
        lines = [escape(text)]
        notes = list(getattr(r, "notes", [])) + list(getattr(getattr(r.camera, "dslr", None), "warnings", []))
        if notes:
            lines.append(escape("; ".join(notes)))
        if r.last:
            lines.append(escape(f"last: {r.last}"))
        if r.errors:
            lines.append(escape(f"{len(r.errors)} errors, last: {r.errors[-1]}"))
        if dry and self.dry_path:
            url = QtCore.QUrl.fromLocalFile(self.dry_path).toString()
            lines.append(f'list: <a href="{url}">{os.path.basename(self.dry_path)}</a>')
        else:
            lines.append(escape(f"log: {r.log_path}"))
        if self.error:
            lines.append(escape(self.error))
        self.status_label.setText("<br>".join(lines))


def sc_camera(dslr, out_dir, name, header):
    from .dslr import ProgramCamera
    return ProgramCamera(dslr, out_dir, name, header)
