"""Одноразово: добавить «БОТ» на аватар. Запуск: python scripts/add_bot_label_to_avatar.py"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SRC = Path(
    r"C:\Users\Dmitriy\.cursor\projects\d-podslushano\assets"
    r"\c__Users_Dmitriy_AppData_Roaming_Cursor_User_workspaceStorage_dc7a9832477528073a6fad661f291d95_images_photo_2026-03-31_11-42-18-811d28d0-478f-443d-8455-c9b76e743a7f.png"
)
OUT = Path(__file__).resolve().parent.parent / "assets" / "avatar_podslushano_bot.png"
TEXT = "БОТ"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(SRC).convert("RGBA")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    fontsize = max(64, w // 6)
    font = None
    for name in ("arialbd.ttf", "arial.ttf", "segoeuib.ttf", "segoeui.ttf"):
        try:
            font = ImageFont.truetype(str(Path(r"C:\Windows\Fonts") / name), fontsize)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (w - tw) // 2
    # Ниже лица (подбородок), с запасом под круглую обрезку аватарки
    y = int(h * 0.63)

    stroke = max(4, fontsize // 14)
    draw.text(
        (x, y),
        TEXT,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 255),
    )

    img.convert("RGB").save(OUT, format="PNG", optimize=True)
    print("Saved:", OUT)


if __name__ == "__main__":
    main()
