#!/usr/bin/env python3
"""
UMD2 GUI (decluttered + capture sidecar raw log + pop-out plots + rolling Reset View)

- Top strip:
    Source (USB Auto/File), Port selector (hidden if one), File picker (for File mode)
    Capture CSV + Start/Stop Capture
    [ ] Raw serial sidecar (.raw.log)   <-- if checked, backend gets --raw-log "<capture>.raw.log" on next Start Stream
    Start/Stop Stream

- Advanced (collapsible):
    Basic (baud/fs/emit/decimate), Scaling, Mode, Smooth & Env, FFT (same as before)

- Plots:
    Displacement & Velocity with Auto-Y toggles, Reset View, and Pop-out buttons.
"""

import sys, os, json, subprocess, signal, time, threading, csv
from pathlib import Path
from typing import List, Optional
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

APP_NAME = "UMD2 Viewer"
ROLLING_SECONDS = 30.0
DEFAULT_BAUD = 921600

CSV_HEADER = ["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s",
              "x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"]

# ------------------ Collapsible Section ------------------
class CollapsibleSection(QtWidgets.QWidget):
    def __init__(self, title: str, parent=None, start_collapsed=True, anim_ms=150):
        super().__init__(parent)
        self.toggle = QtWidgets.QToolButton(text=title, checkable=True, checked=not start_collapsed)
        self.toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(QtCore.Qt.RightArrow if start_collapsed else QtCore.Qt.DownArrow)
        self.toggle.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

        self.content = QtWidgets.QScrollArea()
        self.content.setWidgetResizable(True)
        self.content.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.content.setMaximumHeight(0 if start_collapsed else 16777215)

        self.anim = QtCore.QPropertyAnimation(self.content, b"maximumHeight")
        self.anim.setDuration(anim_ms)
        self.anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.toggle)
        lay.addWidget(self.content)

        self.toggle.toggled.connect(self._on_toggled)

    def setContentLayout(self, layout: QtWidgets.QLayout):
        w = QtWidgets.QWidget()
        w.setLayout(layout)
        self.content.setWidget(w)

    def _on_toggled(self, checked: bool):
        self.toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        start = self.content.maximumHeight()
        self.content.setMaximumHeight(self.content.sizeHint().height() if checked else self.content.height())
        end = self.content.sizeHint().height() if checked else 0
        self.anim.stop()
        self.anim.setStartValue(start)
        self.anim.setEndValue(end)
        self.anim.start()

# ------------------ Floating plot window (pop-out) ------------------
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

