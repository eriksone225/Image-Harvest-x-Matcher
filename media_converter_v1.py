#!/usr/bin/env python3
"""
Media Converter v1 for Image/Video Harvester x Matcher

Converts downloaded .ts files to .mp4 using FFmpeg.

Modes:
- remux: fast container conversion, no re-encode.
- reencode: slower, repairs more broken/incomplete timestamp issues.
- auto: try remux first; if FFmpeg fails, try re-encode.

This does not decrypt DRM/encrypted HLS. It only converts files that already exist locally.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, timezone


VIDEO_INPUT_EXTS = {".ts", ".m2ts", ".mpegts"}
PLAYLIST_EXTS = {".m3u8"}


@dataclass
class ConvertRecord:
    input_path: str
    output_path: str
    mode_requested: str
    mode_used: str
    status: str
    returncode: int
    message: str
    started_utc: str
    finished_utc: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_ffmpeg(explicit: str = "") -> str:
    if explicit:
        p = Path(explicit)
        if p.exists():
            return str(p)
        found = shutil.which(explicit)
        if found:
            return found
    found = shutil.which("ffmpeg")
    if found:
        return found
    common = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    for item in common:
        if Path(item).exists():
            return item
    return ""


def output_for(input_path: Path, output_dir: Path | None = None) -> Path:
    target_dir = output_dir if output_dir else input_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / (input_path.stem + ".mp4")


def run_ffmpeg(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        msg = (p.stdout or "") + ("\n" if p.stdout and p.stderr else "") + (p.stderr or "")
        return p.returncode, msg.strip()
    except Exception as e:
        return 999, str(e)


def remux_cmd(ffmpeg: str, src: Path, dst: Path, overwrite: bool) -> list[str]:
    return [
        ffmpeg,
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts",
        "-i", str(src),
        "-map", "0",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(dst),
    ]


def reencode_cmd(ffmpeg: str, src: Path, dst: Path, overwrite: bool) -> list[str]:
    return [
        ffmpeg,
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel", "error",
        "-fflags", "+genpts",
        "-err_detect", "ignore_err",
        "-i", str(src),
        "-map", "0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(dst),
    ]


def playlist_cmd(ffmpeg: str, src: Path, dst: Path, overwrite: bool) -> list[str]:
    # For local accessible unencrypted m3u8 playlists. Does not decrypt DRM.
    return [
        ffmpeg,
        "-y" if overwrite else "-n",
        "-hide_banner",
        "-loglevel", "error",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-allowed_extensions", "ALL",
        "-i", str(src),
        "-map", "0",
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(dst),
    ]


def convert_one(ffmpeg: str, src: Path, dst: Path, mode: str, overwrite: bool) -> ConvertRecord:
    started = now_iso()
    if dst.exists() and not overwrite:
        return ConvertRecord(str(src), str(dst), mode, "", "skipped", 0, "Output already exists. Use --overwrite to replace.", started, now_iso())

    attempts = []
    suffix = src.suffix.lower()
    if suffix in PLAYLIST_EXTS:
        attempts.append(("playlist_remux", playlist_cmd(ffmpeg, src, dst, overwrite)))
        if mode in {"reencode", "auto"}:
            attempts.append(("reencode", reencode_cmd(ffmpeg, src, dst, overwrite)))
    elif mode == "remux":
        attempts.append(("remux", remux_cmd(ffmpeg, src, dst, overwrite)))
    elif mode == "reencode":
        attempts.append(("reencode", reencode_cmd(ffmpeg, src, dst, overwrite)))
    else:
        attempts.append(("remux", remux_cmd(ffmpeg, src, dst, overwrite)))
        attempts.append(("reencode", reencode_cmd(ffmpeg, src, dst, overwrite)))

    last_code = 1
    last_msg = ""
    last_mode = ""
    for used_mode, cmd in attempts:
        if dst.exists() and overwrite:
            try:
                dst.unlink()
            except Exception:
                pass
        code, msg = run_ffmpeg(cmd)
        last_code, last_msg, last_mode = code, msg, used_mode
        if code == 0 and dst.exists() and dst.stat().st_size > 0:
            return ConvertRecord(str(src), str(dst), mode, used_mode, "ok", code, msg[-1000:], started, now_iso())

    return ConvertRecord(str(src), str(dst), mode, last_mode, "failed", last_code, last_msg[-1500:], started, now_iso())


def discover_inputs(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path]
    pattern = "**/*" if recursive else "*"
    files = []
    for p in path.glob(pattern):
        if p.is_file() and p.suffix.lower() in (VIDEO_INPUT_EXTS | PLAYLIST_EXTS):
            files.append(p)
    return sorted(files, key=lambda p: str(p).lower())


def write_csv(records: list[ConvertRecord], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(ConvertRecord.__dataclass_fields__.keys())
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for r in records:
            wr.writerow(asdict(r))


def main() -> int:
    ap = argparse.ArgumentParser(description="Convert harvested TS/M3U8 media to MP4 using FFmpeg.")
    ap.add_argument("input", help="Input .ts/.m3u8 file or folder containing media files")
    ap.add_argument("--output-dir", default="", help="Optional output folder for MP4 files. Defaults beside each input.")
    ap.add_argument("--ffmpeg-path", default="", help="Path to ffmpeg.exe or command name. Default: auto-detect ffmpeg in PATH.")
    ap.add_argument("--mode", choices=["auto", "remux", "reencode"], default="auto")
    ap.add_argument("--recursive", action="store_true", help="Search folders recursively")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--csv", default="", help="Conversion report CSV path")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg_path)
    if not ffmpeg:
        print("[fatal] FFmpeg was not found.")
        print("Install FFmpeg and add it to PATH, or set --ffmpeg-path C:\\\\path\\\\to\\\\ffmpeg.exe")
        return 2

    inp = Path(args.input)
    if not inp.exists():
        print(f"[fatal] Input not found: {inp}")
        return 2

    output_dir = Path(args.output_dir) if args.output_dir else None
    files = discover_inputs(inp, args.recursive)
    if not files:
        print("[done] No .ts/.m2ts/.mpegts/.m3u8 files found.")
        return 0

    print(f"[ffmpeg] {ffmpeg}")
    print(f"[convert] files={len(files)} mode={args.mode}")

    records: list[ConvertRecord] = []
    for i, src in enumerate(files, start=1):
        dst = output_for(src, output_dir)
        print(f"[convert] {i}/{len(files)} {src.name} -> {dst.name}")
        rec = convert_one(ffmpeg, src, dst, args.mode, args.overwrite)
        records.append(rec)
        print(f"          {rec.status} mode={rec.mode_used or '-'} code={rec.returncode}")

    csv_path = Path(args.csv) if args.csv else ((output_dir or inp if inp.is_dir() else inp.parent) / "mp4_conversion_report.csv")
    write_csv(records, csv_path)

    ok = sum(1 for r in records if r.status == "ok")
    skipped = sum(1 for r in records if r.status == "skipped")
    failed = sum(1 for r in records if r.status == "failed")
    print(f"[done] ok={ok} skipped={skipped} failed={failed}")
    print(f"[report] {csv_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
