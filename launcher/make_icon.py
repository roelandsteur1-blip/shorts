"""Draws launcher/dashboard.ico (a red rounded square with a white play triangle)."""
from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(229, 45, 39))
d.polygon([(96, 70), (96, 186), (194, 128)], fill="white")
img.save(Path(__file__).with_name("dashboard.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
