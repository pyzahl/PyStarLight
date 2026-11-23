"""The Station & equipment dialog of the camera window (from gui.py): required before recording."""

from . import station_header
from .qt import QtWidgets


def default_site():
    """The PySyzygos default site (pystarlight.config.site), {} when there is none or the package
    cannot be imported (the Pi's system Python without its needs)."""
    try:
        from pystarlight.config import load, site
        return site(load()) or {}
    except Exception:
        return {}


class StationDialog(QtWidgets.QDialog):
    """Station and equipment ([observer] of the private config): what every recording's header
    says about where, with what and by whom (station_header.FIELDS). Saved into the config file;
    required fields are marked * and must be filled in before a recording can be armed."""

    def __init__(self, parent, observer, path, need_site=False, why="", pixel_um=None):
        super().__init__(parent)
        self.setWindowTitle("Station & equipment")
        self.path, self.need_site, self.pixel_um = path, need_site, pixel_um
        lay = QtWidgets.QVBoxLayout(self)
        if why:
            note = QtWidgets.QLabel(why, wordWrap=True)
            note.setStyleSheet("color: #c0392b; font-weight: bold")
            lay.addWidget(note)
        form = QtWidgets.QFormLayout()
        lay.addLayout(form)
        self.widgets = {}
        for key, label, kind, required, choices, hint in station_header.FIELDS:
            v = observer.get(key)
            if kind is bool:
                w = QtWidgets.QCheckBox()
                w.setChecked(bool(v))
            elif kind is float:
                w = QtWidgets.QDoubleSpinBox(decimals=7 if key in ("latitude", "longitude") else 3,
                                             minimum=-1e6, maximum=1e6)
                if key.endswith("_mm") or key == "altitude":
                    w.setDecimals(1)
                if key.endswith("_mm"):
                    w.setSuffix(" mm")
                try:
                    w.setValue(float(v or 0))
                except (TypeError, ValueError):
                    w.setValue(0)
            elif choices:
                # the telescope type is one of the report form's; the method code may be another
                w = QtWidgets.QComboBox(editable=key != "telescope_type")
                if key == "telescope_type" and v and str(v) not in choices:
                    choices = (*choices, str(v))        # shown, so it can be seen and replaced
                w.addItems(["", *choices])
                text = str(v or "")
                match = [c for c in choices if c == text or c.split(" ")[0] == text]
                w.setCurrentText(match[0] if match else text)
            else:
                w = QtWidgets.QLineEdit(str(v or ""))
            if hint:
                w.setToolTip(hint)
            self.widgets[key] = (w, kind)
            form.addRow(label + (" *" if required else ""), w)
        self.optics_label = QtWidgets.QLabel(wordWrap=True)
        self.optics_label.setToolTip("what these values imply: compare the plate scale with a "
                                     "plate solve of the stack")
        lay.addWidget(self.optics_label)
        self.missing_label = QtWidgets.QLabel(wordWrap=True)
        self.missing_label.setStyleSheet("color: #c0392b")
        lay.addWidget(self.missing_label)
        where = QtWidgets.QLabel(f"saved in {path}  (* required; --observer / --telescope on the "
                                 "command line override)", wordWrap=True)
        where.setStyleSheet("color: gray")
        lay.addWidget(where)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save
                                             | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        for w, kind in self.widgets.values():
            sig = (w.toggled if kind is bool else w.valueChanged if kind is float else
                   w.currentTextChanged if isinstance(w, QtWidgets.QComboBox) else w.textChanged)
            sig.connect(self.show_missing)
        self.show_missing()

    def values(self):
        out = {}
        for key, (w, kind) in self.widgets.items():
            if kind is bool:
                out[key] = w.isChecked()
            elif kind is float:
                out[key] = float(w.value())
            elif isinstance(w, QtWidgets.QComboBox):
                t = w.currentText().strip()
                out[key] = t.split(" ")[0] if key == "observing_method" else t
            else:
                out[key] = w.text().strip()
        return out

    def show_missing(self, *_):
        values = self.values()
        self.optics_label.setText(station_header.optics_line(values, self.pixel_um))
        miss = station_header.missing(values, self.need_site)
        self.missing_label.setText(("still missing: " + ", ".join(miss)) if miss else "complete")
        self.missing_label.setStyleSheet("color: #c0392b" if miss else "color: #27ae60")
