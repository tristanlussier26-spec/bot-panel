"""Image « qui est en jeu » : la tête du skin de chaque membre connecté, avec son pseudo et son grade.

Fait avec Pillow, dans un thread (pas dans la boucle du bot). Les têtes viennent du même CDN que
nationsglory.fr (files.recube.gg), sans clé : ça compte pas dans la limite de l'API.
"""
from __future__ import annotations

import io

RANK_COLORS = {"LEADER": (241, 196, 15), "OFFICER": (52, 152, 219), "MEMBER": (46, 204, 113), "RECRUIT": (149, 165, 166)}
BG = (30, 31, 34)
CARD = (43, 45, 49)
TEXT = (242, 243, 245)
CELL_W, CELL_H, HEAD = 128, 146, 84
MAX_SHOWN = 48


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)  # Pillow 10.1+ : police intégrée, à la taille voulue
    except TypeError:
        return ImageFont.load_default()


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def render(people: list[dict], columns: int = 6) -> bytes:
    """people : [{"name", "role", "label", "face": bytes | None}] -> PNG."""
    from PIL import Image, ImageDraw

    shown = people[:MAX_SHOWN]
    extra = len(people) - len(shown)
    tiles = len(shown) + (1 if extra else 0)
    cols = max(1, min(columns, tiles))
    rows = max(1, -(-tiles // cols))
    pad = 12
    img = Image.new("RGB", (pad * 2 + cols * CELL_W, pad * 2 + rows * CELL_H), BG)
    draw = ImageDraw.Draw(img)
    name_font, label_font, big_font = _font(16), _font(13), _font(28)
    for i, p in enumerate(shown):
        x = pad + (i % cols) * CELL_W
        y = pad + (i // cols) * CELL_H
        color = RANK_COLORS.get(p.get("role"), (114, 118, 125))
        draw.rounded_rectangle((x + 4, y + 4, x + CELL_W - 4, y + CELL_H - 4), radius=12, fill=CARD)
        hx, hy = x + (CELL_W - HEAD) // 2, y + 12
        draw.rounded_rectangle((hx - 4, hy - 4, hx + HEAD + 4, hy + HEAD + 4), radius=8, fill=color)
        head = None
        if p.get("face"):
            try:
                head = Image.open(io.BytesIO(p["face"])).convert("RGBA").resize((HEAD, HEAD), Image.NEAREST)
            except Exception:  # noqa: BLE001 - image illisible : on met une tête vide
                head = None
        if head is None:
            head = Image.new("RGBA", (HEAD, HEAD), (88, 101, 242, 255))
            ImageDraw.Draw(head).text((HEAD // 2, HEAD // 2), (p.get("name") or "?")[:1].upper(), font=big_font,
                                      fill=TEXT, anchor="mm")
        img.paste(head, (hx, hy), head)
        name = _fit(draw, p.get("name") or "?", name_font, CELL_W - 14)
        draw.text((x + CELL_W // 2, hy + HEAD + 18), name, font=name_font, fill=TEXT, anchor="mm")
        label = _fit(draw, p.get("label") or "", label_font, CELL_W - 14)
        draw.text((x + CELL_W // 2, hy + HEAD + 38), label, font=label_font, fill=color, anchor="mm")
    if extra:
        i = len(shown)
        x, y = pad + (i % cols) * CELL_W, pad + (i // cols) * CELL_H
        draw.rounded_rectangle((x + 4, y + 4, x + CELL_W - 4, y + CELL_H - 4), radius=12, fill=CARD)
        draw.text((x + CELL_W // 2, y + CELL_H // 2), f"+{extra}", font=big_font, fill=TEXT, anchor="mm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
