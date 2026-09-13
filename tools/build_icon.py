from pathlib import Path
from PIL import Image, ImageDraw


output = Path(__file__).resolve().parents[1] / "assets" / "FolderPilot.ico"
output.parent.mkdir(parents=True, exist_ok=True)

size = 256
image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((18, 48, 238, 222), radius=32, fill="#4C7CF3")
draw.rounded_rectangle((30, 31, 126, 91), radius=20, fill="#6F98FA")
draw.rounded_rectangle((34, 82, 222, 207), radius=22, fill="#F7C95C")
draw.rounded_rectangle((48, 101, 208, 193), radius=16, fill="#FFD979")
draw.ellipse((154, 122, 204, 172), fill="#FFFFFF")
draw.rectangle((176, 160, 183, 187), fill="#FFFFFF")
image.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(output)
