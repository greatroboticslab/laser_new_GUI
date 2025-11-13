#!/usr/bin/env python3
"""
Enhanced UMD2 backend: serial/file -> tokens -> computations -> JSONL/CSV

Adds --raw-log <path> to write raw (pre-parse) serial lines when using --serial.
"""

import sys
import os
import re
import argparse
import json
import csv
import math
from collections import deque
from typing import Optional, List

HeaderFS_RE = re.compile(r'^\s*Sample\s+Frequency\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*Hz\s*$', re.IGNORECASE)
Tok_RE = re.compile(r'([A-Za-z]+)\s*:\s*(-?\d+(?:\.\d+)?)\b')

def parse_args(argv=None):
    p = argparse.ArgumentParser(description="UMD2 parser & calculator (enhanced)")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--serial", help="Serial device path (e.g., /dev/tty.usbmodem1101)")
    p.add_argument("--baud", type=int, default=115200, help="Baud rate for --serial (default: 115200)")
    src.add_argument("--file", help="Input file path; if omitted and --serial not set, read stdin")

    # Core
    p.add_argument("--fs", type=float, default=0.0, help="Override sample frequency in Hz (default: 0=auto/header or 1000)")
    p.add_argument("--emit", choices=["every","onstep"], default="every", help="Emit every sample or only on deltaD != 0 (default: every)")
    p.add_argument("--decimate", type=int, default=1, help="After emit filter, output only every Nth kept record (default: 1)")

    # Displacement scaling
    p.add_argument("--stepnm", type=float, default=None, help="Explicit nm per deltaD (overrides lambda/scale-div if set)")
    p.add_argument("--lambda-nm", type=float, default=632.991, help="Laser wavelength in nm (default HeNe ~632.991)")
    p.add_argument("--scale-div", type=int, default=8, choices=[1,2,4,8], help="Interferometer division (1,2,4,8); default 8")
    p.add_argument("--startnm", type=float, default=0.0, help="Starting baseline for x_nm (default: 0)")
    p.add_argument("--straight-mult", type=float, default=1.0, help="Multiply x_nm by this (straightness correction).")

    # Mode: displacement or angle
    p.add_argument("--mode", choices=["displacement","angle"], default="displacement")
    p.add_argument("--angle-norm-nm", type=float, default=1.0)
    p.add_argument("--angle-corr", type=float, default=1.0)
    # angle = asin(clamp(x_nm/angle_norm_nm, -1..1)) * angle_corr * 57.296

    # Smoothing
    p.add_argument("--ema-alpha", type=float, default=0.0)
    p.add_argument("--ma-window", type=int, default=0)

    # Environmental compensation (linear)
    p.add_argument("--env-temp", type=float, default=None)
    p.add_argument("--env-temp0", type=float, default=None)
    p.add_argument("--env-ktemp", type=float, default=0.0)
    p.add_argument("--env-press", type=float, default=None)
    p.add_argument("--env-press0", type=float, default=None)
    p.add_argument("--env-kpress", type=float, default=0.0)
    p.add_argument("--env-hum", type=float, default=None)
    p.add_argument("--env-hum0", type=float, default=None)
    p.add_argument("--env-khum", type=float, default=0.0)

    # FFT
    p.add_argument("--fft-len", type=int, default=0)
    p.add_argument("--fft-every", type=int, default=0)
    p.add_argument("--fft-signal", choices=["x","v"], default="x")

    # Secondary axis input (optional)
    p.add_argument("--enable-xy", action="store_true")

    # Output
    p.add_argument("--out", choices=["jsonl","csv"], default="jsonl")
    p.add_argument("--log", type=str, default=None, help="Append processed CSV to this path.")

    # NEW: raw serial sidecar log (USB only)
    p.add_argument("--raw-log", type=str, default=None, help="Write raw pre-parse serial lines to this path (serial mode only).")

    return p.parse_args(argv)

def iter_lines_serial(port: str, baud: int):
    try:
        import serial  # pyserial
    except Exception:
        print("ERROR: pyserial is required for --serial. Install with: pip install pyserial", file=sys.stderr)
        sys.exit(2)
    ser = serial.Serial(port, baudrate=baud, timeout=0.2)
    try:
        buf = b""
        while True:
            chunk = ser.read(4096)
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode(errors="ignore")
            else:
                pass
    finally:
        try: ser.close()
        except: pass

