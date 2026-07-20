#!/usr/bin/env python3
"""Render ANSI-colored text (read from stdin) to a PNG.

Reproduces the terminal look of the telemetry report so the colored chart
can be shown inline in a chat, where ANSI escape codes are otherwise
stripped. Handles 24-bit (38;2;r;g;b) and 256-color (38;5;n) foreground
SGR codes plus reset (0); everything else is ignored.

Usage: report.py | ansi2png.py OUTPUT.png
"""
import re
import sys

from PIL import Image, ImageDraw, ImageFont

BG = (13, 13, 13)  # near-black terminal background
DEFAULT_FG = (204, 204, 204)  # light gray for uncolored text
FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
FONT_SIZE = 28
PAD = 24
LINE_SPACING = 8

SGR = re.compile(r"\x1b\[([0-9;]*)m")

# xterm-256 -> rgb for the 6x6x6 cube and gray ramp (16..255)
_CUBE = (0, 95, 135, 175, 215, 255)


def _xterm256_rgb(n):
    if n < 16:
        base = [(0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
                (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
                (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
                (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255)]
        return base[n]
    if n <= 231:
        n -= 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    v = 8 + 10 * (n - 232)
    return (v, v, v)


def parse_sgr(params, cur):
    """Apply an SGR parameter list to the current fg color, return new fg."""
    nums = [int(p) if p else 0 for p in params.split(";")] if params else [0]
    i = 0
    fg = cur
    while i < len(nums):
        n = nums[i]
        if n == 0:
            fg = DEFAULT_FG
        elif n == 39:
            fg = DEFAULT_FG
        elif n == 38 and i + 1 < len(nums):
            mode = nums[i + 1]
            if mode == 2 and i + 4 < len(nums):
                fg = (nums[i + 2], nums[i + 3], nums[i + 4])
                i += 4
            elif mode == 5 and i + 2 < len(nums):
                fg = _xterm256_rgb(nums[i + 2])
                i += 2
        i += 1
    return fg


def tokenize_line(line):
    """Yield (text, color) runs for one line."""
    runs = []
    fg = DEFAULT_FG
    pos = 0
    for m in SGR.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], fg))
        fg = parse_sgr(m.group(1), fg)
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], fg))
    return runs


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "report.png"
    raw = sys.stdin.read().rstrip("\n")
    lines = raw.split("\n")

    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    # Cell metrics from a wide glyph; Menlo is monospace.
    probe = Image.new("RGB", (10, 10))
    d = ImageDraw.Draw(probe)
    bbox = d.textbbox((0, 0), "M", font=font)
    cw = bbox[2] - bbox[0]
    ascent, descent = font.getmetrics()
    ch = ascent + descent + LINE_SPACING

    cols = max((len(SGR.sub("", ln)) for ln in lines), default=1)
    W = PAD * 2 + cw * cols
    H = PAD * 2 + ch * len(lines)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    y = PAD
    for ln in lines:
        x = PAD
        for text, color in tokenize_line(ln):
            draw.text((x, y), text, font=font, fill=color)
            x += cw * len(text)
        y += ch

    img.save(out)
    print(out)


if __name__ == "__main__":
    main()
