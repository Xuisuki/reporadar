# -*- coding: utf-8 -*-
"""Чтение README репозитория - источник для подробного описания (как @GitHub Radar)."""
import re
import requests

# Картинки из README: markdown ![alt](url) и html <img src=url>.
_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_IMG_HTML = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
# Бейджи/шилды/svg - не скриншоты продукта.
_BADGE = re.compile(
    r"(shields\.io|/badge|badgen|travis|circleci|codecov|coveralls|license|"
    r"npmjs|pypi|goreportcard|opencollective|visitor|forthebadge|sonarcloud|"
    r"app\.codacy|snyk|deepsource|\.svg([?#].*)?$)", re.I)
_LOWPRI = re.compile(r"(logo|icon|avatar|favicon)", re.I)
_HIPRI = re.compile(r"(screenshot|screen-shot|screen_shot|demo|preview|hero|"
                    r"banner|example|showcase|dashboard|\.gif)", re.I)


def _resolve_img(url, full_name, branch):
    """Абсолютизировать URL картинки: относительные пути и github blob -> raw."""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http"):
        m = re.match(r"https?://github\.com/([^/]+/[^/]+)/(?:blob|raw)/(.+?)(?:\?.*)?$", url)
        if m:
            return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}"
        return url
    if url.startswith(("#", "data:", "mailto:")):
        return ""
    path = url.lstrip("./").lstrip("/").split("?")[0].split("#")[0]
    return f"https://raw.githubusercontent.com/{full_name}/{branch}/{path}"


def find_readme_image(full_name, branch="main", pat="", readme=None):
    """URL первой КОНТЕНТНОЙ картинки README (скриншот/демо), не бейдж/логотип.
    Приоритет: screenshot/demo/preview > обычная > logo/icon. None если нет."""
    if readme is None:
        readme = fetch_readme(full_name, pat)
    if not readme:
        return None
    urls = _IMG_MD.findall(readme) + _IMG_HTML.findall(readme)
    cands = []
    for i, u in enumerate(urls):
        if not u or _BADGE.search(u):
            continue
        rv = _resolve_img(u, full_name, branch or "main")
        if not rv.startswith("http"):
            continue
        pri = 2 if _HIPRI.search(u) else (0 if _LOWPRI.search(u) else 1)
        cands.append((pri, i, rv))          # i сохраняет порядок появления
    if not cands:
        return None
    cands.sort(key=lambda t: (-t[0], t[1]))  # выше приоритет, затем раньше в README
    return cands[0][2]


def fetch_readme(full_name, pat=""):
    headers = {"Accept": "application/vnd.github.raw+json",
               "User-Agent": "gh-trend-bot"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}/readme",
                         headers=headers, timeout=20)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""


def fetch_languages(full_name, pat="", top=3, min_share=0.08):
    """Реальные языки репо через /languages (endpoint отдаёт байты по каждому).
    Возвращает список [(язык, доля 0..1)] топ-N с долей >= min_share.
    В отличие от repo['language'] (только главный) - показывает весь стек."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gh-trend-bot"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    try:
        r = requests.get(f"https://api.github.com/repos/{full_name}/languages",
                         headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json() or {}
            total = sum(data.values()) or 1
            items = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
            out = [(k, v / total) for k, v in items if v / total >= min_share][:top]
            if not out and items:              # всё ниже порога - хотя бы главный
                k, v = items[0]
                out = [(k, v / total)]
            return out
    except Exception:
        pass
    return []