def iter_lines_file(path: str):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            yield line

def iter_lines_stdin():
    for line in sys.stdin:
        yield line

def parse_line_tokens(line: str):
    out = {}
    for key, val in Tok_RE.findall(line):
        k = key.upper()
        try:
            v = float(val) if '.' in val else int(val)
        except ValueError:
            continue
        out[k] = v
    return out

def maybe_extract_fs(line: str) -> Optional[float]:
    m = HeaderFS_RE.match(line)
    if m:
        try:
            return float(m.group(1))
        except:
            return None
    return None

def clamp(val, lo, hi):
    return lo if val < lo else hi if val > hi else val

def compute_step_nm(args) -> float:
    if args.stepnm is not None:
        return float(args.stepnm)
    div = max(1, int(args.scale_div))
    return float(args.lambda_nm) / div

def apply_env(x_nm: float, args) -> float:
    scale = 1.0
    if args.env_temp is not None and args.env_temp0 is not None and args.env_ktemp != 0.0:
        scale *= (1.0 + args.env_ktemp * (args.env_temp - args.env_temp0))
    if args.env_press is not None and args.env_press0 is not None and args.env_kpress != 0.0:
        scale *= (1.0 + args.env_kpress * (args.env_press - args.env_press0))
    if args.env_hum is not None and args.env_hum0 is not None and args.env_khum != 0.0:
        scale *= (1.0 + args.env_khum * (args.env_hum - args.env_hum0))
    return x_nm * scale

def angle_from_displacement(x_nm: float, args) -> float:
    if args.angle_norm_nm == 0:
        return 0.0
    norm = clamp(x_nm / args.angle_norm_nm, -1.0, 1.0)
    return math.asin(norm) * args.angle_corr * 57.296

