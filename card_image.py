# -*- coding: utf-8 -*-
"""Генератор превью-карточки репозитория (1200x630 PNG) под стиль канала.
Автономно (Pillow, локальные шрифты) - работает из systemd без сети, кроме
загрузки аватара владельца."""
import os
import io
import requests
from PIL import Image, ImageDraw, ImageFont

# Шрифты вложены в репо (assets/fonts) - работает на любой ОС без установки.
# Fallback на системные Linux-пути, если папку удалили.
_BASE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIR = os.path.join(_BASE, "assets", "fonts")
_SYS_FONTS = "/usr/share/fonts/truetype"


def _font_path(fname, sys_rel):
    local = os.path.join(_FONT_DIR, fname)
    return local if os.path.exists(local) else f"{_SYS_FONTS}/{sys_rel}"


LATO_BLACK = _font_path("Lato-Black.ttf", "lato/Lato-Black.ttf")
LATO_SEMIBOLD = _font_path("Lato-Semibold.ttf", "lato/Lato-Semibold.ttf")
LATO_BOLD = _font_path("Lato-Bold.ttf", "lato/Lato-Bold.ttf")
DEJAVU_SANS = _font_path("DejaVuSans.ttf", "dejavu/DejaVuSans.ttf")

W, H = 1200, 630
BG_TOP = (13, 17, 23)       # #0d1117
BG_BOT = (22, 27, 34)       # #161b22
FG = (240, 246, 252)        # #f0f6fc
MUTED = (139, 148, 158)     # #8b949e
DESC = (173, 186, 199)      # #adbac7
GOLD = (233, 179, 78)       # #E6B34E (акцент юзера)

# Цвета языков (GitHub linguist), для акцентной точки/полосы.
LANG_COLORS = {
    "Python": (53, 114, 165), "TypeScript": (49, 120, 198),
    "JavaScript": (241, 224, 90), "Rust": (222, 165, 132),
    "Go": (0, 173, 216), "C++": (243, 75, 125), "C": (85, 85, 85),
    "Java": (176, 114, 25), "HTML": (227, 76, 38), "Shell": (137, 224, 81),
    "Ruby": (112, 21, 22), "Swift": (240, 81, 56), "Kotlin": (169, 123, 255),
    "PHP": (79, 93, 149), "Dart": (0, 180, 171), "Vue": (65, 184, 131),
}


