"""Render the README demo GIF.

Renders a stylised terminal session showing the three things that make this
server different: a local warehouse you own, compact server-side analysis, and
answers that survive the network going away.

All figures shown are real output from a live store (see DEMO below); re-run
the tools and update DEMO if the response shapes change.

Usage:
    uv run --with pillow python scripts/render_demo_gif.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --- canvas -----------------------------------------------------------------

W, H = 820, 484
TITLEBAR = 34
PAD_X, PAD_Y = 26, 16
LINE_H = 22
VISIBLE = (H - TITLEBAR - 2 * PAD_Y) // LINE_H

BG = "#0f1419"
TITLEBAR_BG = "#171d24"
DOTS = ("#ff5f56", "#ffbd2e", "#27c93f")

C = {
    "title": "#6b7785",
    "prompt": "#7ee787",
    "user": "#e6edf3",
    "tool": "#79c0ff",
    "head": "#8b949e",
    "val": "#e6edf3",
    "note": "#d29922",
    "warn": "#f85149",
    "dim": "#586069",
}

FONT_PATH = "C:/Windows/Fonts/consola.ttf"
FONT_BOLD_PATH = "C:/Windows/Fonts/consolab.ttf"
FONT = ImageFont.truetype(FONT_PATH, 15)
FONT_BOLD = ImageFont.truetype(FONT_BOLD_PATH, 15)
FONT_TITLE = ImageFont.truetype(FONT_PATH, 13)
CHAR_W = FONT.getlength("M")

# --- the session ------------------------------------------------------------
# (style, text) — style keys map into C above; "user" lines are typed out.

DEMO = [
    ("dim", "  connected: garmin-local-mcp  ~  12 tools"),
    ("gap", ""),
    ("type", "how much of my Garmin history is on this machine?"),
    ("gap", ""),
    ("tool", "  >> sync_status()"),
    ("gap", ""),
    ("head", "     table              rows   first         last"),
    ("val", "     daily_wellness      391   2025-07-08    2026-08-02"),
    ("val", "     sleep               373   2025-07-09    2026-08-02"),
    ("val", "     hrv                 384   2025-07-09    2026-08-02"),
    ("val", "     training_status     391   2025-07-08    2026-08-02"),
    ("val", "     activities           91   2025-07-09    2026-08-02"),
    ("gap", ""),
    ("note", "  13 months, 1,630 rows, in SQLite on your own disk."),
    ("hold", "1400"),
    ("gap", ""),
    ("type", "over the last 3 months, does my HRV track my resting heart rate?"),
    ("gap", ""),
    ("tool", "  >> correlate(hrv, resting_hr, scan_lags=True)"),
    ("gap", ""),
    ("val", "     n = 92     pearson r = -0.677     best lag = 0 days"),
    ("gap", ""),
    ("note", "  One tool call. 146 bytes back. No raw payload in context."),
    ("hold", "1600"),
    ("gap", ""),
    ("warn", "  [ network disconnected ]"),
    ("hold", "1800"),
    ("gap", ""),
    ("type", "same question, offline"),
    ("gap", ""),
    ("tool", "  >> correlate(hrv, resting_hr, scan_lags=True)"),
    ("gap", ""),
    ("val", "     n = 92     pearson r = -0.677     best lag = 0 days"),
    ("gap", ""),
    ("note", "  Sync once. Analyze forever."),
    ("hold", "2600"),
]

TYPE_CHARS_PER_FRAME = 3
D_TYPE = 45
D_LINE = 95
D_GAP = 40


def new_frame(lines: list[tuple[str, str]], cursor_at: tuple[int, int] | None):
    """Render one frame from the current scrollback."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, TITLEBAR], fill=TITLEBAR_BG)
    for i, colour in enumerate(DOTS):
        cx = 20 + i * 18
        d.ellipse([cx - 5, TITLEBAR // 2 - 5, cx + 5, TITLEBAR // 2 + 5], fill=colour)
    title = "garmin-local-mcp"
    d.text(
        ((W - FONT_TITLE.getlength(title)) / 2, TITLEBAR / 2 - 8),
        title,
        font=FONT_TITLE,
        fill=C["title"],
    )

    # Bottom-anchored, so early frames are not mostly empty canvas.
    shown = lines[-VISIBLE:]
    offset = len(lines) - len(shown)
    top = VISIBLE - len(shown)
    for i, (style, text) in enumerate(shown):
        y = TITLEBAR + PAD_Y + (top + i) * LINE_H
        if style == "user":
            d.text((PAD_X, y), ">", font=FONT_BOLD, fill=C["prompt"])
            d.text((PAD_X + CHAR_W * 2, y), text, font=FONT_BOLD, fill=C["user"])
        else:
            font = FONT_BOLD if style in ("note", "warn") else FONT
            d.text((PAD_X, y), text, font=font, fill=C.get(style, C["val"]))

    if cursor_at is not None:
        row, col = cursor_at
        row = row - offset + top
        if 0 <= row < VISIBLE:
            x = PAD_X + CHAR_W * col
            y = TITLEBAR + PAD_Y + row * LINE_H
            d.rectangle([x, y + 2, x + CHAR_W - 1, y + LINE_H - 4], fill=C["prompt"])
    return img


def build():
    frames: list[Image.Image] = []
    durations: list[int] = []
    lines: list[tuple[str, str]] = []

    def emit(img, ms):
        frames.append(img)
        durations.append(ms)

    for style, text in DEMO:
        if style == "hold":
            if frames:
                durations[-1] += int(text)
            continue
        if style == "gap":
            lines.append(("val", ""))
            emit(new_frame(lines, None), D_GAP)
            continue
        if style == "type":
            lines.append(("user", ""))
            row = len(lines) - 1
            for n in range(0, len(text) + TYPE_CHARS_PER_FRAME, TYPE_CHARS_PER_FRAME):
                partial = text[:n]
                lines[row] = ("user", partial)
                emit(new_frame(lines, (row, 2 + len(partial))), D_TYPE)
            lines[row] = ("user", text)
            emit(new_frame(lines, (row, 2 + len(text))), 500)
            continue
        lines.append((style, text))
        emit(new_frame(lines, None), D_LINE)

    out = Path(__file__).resolve().parent.parent / "docs" / "demo.gif"
    out.parent.mkdir(exist_ok=True)
    # One palette for the whole animation. Per-frame quantisation lets the
    # palette drift, which visibly recolours small elements like the dots.
    # Built from an explicit swatch so every colour is present regardless of
    # which lines happen to be on screen in any given frame.
    swatch_colours = [BG, TITLEBAR_BG, *DOTS, *C.values()]
    swatch = Image.new("RGB", (len(swatch_colours), 1))
    swatch.putdata([tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in swatch_colours])
    ref = swatch.quantize(colors=32, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    quantised = [f.quantize(palette=ref, dither=Image.Dither.NONE) for f in frames]
    quantised[0].save(
        out,
        save_all=True,
        append_images=quantised[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=1,
    )
    kb = out.stat().st_size / 1024
    print(f"{out}  ({len(frames)} frames, {kb:.0f} KB, {sum(durations) / 1000:.1f}s)")


if __name__ == "__main__":
    build()