# ------------------ Backend Thread ------------------
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
            cmd = [sys.executable, self.umd2_path] + self.args
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True
            )
        except Exception as e:
            self.error.emit(f"Failed to start backend: {e}")
            self.stopped.emit("spawn-failed"); return

        self.started.emit()
        try:
            for line in self._proc.stdout:
                if self._stop: break
                line = line.strip()
                if not line: continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.line_received.emit(rec)
        except Exception as e:
            self.error.emit(f"Streaming error: {e}")
        finally:
            try: self._proc.terminate()
            except: pass
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

        # ===== Source (minimal) =====
        box = QtWidgets.QGroupBox("Source")
        sgrid = QtWidgets.QGridLayout(box)

        self.source_combo = QtWidgets.QComboBox(); self.source_combo.addItems(["USB (Auto)", "File"])
        self.port_combo = QtWidgets.QComboBox(); self.port_combo.setVisible(False)
        self.refresh_btn = QtWidgets.QPushButton("Refresh Ports")
        self.refresh_btn.clicked.connect(self._populate_ports)

        self.file_edit = QtWidgets.QLineEdit()
        self.browse_btn = QtWidgets.QPushButton("Browse…"); self.browse_btn.clicked.connect(self._browse_file)

        # Capture CSV (processed) + raw serial sidecar checkbox
        self.capture_path = QtWidgets.QLineEdit(); self.capture_path.setPlaceholderText("path/to/capture.csv")
        self.capture_browse = QtWidgets.QPushButton("…"); self.capture_browse.clicked.connect(self._browse_capture)
        self.capture_btn = QtWidgets.QPushButton("Start Capture"); self.capture_btn.setCheckable(True)
        self.capture_btn.clicked.connect(self._toggle_capture)

        self.raw_sidecar_chk = QtWidgets.QCheckBox("Raw serial sidecar (.raw.log)")

        r=0
        sgrid.addWidget(QtWidgets.QLabel("Source"), r,0); sgrid.addWidget(self.source_combo, r,1)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("Port"), r,0); sgrid.addWidget(self.port_combo, r,1); sgrid.addWidget(self.refresh_btn, r,2)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("File"), r,0); sgrid.addWidget(self.file_edit, r,1); sgrid.addWidget(self.browse_btn, r,2)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("Capture CSV"), r,0); sgrid.addWidget(self.capture_path, r,1); sgrid.addWidget(self.capture_browse, r,2)
        r+=1; sgrid.addWidget(self.capture_btn, r,1)
        r+=1; sgrid.addWidget(self.raw_sidecar_chk, r,1)

        root.addWidget(box)

        # ===== Advanced (collapsible) =====
        self.adv_section = CollapsibleSection("Advanced", start_collapsed=True)
        adv_tabs = QtWidgets.QTabWidget()

        # --- Basic tab
        t_basic = QtWidgets.QWidget()
        bgrid = QtWidgets.QGridLayout(t_basic)
        self.adv_baud_spin = QtWidgets.QSpinBox(); self.adv_baud_spin.setRange(9600, 3000000); self.adv_baud_spin.setValue(DEFAULT_BAUD)
        self.fs_spin = QtWidgets.QDoubleSpinBox(); self.fs_spin.setRange(0,1e9); self.fs_spin.setDecimals(2); self.fs_spin.setValue(0.0)
        self.emit_combo = QtWidgets.QComboBox(); self.emit_combo.addItems(["every","onstep"])
        self.decimate_spin = QtWidgets.QSpinBox(); self.decimate_spin.setRange(1,1000); self.decimate_spin.setValue(1)
        r=0
        bgrid.addWidget(QtWidgets.QLabel("Baud (override)"), r,0); bgrid.addWidget(self.adv_baud_spin, r,1); r+=1
        bgrid.addWidget(QtWidgets.QLabel("fs (Hz, 0=auto)"), r,0); bgrid.addWidget(self.fs_spin, r,1); r+=1
        bgrid.addWidget(QtWidgets.QLabel("emit"), r,0); bgrid.addWidget(self.emit_combo, r,1); r+=1
        bgrid.addWidget(QtWidgets.QLabel("decimate"), r,0); bgrid.addWidget(self.decimate_spin, r,1)
        t_basic.setLayout(bgrid)

        # --- Scaling tab
        t_scale = QtWidgets.QWidget()
        cgrid = QtWidgets.QGridLayout(t_scale)
        self.stepnm_edit = QtWidgets.QLineEdit(); self.stepnm_edit.setPlaceholderText("(optional override nm/count)")
        self.lambda_spin = QtWidgets.QDoubleSpinBox(); self.lambda_spin.setRange(1,1e6); self.lambda_spin.setDecimals(3); self.lambda_spin.setValue(632.991)
        self.scale_div_combo = QtWidgets.QComboBox(); self.scale_div_combo.addItems(["1","2","4","8"]); self.scale_div_combo.setCurrentText("8")
        self.startnm_spin = QtWidgets.QDoubleSpinBox(); self.startnm_spin.setRange(-1e15,1e15); self.startnm_spin.setDecimals(6); self.startnm_spin.setValue(0.0)
        self.straight_spin = QtWidgets.QDoubleSpinBox(); self.straight_spin.setRange(0,1e6); self.straight_spin.setDecimals(6); self.straight_spin.setValue(1.0)
        r=0
        cgrid.addWidget(QtWidgets.QLabel("stepnm override"), r,0); cgrid.addWidget(self.stepnm_edit, r,1); r+=1
        cgrid.addWidget(QtWidgets.QLabel("lambda (nm)"), r,0); cgrid.addWidget(self.lambda_spin, r,1); r+=1
        cgrid.addWidget(QtWidgets.QLabel("scale divisor"), r,0); cgrid.addWidget(self.scale_div_combo, r,1); r+=1
        cgrid.addWidget(QtWidgets.QLabel("startnm (baseline)"), r,0); cgrid.addWidget(self.startnm_spin, r,1); r+=1
        cgrid.addWidget(QtWidgets.QLabel("straightness multiplier"), r,0); cgrid.addWidget(self.straight_spin, r,1)
        t_scale.setLayout(cgrid)

        # --- Mode tab
        t_mode = QtWidgets.QWidget()
        mgrid = QtWidgets.QGridLayout(t_mode)
        self.mode_calc_combo = QtWidgets.QComboBox(); self.mode_calc_combo.addItems(["displacement","angle"])
        self.angle_norm = QtWidgets.QDoubleSpinBox(); self.angle_norm.setRange(1e-9,1e12); self.angle_norm.setDecimals(6); self.angle_norm.setValue(1.0)
        self.angle_corr = QtWidgets.QDoubleSpinBox(); self.angle_corr.setRange(0,1e6); self.angle_corr.setDecimals(6); self.angle_corr.setValue(1.0)
        r=0
        mgrid.addWidget(QtWidgets.QLabel("Compute"), r,0); mgrid.addWidget(self.mode_calc_combo, r,1); r+=1
        mgrid.addWidget(QtWidgets.QLabel("angle_norm_nm"), r,0); mgrid.addWidget(self.angle_norm, r,1); r+=1
        mgrid.addWidget(QtWidgets.QLabel("angle_corr"), r,0); mgrid.addWidget(self.angle_corr, r,1)
        t_mode.setLayout(mgrid)

        # --- Smooth & Env tab
        t_env = QtWidgets.QWidget()
        fgrid = QtWidgets.QGridLayout(t_env)
        self.ema_alpha = QtWidgets.QDoubleSpinBox(); self.ema_alpha.setRange(0,1); self.ema_alpha.setSingleStep(0.05); self.ema_alpha.setValue(0.0)
        self.ma_window = QtWidgets.QSpinBox(); self.ma_window.setRange(0,100000); self.ma_window.setValue(0)
        self.env_temp = QtWidgets.QLineEdit(); self.env_temp.setPlaceholderText("temp C (opt)")
        self.env_temp0 = QtWidgets.QLineEdit(); self.env_temp0.setPlaceholderText("ref temp C")
        self.env_ktemp = QtWidgets.QLineEdit(); self.env_ktemp.setPlaceholderText("ktemp per C")
        self.env_press = QtWidgets.QLineEdit(); self.env_press.setPlaceholderText("press (opt)")
        self.env_press0 = QtWidgets.QLineEdit(); self.env_press0.setPlaceholderText("ref press")
        self.env_kpress = QtWidgets.QLineEdit(); self.env_kpress.setPlaceholderText("kpress")
        self.env_hum = QtWidgets.QLineEdit(); self.env_hum.setPlaceholderText("RH% (opt)")
        self.env_hum0 = QtWidgets.QLineEdit(); self.env_hum0.setPlaceholderText("ref RH%")
        self.env_khum = QtWidgets.QLineEdit(); self.env_khum.setPlaceholderText("khum")
        r=0
        fgrid.addWidget(QtWidgets.QLabel("EMA alpha"), r,0); fgrid.addWidget(self.ema_alpha, r,1); r+=1
        fgrid.addWidget(QtWidgets.QLabel("MA window"), r,0); fgrid.addWidget(self.ma_window, r,1); r+=1
        fgrid.addWidget(QtWidgets.QLabel("Temp/Press/Hum (opt)"), r,0); r+=1
        fgrid.addWidget(self.env_temp, r,0); fgrid.addWidget(self.env_temp0, r,1); fgrid.addWidget(self.env_ktemp, r,2); r+=1
        fgrid.addWidget(self.env_press, r,0); fgrid.addWidget(self.env_press0, r,1); fgrid.addWidget(self.env_kpress, r,2); r+=1
        fgrid.addWidget(self.env_hum, r,0); fgrid.addWidget(self.env_hum0, r,1); fgrid.addWidget(self.env_khum, r,2)
        t_env.setLayout(fgrid)

        # --- FFT tab
        t_fft = QtWidgets.QWidget()
        egrid = QtWidgets.QGridLayout(t_fft)
        self.fft_len = QtWidgets.QSpinBox(); self.fft_len.setRange(0, 1_048_576); self.fft_len.setValue(0)
        self.fft_every = QtWidgets.QSpinBox(); self.fft_every.setRange(0, 1_000_000); self.fft_every.setValue(0)
        self.fft_signal = QtWidgets.QComboBox(); self.fft_signal.addItems(["x","v"])
        self.enable_xy = QtWidgets.QCheckBox("Parse X/Y second channel")
        r=0
        egrid.addWidget(QtWidgets.QLabel("fft_len"), r,0); egrid.addWidget(self.fft_len, r,1); r+=1
        egrid.addWidget(QtWidgets.QLabel("fft_every"), r,0); egrid.addWidget(self.fft_every, r,1); r+=1
        egrid.addWidget(QtWidgets.QLabel("fft_signal"), r,0); egrid.addWidget(self.fft_signal, r,1); r+=1
        egrid.addWidget(self.enable_xy, r,0,1,2)
        t_fft.setLayout(egrid)

        adv_tabs.addTab(t_basic, "Basic")
        adv_tabs.addTab(t_scale, "Scaling")
        adv_tabs.addTab(t_mode, "Mode")
        adv_tabs.addTab(t_env, "Smooth & Env")
        adv_tabs.addTab(t_fft, "FFT")
        adv_layout = QtWidgets.QVBoxLayout()
        adv_layout.addWidget(adv_tabs)
        self.adv_section.setContentLayout(adv_layout)
        root.addWidget(self.adv_section)

        # ===== Stream controls =====
        btns = QtWidgets.QHBoxLayout()
        root.addLayout(btns)
        self.start_btn = QtWidgets.QPushButton("Start Stream")
        self.stop_btn = QtWidgets.QPushButton("Stop Stream"); self.stop_btn.setEnabled(False)
        btns.addWidget(self.start_btn); btns.addWidget(self.stop_btn)

        # View controls for plots (+ pop-out)
        viewrow = QtWidgets.QHBoxLayout()
        root.addLayout(viewrow)
        self.autoY_x = QtWidgets.QCheckBox("Auto Y: Displacement"); self.autoY_x.setChecked(True)
        self.autoY_v = QtWidgets.QCheckBox("Auto Y: Velocity"); self.autoY_v.setChecked(True)
        self.reset_view_btn = QtWidgets.QPushButton("Reset View")
        self.pop_x_btn = QtWidgets.QPushButton("Pop-out Displacement")
        self.pop_v_btn = QtWidgets.QPushButton("Pop-out Velocity")
        viewrow.addWidget(self.autoY_x); viewrow.addWidget(self.autoY_v)
        viewrow.addStretch(1)
        viewrow.addWidget(self.reset_view_btn)
        viewrow.addWidget(self.pop_x_btn)
        viewrow.addWidget(self.pop_v_btn)

        # ===== Plots =====
        self.split = QtWidgets.QSplitter(QtCore.Qt.Vertical); root.addWidget(self.split, 1)
        self.plot_x = pg.PlotWidget(title="Displacement x_nm (rolling)")
        self.plot_v = pg.PlotWidget(title="Velocity v_nm_s (rolling)")
        for pw in (self.plot_x, self.plot_v):
            pw.showGrid(x=True,y=True,alpha=0.3)
            pw.setMouseEnabled(x=True, y=True)
            vb = pw.getViewBox()
            vb.enableAutoRange(pg.ViewBox.XAxis, False)  # we manage rolling X window
            vb.enableAutoRange(pg.ViewBox.YAxis, True)
        self.curve_x = self.plot_x.plot([],[],pen=pg.mkPen(width=2))
        self.curve_v = self.plot_v.plot([],[],pen=pg.mkPen(width=2))
        self.split.addWidget(self.plot_x); self.split.addWidget(self.plot_v)

        self.plot_fft = pg.PlotWidget(title="FFT (magnitude)")
        self.curve_fft = self.plot_fft.plot([],[],pen=pg.mkPen(width=2))
        root.addWidget(self.plot_fft); self.plot_fft.setVisible(False)

        # ===== State =====
        self.t0=None; self.ts=[]; self.xs=[]; self.vs=[]
        self.worker: Optional[BackendThread] = None

        # pop-out state
        self._x_floating: Optional[FloatingPlotWindow] = None
        self._v_floating: Optional[FloatingPlotWindow] = None

        # Capture state
        self.capture_active = False
        self.capture_file = None
        self.capture_writer: Optional[csv.writer] = None

        # Raw sidecar intent/state
        self.raw_sidecar_enabled = False
        self.rawlog_sidecar_path = ""

        # Wire
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.reset_view_btn.clicked.connect(self._reset_view)
        self.autoY_x.toggled.connect(lambda _: self._apply_autoY(self.plot_x, self.autoY_x.isChecked()))
        self.autoY_v.toggled.connect(lambda _: self._apply_autoY(self.plot_v, self.autoY_v.isChecked()))
        self.pop_x_btn.clicked.connect(lambda: self._toggle_popout('x'))
        self.pop_v_btn.clicked.connect(lambda: self._toggle_popout('v'))
        self._on_source_changed()

        self.status = QtWidgets.QStatusBar(); self.setStatusBar(self.status)
        self.trim_timer = QtCore.QTimer(self); self.trim_timer.timeout.connect(self._trim); self.trim_timer.start(500)

        # Auto-port refresh every 2s in USB mode
        self.port_timer = QtCore.QTimer(self); self.port_timer.timeout.connect(self._populate_ports)
        self.port_timer.start(2000)

        self._apply_autoY(self.plot_x, True)
        self._apply_autoY(self.plot_v, True)

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

    def _on_source_changed(self):
        usb = (self.source_combo.currentText() == "USB (Auto)")
        self.file_edit.setEnabled(not usb)
        self.browse_btn.setEnabled(not usb)
        self.refresh_btn.setEnabled(usb)
        # sidecar only meaningful for USB capture, but we let you pre-arm it
        self._populate_ports()

    def _populate_ports(self):
        usb = (self.source_combo.currentText() == "USB (Auto)")
        self.port_combo.setVisible(False)
        if not usb:
            return
        ports = []
        try:
            import serial.tools.list_ports as lp
            ports = [p.device for p in lp.comports()]
        except Exception:
            ports = []
        self.port_combo.clear()
        if not ports:
            self.port_combo.addItem("<no ports>")
            self.port_combo.setVisible(True)
            return
        if len(ports) == 1:
            self.port_combo.addItem(ports[0])
            self.port_combo.setVisible(False)
        else:
            self.port_combo.addItems(ports)
            self.port_combo.setVisible(True)

    def _browse_file(self):
        p,_ = QtWidgets.QFileDialog.getOpenFileName(self,"Choose input file","","All files (*)")
        if p: self.file_edit.setText(p)

    def _browse_capture(self):
        p,_ = QtWidgets.QFileDialog.getSaveFileName(self,"Choose CSV to capture","","CSV files (*.csv);;All files (*)")
        if p: self.capture_path.setText(p)

    # ---------- Build backend args ----------
    def _build_args(self) -> List[str]:
        args: List[str] = []

        # Source
        if self.source_combo.currentText() == "USB (Auto)":
            port = None
            if self.port_combo.isVisible():
                port = self.port_combo.currentText()
            else:
                if self.port_combo.count() > 0:
                    port = self.port_combo.itemText(0)
            if not port or port == "<no ports>":
                raise RuntimeError("No serial ports found.")
            baud = self.adv_baud_spin.value() if self.adv_section.toggle.isChecked() else DEFAULT_BAUD
            args += ["--serial", port, "--baud", str(baud)]
        else:
            f = self.file_edit.text().strip()
            if not f: raise RuntimeError("Select an input file or choose USB (Auto).")
            args += ["--file", f]

        # Advanced options (only if drawer open)
        if self.adv_section.toggle.isChecked():
            if self.fs_spin.value() > 0:
                args += ["--fs", str(self.fs_spin.value())]
            args += ["--emit", self.emit_combo.currentText(), "--decimate", str(self.decimate_spin.value())]

            # Scaling
            stepov = self.stepnm_edit.text().strip()
            if stepov: args += ["--stepnm", stepov]
            args += ["--lambda-nm", str(self.lambda_spin.value()), "--scale-div", self.scale_div_combo.currentText()]
            args += ["--startnm", str(self.startnm_spin.value()), "--straight-mult", str(self.straight_spin.value())]

            # Mode
            args += ["--mode", self.mode_calc_combo.currentText(),
                     "--angle-norm-nm", str(self.angle_norm.value()),
                     "--angle-corr", str(self.angle_corr.value())]

            # Smoothing & Env
            args += ["--ema-alpha", str(self.ema_alpha.value()), "--ma-window", str(self.ma_window.value())]
            def add_env(flag, widget):
                txt = widget.text().strip()
                if txt: args += [flag, txt]
            add_env("--env-temp", self.env_temp); add_env("--env-temp0", self.env_temp0); add_env("--env-ktemp", self.env_ktemp)
            add_env("--env-press", self.env_press); add_env("--env-press0", self.env_press0); add_env("--env-kpress", self.env_kpress)
            add_env("--env-hum", self.env_hum); add_env("--env-hum0", self.env_hum0); add_env("--env-khum", self.env_khum)

            # FFT
            args += ["--fft-len", str(self.fft_len.value()), "--fft-every", str(self.fft_every.value()),
                     "--fft-signal", self.fft_signal.currentText()]

        # Raw sidecar passing (only if USB and armed BEFORE start)
        if self._is_usb_mode() and self.raw_sidecar_enabled and self.rawlog_sidecar_path:
            args += ["--raw-log", self.rawlog_sidecar_path]

        # GUI always expects JSONL
        args += ["--out", "jsonl"]
        return args

    def _is_usb_mode(self) -> bool:
        return self.source_combo.currentText() == "USB (Auto)"

    # ---------- Stream control ----------
    def _start(self):
        try:
            umd2_path = str(Path(__file__).with_name("umd2.py"))
            if not os.path.exists(umd2_path): raise RuntimeError("umd2.py not found next to GUI script.")
            args = self._build_args()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self,"Config error",str(e)); return

        self.ts.clear(); self.xs.clear(); self.vs.clear(); self.t0=None
        self.curve_x.setData([],[]); self.curve_v.setData([],[])
        self._reset_view()

        self.worker = BackendThread(umd2_path, args)
        self.worker.line_received.connect(self._on_line)
        self.worker.started.connect(lambda: self._set_running(True))
        self.worker.stopped.connect(lambda reason: self._on_stopped(reason))
        self.worker.error.connect(lambda msg: self.status.showMessage(msg,5000))
        self.worker.start()

    def _stop(self):
        if self.worker: self.worker.stop()
        self._set_running(False)
        if self.capture_active: self._toggle_capture(force_off=True)
        self.status.showMessage("Stopped.", 2000)

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        # Lock source & advanced during run (capture remains usable)
        for w in (self.source_combo, self.port_combo, self.refresh_btn, self.file_edit, self.browse_btn, self.adv_section.toggle):
            w.setEnabled(not running)

    # ---------- Capture control (processed CSV) + sidecar raw ----------
    def _toggle_capture(self, force_off: bool=False):
        if force_off or (self.capture_active and not self.capture_btn.isChecked()):
            self.capture_active = False
            self.capture_btn.setChecked(False)
            self.capture_btn.setText("Start Capture")
            try:
                if self.capture_file:
                    self.capture_file.flush(); self.capture_file.close()
            finally:
                self.capture_file = None; self.capture_writer = None
            self.status.showMessage("Capture stopped.", 2000)
            return

        # Start capture
        path = self.capture_path.text().strip()
        if not path:
            p,_ = QtWidgets.QFileDialog.getSaveFileName(self,"Choose CSV to capture","","CSV files (*.csv);;All files (*)")
            if not p:
                self.capture_btn.setChecked(False); return
            self.capture_path.setText(p); path = p
        try:
            new = not os.path.exists(path)
            self.capture_file = open(path, "a", newline="")
            self.capture_writer = csv.writer(self.capture_file)
            if new:
                self.capture_writer.writerow(CSV_HEADER)
            self.capture_active = True
            self.capture_btn.setText("Stop Capture")
            self.status.showMessage(f"Capturing to {path}", 2000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self,"Capture",f"Failed to open file:\n{e}")
            self.capture_btn.setChecked(False)
            self.capture_file=None; self.capture_writer=None; self.capture_active=False
            return

        # If raw sidecar is checked, arm it and compute sidecar path
        if self.raw_sidecar_chk.isChecked() and self._is_usb_mode():
            sidecar = path + ".raw.log"
            self.raw_sidecar_enabled = True
            self.rawlog_sidecar_path = sidecar
            # If stream already running, this only takes effect on the next Start
            self.status.showMessage(f"Raw sidecar armed: {sidecar} (applies on next Start Stream).", 4000)
        else:
            self.raw_sidecar_enabled = False
            self.rawlog_sidecar_path = ""

    # ---------- Pop-out/in helpers ----------
    def _toggle_popout(self, which: str):
        if which == 'x':
            if self._x_floating is None:
                self._x_floating = FloatingPlotWindow("Displacement (Pop-out)", self.plot_x, self._dock_back_x, self)
                self._x_floating.resize(800, 400)
                self._x_floating.show()
            else:
                self._x_floating.close(); self._x_floating = None
        else:
            if self._v_floating is None:
                self._v_floating = FloatingPlotWindow("Velocity (Pop-out)", self.plot_v, self._dock_back_v, self)
                self._v_floating.resize(800, 400)
                self._v_floating.show()
            else:
                self._v_floating.close(); self._v_floating = None

    def _dock_back_x(self, plot_widget: pg.PlotWidget):
        self._x_floating = None
        self.split.insertWidget(0, plot_widget)  # top
        plot_widget.show()

    def _dock_back_v(self, plot_widget: pg.PlotWidget):
        self._v_floating = None
        self.split.addWidget(plot_widget)  # bottom
        plot_widget.show()

    # ---------- Plot view helpers ----------
    def _apply_autoY(self, plot: pg.PlotWidget, enabled: bool):
        vb = plot.getViewBox()
        vb.enableAutoRange(pg.ViewBox.YAxis, enabled)

    def _reset_view(self):
        if self.ts:
            tmax = self.ts[-1]
            xmin = max(0.0, tmax - ROLLING_SECONDS)
            xmax = tmax if tmax > 0 else ROLLING_SECONDS
        else:
            xmin, xmax = 0.0, ROLLING_SECONDS

        def window_minmax(t, y):
            if not t or not y:
                return (0.0, 1.0)
            lo = 0
            while lo < len(t) and t[lo] < xmin:
                lo += 1
            if lo >= len(t):
                return (0.0, 1.0)
            seg = y[lo:]
            ymin = min(seg)
            ymax = max(seg)
            if ymax <= ymin:
                ymax = ymin + 1.0
            return (ymin, ymax)

        for plot in (self.plot_x, self.plot_v):
            vb = plot.getViewBox()
            vb.setXRange(xmin, xmax, padding=0.05)

        if self.autoY_x.isChecked():
            ymin, ymax = window_minmax(self.ts, self.xs)
            self.plot_x.getViewBox().setYRange(ymin, ymax, padding=0.10)
        if self.autoY_v.isChecked():
            ymin, ymax = window_minmax(self.ts, self.vs)
            self.plot_v.getViewBox().setYRange(ymin, ymax, padding=0.10)

        self._apply_autoY(self.plot_x, self.autoY_x.isChecked())
        self._apply_autoY(self.plot_v, self.autoY_v.isChecked())

    # ---------- Incoming data ----------
    @QtCore.Slot(dict)
    def _on_line(self, rec: dict):
        if rec.get("type") == "fft":
            self.plot_fft.setVisible(True)
            freq = rec.get("freq") or []; mag = rec.get("mag") or []
            self.curve_fft.setData(freq, mag)
            return

        t = time.time()
        if self.t0 is None: self.t0 = t
        relt = t - self.t0

        x = float(rec.get("x_nm", 0.0)); v = float(rec.get("v_nm_s", 0.0))
        self.ts.append(relt); self.xs.append(x); self.vs.append(v)
        self._trim()
        self.curve_x.setData(self.ts, self.xs)
        self.curve_v.setData(self.ts, self.vs)

        if self.capture_active and self.capture_writer:
            row = [ rec.get("seq"), rec.get("fs_hz"), rec.get("D"), rec.get("deltaD"),
                    rec.get("step_nm"), rec.get("x_nm"), rec.get("v_nm_s"),
                    rec.get("x_nm_ema"), rec.get("x_nm_ma"), rec.get("x_nm_env"),
                    rec.get("angle_deg"), rec.get("x2"), rec.get("y2") ]
            try: self.capture_writer.writerow(row)
            except Exception as e: self.status.showMessage(f"Capture write error: {e}", 4000)

        details = f"seq={rec.get('seq')} D={rec.get('D')} dD={rec.get('deltaD')} x={x:.3f}nm v={v:.3f}nm/s"
        ang = rec.get("angle_deg")
        if ang is not None: details += f" angle={ang:.4f}deg"
        self.status.showMessage(details, 800)

    def _trim(self):
        if not self.ts: return
        cutoff = self.ts[-1] - ROLLING_SECONDS
        i=0
        while i < len(self.ts) and self.ts[i] < cutoff: i+=1
        if i>0:
            self.ts = self.ts[i:]; self.xs = self.xs[i:]; self.vs = self.vs[i:]

    def _on_stopped(self, reason: str):
        self._set_running(False)
        if self.capture_active: self._toggle_capture(force_off=True)
        self.status.showMessage(f"Backend stopped: {reason}", 3000)

    def closeEvent(self, event):
        if self.capture_active: self._toggle_capture(force_off=True)
        super().closeEvent(event)

# ------------------ Entrypoint ------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.resize(1100, 820); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