def _f(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()      # крайний fallback, чтобы не падать


def _load_fonts():
    return {
        "title": _f(LATO_BLACK, 62),
        "title_sm": _f(LATO_BLACK, 48),
        "owner": _f(LATO_SEMIBOLD, 30),
        "meta": _f(LATO_BOLD, 34),
        "badge": _f(LATO_BOLD, 26),
        "wm": _f(LATO_SEMIBOLD, 24),
        "desc": _f(DEJAVU_SANS, 28),
        "star": _f(DEJAVU_SANS, 34),
    }


def _gradient():
    base = Image.new("RGB", (W, H), BG_TOP)
    top, bot = BG_TOP, BG_BOT
    px = base.load()
    for y in range(H):
        t = y / H
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    return base


def _circle_avatar(url, size):
    try:
        r = requests.get(url, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        av = Image.open(io.BytesIO(r.content)).convert("RGBA").resize((size, size))
    except Exception:
        av = Image.new("RGBA", (size, size), (48, 54, 61, 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    av.putalpha(mask)
    return av


def _wrap(draw, text, font, max_w, max_lines=2):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and (len(" ".join(lines).split()) < len(words)):
        while lines and draw.textlength(lines[-1] + " …", font=font) > max_w:
            lines[-1] = lines[-1].rsplit(" ", 1)[0]
        lines[-1] = lines[-1] + " …"
    return lines


def _fmt_stars(n):
    if n >= 1000:
        return f"{n/1000:.1f}k".replace(".0k", "k")
    return str(n)


def render_card(repo, rubric, summary, out_path):
    fonts = _load_fonts()
    img = _gradient()
    d = ImageDraw.Draw(img)

    name = repo.get("name", "")
    owner = (repo.get("owner") or {}).get("login", "")
    lang = repo.get("language") or ""
    accent = LANG_COLORS.get(lang, GOLD)

    # Акцентная полоса слева
    d.rectangle((0, 0, 10, H), fill=accent)

    pad = 80
    # Аватар
    asize = 132
    av = _circle_avatar((repo.get("owner") or {}).get("avatar_url", ""), asize)
    img.paste(av, (pad, pad), av)

    # Owner + name справа от аватара
    tx = pad + asize + 34
    d.text((tx, pad + 6), owner, font=fonts["owner"], fill=MUTED)
    tfont = fonts["title"] if d.textlength(name, font=fonts["title"]) <= W - tx - pad else fonts["title_sm"]
    # если совсем длинное - обрежем
    dn = name
    while d.textlength(dn, font=tfont) > W - tx - pad and len(dn) > 4:
        dn = dn[:-2]
    if dn != name:
        dn = dn.rstrip() + "…"
    d.text((tx, pad + 44), dn, font=tfont, fill=FG)

    # Бейдж рубрики (верх-право)
    btext = rubric["hashtag"]
    bw = d.textlength(btext, font=fonts["badge"]) + 40
    d.rounded_rectangle((W - pad - bw, pad, W - pad, pad + 48), radius=24,
                        fill=(accent[0], accent[1], accent[2]))
    d.text((W - pad - bw + 20, pad + 10), btext, font=fonts["badge"], fill=(13, 17, 23))

    # Описание (русская выжимка приоритетно - DejaVu надёжно рендерит кириллицу)
    desc = (summary or repo.get("description") or "").strip()
    if desc:
        lines = _wrap(d, desc, fonts["desc"], W - 2 * pad, max_lines=2)
        y = 300
        for ln in lines:
            d.text((pad, y), ln, font=fonts["desc"], fill=DESC)
            y += 44

    # Низ: звёзды, язык, форки
    by = 500
    stars = _fmt_stars(repo.get("stargazers_count") or 0)
    d.text((pad, by), "★", font=fonts["star"], fill=GOLD)
    sx = pad + 44
    d.text((sx, by), stars, font=fonts["meta"], fill=FG)
    sx += d.textlength(stars, font=fonts["meta"]) + 60

    # Языки: топ-3 реальных (repo['_langs']) с цветом linguist у каждого,
    # fallback - один primary-язык. Не вылезаем за правый край.
    langs = repo.get("_langs") or ([(lang, 1.0)] if lang else [])
    for lname, _ in langs[:3]:
        lname_w = d.textlength(lname, font=fonts["meta"])
        if sx + 34 + lname_w > W - pad:
            break
        lcol = LANG_COLORS.get(lname, GOLD)
        d.ellipse((sx, by + 12, sx + 22, by + 34), fill=lcol)
        sx += 34
        d.text((sx, by), lname, font=fonts["meta"], fill=FG)
        sx += lname_w + 40

    # Вотермарк
    wm = "GitHub Trending"
    d.text((W - pad - d.textlength(wm, font=fonts["wm"]), H - 58),
           wm, font=fonts["wm"], fill=(110, 118, 129))

    img.save(out_path, "PNG")
    return out_path


def render_hybrid(repo, rubric, summary, shot_path, out_path):
    """Гибрид как у @GitHub Radar: реальный скриншот из README сверху (letterbox
    на тёмном фоне) + фирменная плашка меты снизу. Fallback на render_card, если
    скриншот не открылся."""
    try:
        shot = Image.open(shot_path)
        if getattr(shot, "is_animated", False):
            shot.seek(0)                      # первый кадр GIF-демки
        shot = shot.convert("RGB")
    except Exception:
        return render_card(repo, rubric, summary, out_path)

    fonts = _load_fonts()
    img = _gradient()
    d = ImageDraw.Draw(img)

    name = repo.get("name", "")
    owner = (repo.get("owner") or {}).get("login", "")
    lang = repo.get("language") or ""
    accent = LANG_COLORS.get(lang, GOLD)

    # Скриншот вписываем в верхнюю зону (contain, по центру).
    SHOT_H = 400
    sw, sh = shot.size
    scale = min(W / sw, SHOT_H / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    shot = shot.resize((nw, nh))
    img.paste(shot, ((W - nw) // 2, (SHOT_H - nh) // 2))

    # Плашка меты снизу.
    py = SHOT_H
    d.rectangle((0, py, W, H), fill=BG_BOT)
    d.rectangle((0, py, W, py + 4), fill=accent)

    pad = 60
    ty = py + 26
    # Бейдж рубрики (право)
    btext = rubric["hashtag"]
    bw = d.textlength(btext, font=fonts["badge"]) + 36
    d.rounded_rectangle((W - pad - bw, ty, W - pad, ty + 44), radius=22, fill=accent)
    d.text((W - pad - bw + 18, ty + 8), btext, font=fonts["badge"], fill=(13, 17, 23))

    # Owner + name (слева)
    d.text((pad, ty), owner, font=fonts["owner"], fill=MUTED)
    tfont = fonts["title_sm"]
    dn = name
    while d.textlength(dn, font=tfont) > W - 2 * pad - bw - 20 and len(dn) > 4:
        dn = dn[:-2]
    if dn != name:
        dn = dn.rstrip() + "…"
    d.text((pad, ty + 40), dn, font=tfont, fill=FG)

    # Низ плашки: звёзды + языки
    by = H - 66
    stars = _fmt_stars(repo.get("stargazers_count") or 0)
    d.text((pad, by), "★", font=fonts["star"], fill=GOLD)
    sx = pad + 44
    d.text((sx, by), stars, font=fonts["meta"], fill=FG)
    sx += d.textlength(stars, font=fonts["meta"]) + 50
    langs = repo.get("_langs") or ([(lang, 1.0)] if lang else [])
    for lname, _ in langs[:3]:
        lname_w = d.textlength(lname, font=fonts["meta"])
        if sx + 34 + lname_w > W - pad:
            break
        d.ellipse((sx, by + 12, sx + 22, by + 34), fill=LANG_COLORS.get(lname, GOLD))
        sx += 34
        d.text((sx, by), lname, font=fonts["meta"], fill=FG)
        sx += lname_w + 36

    img.save(out_path, "PNG")
    return out_path
