#!/usr/bin/env python3
"""
UMD2 Viewer (drop-in)
- GUI-side draw throttle (~30 FPS)
- GUI-side display decimation (every Nth point)
- GUI-side "only draw steps (ΔD≠0)" filter
- Optional display EMA smoothing for x_nm (does not affect backend output)
- CSV logging (logs ALL incoming records, independent of display filters)
"""

import sys, os, json, subprocess, signal, time, threading, csv
from pathlib import Path
from typing import List, Optional

from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

APP_NAME = "UMD2 Viewer"
ROLLING_SECONDS = 30.0
DEFAULT_BAUD = 921600

# ------------------ Stable Port Combo ------------------
class StableComboBox(QtWidgets.QComboBox):
    popupShown = QtCore.Signal()
    popupHidden = QtCore.Signal()
    def showPopup(self):
        self.popupShown.emit()
        super().showPopup()
    def hidePopup(self):
        self.popupHidden.emit()
        super().hidePopup()

# ------------------ Floating plot window ------------------
class FloatingPlotWindow(QtWidgets.QMainWindow):
    def __init__(self, title: str, plot_widget: pg.PlotWidget, on_close_cb, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._plot = plot_widget
        self._on_close = on_close_cb
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        lay = QtWidgets.QVBoxLayout(central)
        lay.setContentsMargins(0,0,0,0)
        lay.addWidget(self._plot)
    def closeEvent(self, event):
        try:
            self._on_close(self._plot)
        except Exception:
            pass
        super().closeEvent(event)

# ------------------ Backend Reader ------------------
class BackendThread(QtCore.QObject):
    line_received = QtCore.Signal(dict)
    started = QtCore.Signal()
    stopped = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, umd2_path: str, args: List[str], parent=None):
        super().__init__(parent)
        self.umd2_path = umd2_path
        self.args = args
        self._proc = None
        self._stop = False
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._proc and self._proc.poll() is None:
            try:
                if os.name == "nt":
                    self._proc.terminate()
                else:
                    self._proc.send_signal(signal.SIGINT)
                    time.sleep(0.2)
                    self._proc.terminate()
            except Exception:
                pass

    def _run(self):
        try:
            py = sys.executable
            cmd = [py, self.umd2_path] + self.args
            print(f"[GUI] launching backend: {cmd}", file=sys.stderr, flush=True)
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True
            )
        except Exception as e:
            self.error.emit(f"Failed to start backend: {e}")
            self.stopped.emit("spawn-failed")
            return

        self.started.emit()

        # Read stdout lines (JSONL records)
        def pump_stdout():
            try:
                for line in self._proc.stdout:
                    if self._stop:
                        break
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        rec = json.loads(s)
                    except Exception:
                        continue
                    self.line_received.emit(rec)
            except Exception as e:
                self.error.emit(f"Streaming error: {e}")

        # Mirror stderr to console for debugging
        def pump_stderr():
            try:
                for line in self._proc.stderr:
                    if self._stop:
                        break
                    sys.stderr.write(line)
            except Exception:
                pass

        t1 = threading.Thread(target=pump_stdout, daemon=True)
        t2 = threading.Thread(target=pump_stderr, daemon=True)
        t1.start(); t2.start()
        t1.join()
        try:
            self._proc.terminate()
        except Exception:
            pass
        self.stopped.emit("exited")

