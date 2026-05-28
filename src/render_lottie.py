#!/usr/bin/env python3
"""Render a Lottie/.tgs animation to transparent PNG frames using rlottie.

Drop-in replacement for the old Playwright/Chromium render_lottie.mjs.
rlottie is the same native renderer Telegram uses for .tgs — no browser,
~20x faster, far less RAM. Same CLI + output contract (frame_%05d.png +
manifest.json) so bot.py's ffmpeg pipeline is unchanged.
"""
import argparse
import json
import math
from pathlib import Path

from rlottie_python import LottieAnimation


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--width", type=int, default=512)
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--max-seconds", type=float, default=6.0)
    a = ap.parse_args()

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    inp = str(a.input)
    if inp.endswith(".tgs"):
        anim = LottieAnimation.from_tgs(inp)
    else:
        anim = LottieAnimation.from_file(inp)

    total = anim.lottie_animation_get_totalframe()
    src_fps = anim.lottie_animation_get_framerate() or 60.0
    duration_src = total / src_fps if src_fps else (total / 60.0)
    duration = min(max(1.0 / max(src_fps, 1.0), duration_src), a.max_seconds)
    frame_count = max(1, math.ceil(duration * a.fps))

    for i in range(frame_count):
        pos_seconds = i / a.fps
        src_frame = min(total - 1, max(0, round(pos_seconds * src_fps)))
        img = anim.render_pillow_frame(
            frame_num=src_frame, width=a.width, height=a.height
        )
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        img.save(out_dir / f"frame_{i + 1:05d}.png", "PNG")

    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "fps": a.fps,
                "source_fps": src_fps,
                "frame_count": frame_count,
                "duration": duration,
                "width": a.width,
                "height": a.height,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
