#!/usr/bin/env python3
"""Generate reusable, low-bandwidth motion assets for the digest email.

Pillow is a development-only dependency. The generated GIF/PNG files are
committed to the repository; the scheduled digest has no extra dependency.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "email"

INK = (5, 14, 24)
PANEL = (7, 19, 31)
CYAN = (103, 216, 243)
ICE = (216, 248, 255)
BLUE = (113, 136, 255)
GRID = (31, 74, 96)
FRAME_DURATION_MS = 100


def glow_dot(image: Image.Image, xy: tuple[int, int], color: tuple[int, int, int], radius: int) -> None:
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    x, y = xy
    for scale, alpha in ((3.2, 18), (2.2, 35), (1.5, 65)):
        r = max(1, int(radius * scale))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(*color, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(max(2, radius * 2)))
    image.alpha_composite(glow)
    draw = ImageDraw.Draw(image)
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, 238))


def rgba_canvas(size: tuple[int, int], color: tuple[int, int, int] = PANEL) -> Image.Image:
    return Image.new("RGBA", size, (*color, 255))


def palette_frames(frames: list[Image.Image], colors: int = 48) -> list[Image.Image]:
    palette = frames[0].convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
    return [frame.convert("RGB").quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for frame in frames]


def save_motion(name: str, frames: list[Image.Image], duration: int) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    frames[0].convert("RGB").save(OUTPUT / f"{name}-static.png", optimize=True)
    gif_frames = palette_frames(frames)
    gif_frames[0].save(
        OUTPUT / f"{name}.gif",
        save_all=True,
        append_images=gif_frames[1:],
        duration=duration,
        loop=0,
        disposal=1,
        optimize=True,
    )


def header_spectrum() -> None:
    width, height = 1280, 40
    frames: list[Image.Image] = []
    frame_count = 60
    segments = ((30, 190), (215, 285), (330, 540), (570, 760), (810, 940), (970, 1220))
    pulse_x = (305, 785, 950)
    for frame_index in range(frame_count):
        image = rgba_canvas((width, height), INK)
        draw = ImageDraw.Draw(image)
        y = height // 2
        for index, (start, end) in enumerate(segments):
            color = GRID if index % 2 == 0 else (39, 98, 124)
            draw.line((start, y, end, y), fill=(*color, 220), width=2)
        for x in pulse_x:
            draw.line((x, y - 10, x, y + 10), fill=(*CYAN, 210), width=3)
        progress = frame_index / (frame_count - 1)
        sweep_x = int(-160 + progress * (width + 320))
        trail = Image.new("RGBA", image.size, (0, 0, 0, 0))
        trail_draw = ImageDraw.Draw(trail)
        for offset in range(160):
            alpha = int(145 * (1 - offset / 160) ** 2)
            trail_draw.line((sweep_x - offset, y, sweep_x - offset + 2, y), fill=(*CYAN, alpha), width=3)
        trail = trail.filter(ImageFilter.GaussianBlur(2))
        image.alpha_composite(trail)
        if 0 <= sweep_x < width:
            glow_dot(image, (sweep_x, y), ICE, 3)
        frames.append(image)
    save_motion("signal-spectrum", frames, FRAME_DURATION_MS)


def trend_radar() -> None:
    width, height = 1200, 360
    frames: list[Image.Image] = []
    frame_count = 72
    center = (width // 2, height // 2)
    nodes = ((790, 105, CYAN), (340, 258, BLUE), (855, 265, ICE))
    for frame_index in range(frame_count):
        image = rgba_canvas((width, height))
        draw = ImageDraw.Draw(image)
        cx, cy = center
        draw.line((110, cy, width - 110, cy), fill=(*GRID, 145), width=2)
        draw.line((cx, 34, cx, height - 34), fill=(*GRID, 105), width=2)
        draw.ellipse((cx - 250, cy - 95, cx + 250, cy + 95), outline=(*GRID, 190), width=2)
        draw.ellipse((cx - 170, cy - 145, cx + 170, cy + 145), outline=(*BLUE, 90), width=2)
        for x, y, color in nodes:
            glow_dot(image, (x, y), color, 5)
        angle = -math.pi + frame_index / frame_count * math.tau
        length = 295
        end = (int(cx + math.cos(angle) * length), int(cy + math.sin(angle) * length))
        sweep = Image.new("RGBA", image.size, (0, 0, 0, 0))
        sweep_draw = ImageDraw.Draw(sweep)
        for trail_index in range(6, 0, -1):
            trail_angle = angle - math.radians(trail_index * 3.2)
            trail_end = (
                int(cx + math.cos(trail_angle) * length),
                int(cy + math.sin(trail_angle) * length),
            )
            trail_alpha = int(16 + (6 - trail_index) * 13)
            sweep_draw.line((cx, cy, *trail_end), fill=(*CYAN, trail_alpha), width=2)
        sweep_draw.line((cx, cy, *end), fill=(*CYAN, 158), width=3)
        sweep = sweep.filter(ImageFilter.GaussianBlur(3))
        image.alpha_composite(sweep)
        glow_dot(image, center, ICE, 7)
        orbit = frame_index / frame_count * math.tau
        probe = (int(cx + math.cos(orbit) * 225), int(cy + math.sin(orbit) * 78))
        glow_dot(image, probe, BLUE, 4)
        frames.append(image)
    save_motion("trend-radar", frames, FRAME_DURATION_MS)


def radar_horizon() -> None:
    width, height = 1200, 244
    frames: list[Image.Image] = []
    frame_count = 80
    center_x = width // 2
    base_y = height - 14
    for frame_index in range(frame_count):
        image = rgba_canvas((width, height), INK)
        draw = ImageDraw.Draw(image)
        draw.line((120, base_y, width - 120, base_y), fill=(*GRID, 145), width=2)
        for radius_x, radius_y, color in (
            (420, 176, GRID),
            (300, 128, (49, 105, 130)),
            (185, 82, (73, 91, 157)),
        ):
            draw.arc(
                (center_x - radius_x, base_y - radius_y, center_x + radius_x, base_y + radius_y),
                180,
                360,
                fill=(*color, 165),
                width=2,
            )
        progress = frame_index / (frame_count - 1)
        angle = math.radians(195 + progress * 150)
        edge_fade = min(1.0, progress / 0.14, (1.0 - progress) / 0.14)
        end = (int(center_x + math.cos(angle) * 355), int(base_y + math.sin(angle) * 160))
        sweep = Image.new("RGBA", image.size, (0, 0, 0, 0))
        sweep_draw = ImageDraw.Draw(sweep)
        for trail_index in range(5, 0, -1):
            trail_angle = angle - math.radians(trail_index * 2.8)
            trail_end = (
                int(center_x + math.cos(trail_angle) * 355),
                int(base_y + math.sin(trail_angle) * 160),
            )
            trail_alpha = int((12 + (5 - trail_index) * 12) * edge_fade)
            sweep_draw.line(
                (center_x, base_y, *trail_end),
                fill=(*CYAN, trail_alpha),
                width=2,
            )
        sweep_draw.line(
            (center_x, base_y, *end),
            fill=(*CYAN, int(150 * edge_fade)),
            width=3,
        )
        sweep = sweep.filter(ImageFilter.GaussianBlur(3))
        image.alpha_composite(sweep)
        phase = frame_index / frame_count * math.tau
        glow_dot(image, (365, 98), CYAN, 3 + int(2 * (1 + math.sin(phase)) / 2))
        glow_dot(image, (850, 145), BLUE, 3 + int(2 * (1 + math.sin(phase + 2.2)) / 2))
        glow_dot(image, (center_x, base_y - 2), ICE, 5)
        frames.append(image)
    save_motion("radar-horizon", frames, FRAME_DURATION_MS)


def main() -> None:
    header_spectrum()
    trend_radar()
    radar_horizon()
    for path in sorted(OUTPUT.iterdir()):
        print(f"{path.relative_to(ROOT)}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