# ------------------ Main Window ------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        pg.setConfigOptions(antialias=True)
        self._apply_dark_palette()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # ===== Source strip =====
        box = QtWidgets.QGroupBox("Source")
        sgrid = QtWidgets.QGridLayout(); box.setLayout(sgrid)

        self.source_combo = QtWidgets.QComboBox(); self.source_combo.addItems(["USB (Auto)", "File"])
        self.port_combo = StableComboBox()
        self.port_combo.setVisible(True)
        self._ports_popup_open = False
        self.port_combo.popupShown.connect(lambda: self._set_ports_popup(True))
        self.port_combo.popupHidden.connect(lambda: self._set_ports_popup(False))
        self.refresh_btn = QtWidgets.QPushButton("Refresh Ports")
        self.refresh_btn.clicked.connect(lambda: self._populate_ports(force=True))

        self.file_edit = QtWidgets.QLineEdit()
        self.browse_btn = QtWidgets.QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse_file)

        r=0
        sgrid.addWidget(QtWidgets.QLabel("Source"), r,0); sgrid.addWidget(self.source_combo, r,1); r+=1
        sgrid.addWidget(QtWidgets.QLabel("Port"), r,0); sgrid.addWidget(self.port_combo, r,1); sgrid.addWidget(self.refresh_btn, r,2); r+=1
        sgrid.addWidget(QtWidgets.QLabel("File"), r,0); sgrid.addWidget(self.file_edit, r,1); sgrid.addWidget(self.browse_btn, r,2)
        root.addWidget(box)

        # ===== Display filters (GUI-side) =====
        filt = QtWidgets.QGroupBox("Display Filters (GUI-side)")
        fgrid = QtWidgets.QGridLayout(); filt.setLayout(fgrid)
        self.only_steps = QtWidgets.QCheckBox("Only draw steps (ΔD ≠ 0)")
        self.only_steps.setChecked(True)
        self.decimate_spin = QtWidgets.QSpinBox(); self.decimate_spin.setRange(1, 500); self.decimate_spin.setValue(10)
        self.fps_spin = QtWidgets.QSpinBox(); self.fps_spin.setRange(5, 120); self.fps_spin.setValue(30)
        self.ema_alpha = QtWidgets.QDoubleSpinBox(); self.ema_alpha.setDecimals(3); self.ema_alpha.setRange(0.0, 0.999); self.ema_alpha.setSingleStep(0.05); self.ema_alpha.setValue(0.20)
        self.apply_btn = QtWidgets.QPushButton("Apply")
        self.apply_btn.clicked.connect(self._apply_display_settings)

        rr=0
        fgrid.addWidget(self.only_steps, rr, 0, 1, 2); rr+=1
        fgrid.addWidget(QtWidgets.QLabel("Draw every Nth point:"), rr,0); fgrid.addWidget(self.decimate_spin, rr,1); rr+=1
        fgrid.addWidget(QtWidgets.QLabel("Flush FPS:"), rr,0); fgrid.addWidget(self.fps_spin, rr,1); rr+=1
        fgrid.addWidget(QtWidgets.QLabel("EMA α (display):"), rr,0); fgrid.addWidget(self.ema_alpha, rr,1); rr+=1
        fgrid.addWidget(self.apply_btn, rr,1)
        root.addWidget(filt)

        # ===== Logging =====
        logbox = QtWidgets.QGroupBox("Logging")
        lgrid = QtWidgets.QGridLayout(); logbox.setLayout(lgrid)
        self.log_chk = QtWidgets.QCheckBox("Log to CSV")
        self.log_path_edit = QtWidgets.QLineEdit(self._default_log_path())
        self.log_browse_btn = QtWidgets.QPushButton("Browse…")
        self.log_browse_btn.clicked.connect(self._browse_log)
        lr=0
        lgrid.addWidget(self.log_chk, lr,0,1,3); lr+=1
        lgrid.addWidget(QtWidgets.QLabel("CSV file"), lr,0); lgrid.addWidget(self.log_path_edit, lr,1); lgrid.addWidget(self.log_browse_btn, lr,2)
        root.addWidget(logbox)

        # ===== Start/Stop =====
        btns = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start Stream")
        self.stop_btn = QtWidgets.QPushButton("Stop Stream"); self.stop_btn.setEnabled(False)
        btns.addWidget(self.start_btn); btns.addWidget(self.stop_btn)
        root.addLayout(btns)

        # ===== View controls =====
        viewrow = QtWidgets.QHBoxLayout(); root.addLayout(viewrow)
        self.autoY_x = QtWidgets.QCheckBox("Auto Y: Displacement"); self.autoY_x.setChecked(True)
        self.autoY_v = QtWidgets.QCheckBox("Auto Y: Velocity"); self.autoY_v.setChecked(True)
        self.reset_view_btn = QtWidgets.QPushButton("Reset View")
        self.pop_x_btn = QtWidgets.QPushButton("Pop-out Displacement")
        self.pop_v_btn = QtWidgets.QPushButton("Pop-out Velocity")
        viewrow.addWidget(self.autoY_x); viewrow.addWidget(self.autoY_v); viewrow.addStretch(1)
        viewrow.addWidget(self.reset_view_btn); viewrow.addWidget(self.pop_x_btn); viewrow.addWidget(self.pop_v_btn)

        # ===== Plots =====
        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical); root.addWidget(self.split, 1)
        self.plot_x = pg.PlotWidget(title="Displacement x_nm (rolling)")
        self.plot_v = pg.PlotWidget(title="Velocity v_nm_s (rolling)")
        for pw in (self.plot_x, self.plot_v):
            pw.showGrid(x=True,y=True,alpha=0.3)
            vb = pw.getViewBox()
            vb.enableAutoRange(pg.ViewBox.XAxis, True)
            vb.enableAutoRange(pg.ViewBox.YAxis, True)
        self.curve_x = self.plot_x.plot([],[],pen=pg.mkPen(width=2))
        self.curve_v = self.plot_v.plot([],[],pen=pg.mkPen(width=2))
        self.split.addWidget(self.plot_x); self.split.addWidget(self.plot_v)

        # ===== State =====
        self.t0=None; self.ts=[]; self.xs=[]; self.vs=[]
        self._pend_t=[]; self._pend_x=[]; self._pend_v=[]
        self._draw_every = self.decimate_spin.value()
        self._only_steps = self.only_steps.isChecked()
        self._ema_alpha = self.ema_alpha.value()
        self._ema_x = None
        self._accept_count = 0

        # CSV logging state
        self._log_file = None
        self._log_writer = None
        self._log_wrote_header = False

        self.worker: Optional[BackendThread] = None
        self._x_floating: Optional[FloatingPlotWindow] = None
        self._v_floating: Optional[FloatingPlotWindow] = None

        # Wire
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.reset_view_btn.clicked.connect(self._reset_view)
        self.autoY_x.toggled.connect(lambda _: self._apply_autoY(self.plot_x, self.autoY_x.isChecked()))
        self.autoY_v.toggled.connect(lambda _: self._apply_autoY(self.plot_v, self.autoY_v.isChecked()))
        self.pop_x_btn.clicked.connect(lambda: self._toggle_popout('x'))
        self.pop_v_btn.clicked.connect(lambda: self._toggle_popout('v'))

        # Status bar
        self.status = QtWidgets.QStatusBar(); self.setStatusBar(self.status)

        # Timers
        self.trim_timer = QtCore.QTimer(self); self.trim_timer.timeout.connect(self._trim); self.trim_timer.start(500)
        self.flush_timer = QtCore.QTimer(self); self.flush_timer.timeout.connect(self._flush_curves); self._set_fps(self.fps_spin.value())

        # Initial UI
        self._on_source_changed()
        self._apply_autoY(self.plot_x, True)
        self._apply_autoY(self.plot_v, True)

        # Auto-port refresh
        self.port_timer = QtCore.QTimer(self); self.port_timer.timeout.connect(self._populate_ports); self.port_timer.start(2000)

    # ---------- UI helpers ----------
    def _apply_dark_palette(self):
        app = QtWidgets.QApplication.instance()
        app.setStyle("Fusion")
        pal = QtGui.QPalette()
        pal.setColor(QtGui.QPalette.Window, QtGui.QColor(30,30,30))
        pal.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Base, QtGui.QColor(24,24,24))
        pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(36,36,36))
        pal.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Button, QtGui.QColor(45,45,45))
        pal.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(64,128,255))
        pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        app.setPalette(pal)

    def _set_ports_popup(self, is_open: bool):
        self._ports_popup_open = is_open
        if hasattr(self, "port_timer") and self.port_timer is not None:
            if is_open:
                self.port_timer.stop()
            else:
                self.port_timer.start(2000)

    def _on_source_changed(self):
        usb = (self.source_combo.currentText() == "USB (Auto)")
        self.file_edit.setEnabled(not usb)
        self.browse_btn.setEnabled(not usb)
        self.refresh_btn.setEnabled(usb)
        self.port_combo.setVisible(usb)
        self._populate_ports(force=True)

    def _populate_ports(self, force: bool=False):
        if self.source_combo.currentText() != "USB (Auto)":
            return
        if self._ports_popup_open and not force:
            return
        now = time.time()
        last = getattr(self, "_last_ports_refresh", 0.0)
        if not force and (now - last) < 0.8:
            return
        self._last_ports_refresh = now

        prev_text = self.port_combo.currentText().strip()
        ports=[]
        try:
            import serial.tools.list_ports as lp
            entries = list(lp.comports())
            ports = [p.device for p in entries if getattr(p, "device", None)]
        except Exception:
            ports=[]
        if not ports:
            ports=["<no ports>"]
        current = [self.port_combo.itemText(i) for i in range(self.port_combo.count())]
        if ports != current:
            self.port_combo.blockSignals(True)
            self.port_combo.clear()
            self.port_combo.addItems(ports)
            if prev_text and prev_text in ports:
                self.port_combo.setCurrentText(prev_text)
            else:
                self.port_combo.setCurrentIndex(0)
            self.port_combo.blockSignals(False)
        self.port_combo.setEnabled(ports != ["<no ports>"])

    def _browse_file(self):
        p,_ = QtWidgets.QFileDialog.getOpenFileName(self,"Choose input file","","All files (*)")
        if p: self.file_edit.setText(p)

    def _browse_log(self):
        p,_ = QtWidgets.QFileDialog.getSaveFileName(self,"Choose CSV log file", self.log_path_edit.text(), "CSV (*.csv);;All files (*)")
        if p: self.log_path_edit.setText(p)

    def _default_log_path(self) -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        folder = Path.home() / "Desktop" / "umd2_logs"
        return str(folder / f"umd2_{ts}.csv")

    # ---------- Display controls ----------
    def _apply_display_settings(self):
        self._only_steps = self.only_steps.isChecked()
        self._draw_every = max(1, int(self.decimate_spin.value()))
        self._set_fps(int(self.fps_spin.value()))
        self._ema_alpha = float(self.ema_alpha.value())
        self._ema_x = None  # reset EMA when user changes alpha

    def _set_fps(self, fps:int):
        fps = max(5, min(120, int(fps)))
        self.flush_timer.stop()
        self.flush_timer.setInterval(int(1000 / fps))
        self.flush_timer.start()

    # ---------- Start/Stop ----------
    def _build_args(self) -> List[str]:
        args: List[str] = []
        if self.source_combo.currentText() == "USB (Auto)":
            port = None
            if self.port_combo.count() > 0:
                port = self.port_combo.currentText()
            if not port or port == "<no ports>":
                raise RuntimeError("No serial ports found.")
            args += ["--serial", port, "--baud", str(DEFAULT_BAUD)]
        else:
            f = self.file_edit.text().strip()
            if not f:
                raise RuntimeError("Select an input file or choose USB (Auto).")
            args += ["--file", f]
        args += ["--out", "jsonl"]
        return args

    def _open_log_if_needed(self):
        # Close any previous
        self._close_log()

        if not self.log_chk.isChecked():
            return

        try:
            path = Path(self.log_path_edit.text().strip() or self._default_log_path())
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(path, "a", newline="")
            self._log_writer = csv.writer(self._log_file)
            self._log_wrote_header = False
            self.status.showMessage(f"Logging to {path}", 3000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CSV error", f"Cannot open CSV file:\n{e}")

    def _close_log(self):
        try:
            if self._log_file:
                self._log_file.flush()
                self._log_file.close()
        except Exception:
            pass
        self._log_file = None
        self._log_writer = None
        self._log_wrote_header = False

    def _start(self):
        try:
            umd2_path = str(Path(__file__).with_name("umd2.py"))
            if not os.path.exists(umd2_path):
                raise RuntimeError("umd2.py not found next to GUI script.")
            args = self._build_args()
            print(f"[GUI] umd2_path={umd2_path}", file=sys.stderr, flush=True)
            print(f"[GUI] args={args}", file=sys.stderr, flush=True)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self,"Config error",str(e)); return

        self._reset_buffers()
        self._reset_view()
        self._open_log_if_needed()

        self.worker = BackendThread(umd2_path, args)
        self.worker.line_received.connect(self._on_line)
        self.worker.started.connect(lambda: self._set_running(True))
        self.worker.stopped.connect(lambda reason: self._on_stopped(reason))
        self.worker.error.connect(lambda msg: self.status.showMessage(msg,5000))
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
        self._set_running(False)
        self._close_log()
        self.status.showMessage("Stopped.", 1500)

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        for w in (self.source_combo, self.port_combo, self.refresh_btn, self.file_edit, self.browse_btn, self.log_chk, self.log_path_edit, self.log_browse_btn):
            w.setEnabled(not running)

    # ---------- Buffers / view ----------
    def _reset_buffers(self):
        self.ts=[]; self.xs=[]; self.vs=[]
        self._pend_t=[]; self._pend_x=[]; self._pend_v=[]
        self._accept_count = 0
        self._ema_x = None
        self.t0=None
        self.curve_x.setData([],[])
        self.curve_v.setData([],[])

    def _apply_autoY(self, plot: pg.PlotWidget, enabled: bool):
        vb = plot.getViewBox()
        vb.enableAutoRange(pg.ViewBox.XAxis, True)
        vb.enableAutoRange(pg.ViewBox.YAxis, enabled)

    def _reset_view(self):
        if self.ts:
            tmax = self.ts[-1]
            xmin = max(0.0, tmax - ROLLING_SECONDS)
            xmax = tmax if tmax > 0 else ROLLING_SECONDS
        else:
            xmin, xmax = 0.0, ROLLING_SECONDS
        for plot in (self.plot_x, self.plot_v):
            vb = plot.getViewBox()
            vb.setXRange(xmin, xmax, padding=0.05)
        if self.autoY_x.isChecked():
            self.plot_x.getViewBox().enableAutoRange(pg.ViewBox.YAxis, True)
        if self.autoY_v.isChecked():
            self.plot_v.getViewBox().enableAutoRange(pg.ViewBox.YAxis, True)

    def _trim(self):
        if not self.ts: return
        cutoff = self.ts[-1] - ROLLING_SECONDS
        i=0
        while i < len(self.ts) and self.ts[i] < cutoff:
            i+=1
        if i>0:
            self.ts = self.ts[i:]; self.xs = self.xs[i:]; self.vs = self.vs[i:]

    # ---------- Incoming data ----------
    @QtCore.Slot(dict)
    def _on_line(self, rec: dict):
        # Timestamp basis for logging & plotting
        t = time.time()
        if self.t0 is None:
            self.t0 = t
        relt = t - self.t0

        # ---- CSV logging (ALL records) ----
        if self._log_writer:
            if not self._log_wrote_header:
                header = ["ts_rel_s","seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s","x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"]
                self._log_writer.writerow(header)
                self._log_wrote_header = True
            row = [
                f"{relt:.6f}",
                rec.get("seq"),
                rec.get("fs_hz"),
                rec.get("D"),
                rec.get("deltaD"),
                rec.get("step_nm"),
                rec.get("x_nm"),
                rec.get("v_nm_s"),
                rec.get("x_nm_ema"),
                rec.get("x_nm_ma"),
                rec.get("x_nm_env"),
                rec.get("angle_deg"),
                rec.get("x2"),
                rec.get("y2"),
            ]
            try:
                self._log_writer.writerow(row)
            except Exception:
                pass

        # ---- Display filters (optional) ----
        if self._only_steps and int(rec.get("deltaD", 0)) == 0:
            return
        self._accept_count += 1
        if self._draw_every > 1 and (self._accept_count % self._draw_every) != 0:
            return

        x = float(rec.get("x_nm", 0.0))
        v = float(rec.get("v_nm_s", 0.0))

        # Optional display EMA for x
        if self._ema_alpha > 0.0:
            if self._ema_x is None:
                self._ema_x = x
            else:
                a = self._ema_alpha
                self._ema_x = a*x + (1.0 - a)*self._ema_x
            x_plot = self._ema_x
        else:
            x_plot = x

        # Buffer for flush timer
        self._pend_t.append(relt)
        self._pend_x.append(x_plot)
        self._pend_v.append(v)

        # Status line
        self.status.showMessage(
            f"seq={rec.get('seq')} D={rec.get('D')} dD={rec.get('deltaD')} x={x:.3f}nm v={v:.3f}nm/s",
            600
        )

    def _flush_curves(self):
        if not self._pend_t:
            return
        self.ts.extend(self._pend_t); self._pend_t.clear()
        self.xs.extend(self._pend_x); self._pend_x.clear()
        self.vs.extend(self._pend_v); self._pend_v.clear()
        self._trim()
        self.curve_x.setData(self.ts, self.xs)
        self.curve_v.setData(self.ts, self.vs)

    def _on_stopped(self, reason: str):
        self._set_running(False)
        self._close_log()
        self.status.showMessage(f"Backend stopped: {reason}", 2000)

    # ---------- Pop-out helpers ----------
    def _toggle_popout(self, which: str):
        if which == 'x':
            if getattr(self, "_x_floating", None) is None:
                self._x_floating = FloatingPlotWindow("Displacement (Pop-out)", self.plot_x, self._dock_back_x, self)
                self._x_floating.resize(800, 400)
                self._x_floating.show()
            else:
                self._x_floating.close(); self._x_floating = None
        else:
            if getattr(self, "_v_floating", None) is None:
                self._v_floating = FloatingPlotWindow("Velocity (Pop-out)", self.plot_v, self._dock_back_v, self)
                self._v_floating.resize(800, 400)
                self._v_floating.show()
            else:
                self._v_floating.close(); self._v_floating = None

    def _dock_back_x(self, plot_widget: pg.PlotWidget):
        self._x_floating = None
        self.split.insertWidget(0, plot_widget)
        plot_widget.show()

    def _dock_back_v(self, plot_widget: pg.PlotWidget):
        self._v_floating = None
        self.split.addWidget(plot_widget)
        plot_widget.show()

    def closeEvent(self, event):
        try:
            if self.worker:
                self.worker.stop()
        except Exception:
            pass
        self._close_log()
        super().closeEvent(event)

# ------------------ Entrypoint ------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.resize(1120, 820); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
