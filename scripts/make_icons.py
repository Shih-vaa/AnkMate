"""Generate the PWA icons with the standard library only (placeholder artwork; swap for the school's logo).

    python scripts/make_icons.py
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web" / "static" / "web" / "icons"
BG, PAGE, GOLD = (31, 78, 121), (255, 255, 255), (232, 168, 56)


def pixel(u, v):
    """An open book with a gold spark above it. Kept inside the central 60% so it is safe as a maskable icon."""
    if 0.34 <= v <= 0.68 and (0.22 <= u <= 0.485 or 0.515 <= u <= 0.78):
        return PAGE
    if (u - 0.5) ** 2 + (v - 0.24) ** 2 <= 0.06 ** 2:
        return GOLD
    return BG


def write_png(path, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type: none
        for x in range(size):
            raw.extend(pixel((x + 0.5) / size, (y + 0.5) / size))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        write_png(OUT / f"icon-{size}.png", size)
        print("wrote", OUT / f"icon-{size}.png")