def main(argv=None):
    args = parse_args(argv)

    # Source
    if args.serial:
        source = iter_lines_serial(args.serial, args.baud)
    elif args.file:
        source = iter_lines_file(args.file)
    else:
        source = iter_lines_stdin()

    fs_hz = args.fs if args.fs > 0 else 0.0
    step_nm_per_count = compute_step_nm(args)
    x_nm = float(args.startnm)
    prevD = None

    # Smoothing
    ema_x = None
    ma_buf: deque = deque(maxlen=args.ma_window if args.ma_window > 0 else 1)

    # FFT
    fft_buf: List[float] = []
    emitted = 0

    # Logging (processed)
    log_file = None
    log_writer = None
    if args.log:
        log_exists = os.path.exists(args.log)
        log_file = open(args.log, "a", newline="")
        log_writer = csv.writer(log_file)
        if not log_exists:
            log_writer.writerow(["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s",
                                 "x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"])

    # Stdout CSV
    stdout_writer = None
    if args.out == "csv":
        stdout_writer = csv.writer(sys.stdout, lineterminator="\n")
        stdout_writer.writerow(["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s",
                                "x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"])

    # NEW: raw serial sidecar file (serial mode only)
    raw_log_fh = None
    if args.raw_log and args.serial:
        try:
            raw_log_fh = open(args.raw_log, "a", encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"WARNING: cannot open raw-log file '{args.raw_log}': {e}", file=sys.stderr)
            raw_log_fh = None

    for raw in source:
        # Write raw line BEFORE any parsing (USB mode only)
        if raw_log_fh is not None:
            try:
                raw_log_fh.write(raw)
                if not raw.endswith("\n"):
                    raw_log_fh.write("\n")
            except Exception:
                pass

        line = raw.strip()
        if not line:
            continue

        fs_found = maybe_extract_fs(line)
        if fs_found:
            fs_hz = fs_found
            continue

        toks = parse_line_tokens(line)
        # D preference: DIFF > D
        D = None
        if "DIFF" in toks:
            D = int(toks["DIFF"])
        elif "D" in toks:
            D = int(toks["D"])
        else:
            continue

        N = int(toks.get("N", 0))

        x2 = float(toks["X"]) if ("X" in toks and args.enable_xy) else None
        y2 = float(toks["Y"]) if ("Y" in toks and args.enable_xy) else None

        if fs_hz <= 0.0:
            fs_hz = 1000.0

        if prevD is None:
            dD = 0
            prevD = D
        else:
            dD = D - prevD
            prevD = D

        dx = step_nm_per_count * float(dD)
        x_nm = (x_nm + dx) * args.straight_mult
        v_nm_s = dx * fs_hz

        x_nm_ema = None
        if args.ema_alpha > 0.0:
            if ema_x is None:
                ema_x = x_nm
            else:
                ema_x = args.ema_alpha * x_nm + (1.0 - args.ema_alpha) * ema_x
            x_nm_ema = ema_x

        x_nm_ma = None
        if args.ma_window > 0:
            if ma_buf.maxlen != args.ma_window:
                ma_buf = deque(ma_buf, maxlen=args.ma_window)
            ma_buf.append(x_nm)
            x_nm_ma = sum(ma_buf) / len(ma_buf)

        x_nm_env = apply_env(x_nm, args)

        angle_deg = None
        if args.mode == "angle":
            angle_deg = angle_from_displacement(x_nm, args)

        keep = True
        if args.emit == "onstep":
            keep = (dD != 0)

        if keep:
            emitted += 1
            if args.decimate > 1 and (emitted % args.decimate) != 0:
                keep = False

        if not keep:
            continue

        rec = {
            "seq": int(N),
            "fs_hz": float(fs_hz),
            "D": int(D),
            "deltaD": int(dD),
            "step_nm": float(dx),
            "x_nm": float(x_nm),
            "v_nm_s": float(v_nm_s),
            "x_nm_ema": (float(x_nm_ema) if x_nm_ema is not None else None),
            "x_nm_ma": (float(x_nm_ma) if x_nm_ma is not None else None),
            "x_nm_env": float(x_nm_env),
            "angle_deg": (float(angle_deg) if angle_deg is not None else None),
            "x2": (float(x2) if x2 is not None else None),
            "y2": (float(y2) if y2 is not None else None),
        }

        if args.out == "jsonl":
            sys.stdout.write(json.dumps(rec, separators=(",",":")) + "\n")
        else:
            stdout_writer.writerow([rec["seq"], rec["fs_hz"], rec["D"], rec["deltaD"], rec["step_nm"], rec["x_nm"],
                                    rec["v_nm_s"], rec["x_nm_ema"], rec["x_nm_ma"], rec["x_nm_env"],
                                    rec["angle_deg"], rec["x2"], rec["y2"]])

        if log_writer:
            log_writer.writerow([rec["seq"], rec["fs_hz"], rec["D"], rec["deltaD"], rec["step_nm"], rec["x_nm"],
                                 rec["v_nm_s"], rec["x_nm_ema"], rec["x_nm_ma"], rec["x_nm_env"],
                                 rec["angle_deg"], rec["x2"], rec["y2"]])

        # FFT snapshots (optional)
        if args.fft_len > 0 and args.fft_every > 0:
            try:
                import numpy as np
            except Exception:
                np = None
            if np is not None:
                sigval = x_nm if args.fft_signal == "x" else v_nm_s
                fft_buf.append(sigval)
                if len(fft_buf) >= args.fft_len and (emitted % args.fft_every) == 0:
                    buf = np.array(fft_buf[-args.fft_len:], dtype=np.float64)
                    w = np.hanning(len(buf))
                    bufw = buf * w
                    spec = np.fft.rfft(bufw)
                    mag = np.abs(spec)
                    freq = np.fft.rfftfreq(len(bufw), d=(1.0/fs_hz if fs_hz>0 else 0.001))
                    sys.stdout.write(json.dumps({
                        "type":"fft",
                        "signal": args.fft_signal,
                        "fs_hz": float(fs_hz),
                        "freq": freq.tolist(),
                        "mag": mag.tolist()
                    }, separators=(",",":")) + "\n")

    if log_file:
        try: log_file.flush(); log_file.close()
        except: pass
    if raw_log_fh:
        try: raw_log_fh.flush(); raw_log_fh.close()
        except: pass

if __name__ == "__main__":
    main()
