#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gh-trend-bot - находит трендовые репозитории GitHub и постит их карточками
в Telegram-канал (Bot API 10.1 Rich Messages, sendRichMessage).

Режим: полный автомат. Один запуск = один аккуратный пост.
Планировщик - systemd timer (gh-trend-bot.timer), по 1 посту каждые N часов.

Источник трендов - официальный GitHub Search API (created:>дата sort:stars),
это надёжнее хрупкого скрейпа github.com/trending. Рубрики ротируются по кругу.

Команды:
  python3 bot.py run          # выбрать следующую рубрику, найти свежий репо, запостить
  python3 bot.py preview      # собрать карточку и вывести HTML (без отправки)
  python3 bot.py fetch        # показать кандидатов текущей рубрики (без отправки)
  python3 bot.py chatid       # определить chat_id канала (добавьте бота админом)
  python3 bot.py whoami       # getMe

Конфиг: .env рядом со скриптом (см. .env.example).
LLM: любой OpenAI-совместимый провайдер (по умолчанию локальная Ollama).
"""
import os
import re
import sys
import time
import html as _html
import json
import sqlite3
import datetime
import argparse
import requests
import card_image
import enrich

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "store.db")
ENV_PATH = os.path.join(BASE, ".env")

# Пути к секретам по умолчанию (переопределяются в .env). Опциональны:
# без GitHub-токена работает на пониженном лимите Search API.
DEFAULT_GH_TOKEN_FILE = "/root/.config/github/pat"


# ---------------------------------------------------------------- конфиг
def load_env():
    """Простой парсер .env: KEY=VALUE, реальные os.environ имеют приоритет."""
    cfg = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        cfg[k] = v
    return cfg


def read_secret(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


CFG = load_env()
BOT_TOKEN = CFG.get("GH_TREND_BOT_TOKEN", "")
CHANNEL_ID = CFG.get("GH_TREND_CHANNEL_ID", "")
USE_AI = CFG.get("USE_AI", "1") == "1"

# --- LLM: любой OpenAI-совместимый провайдер --------------------------------
# По умолчанию - локальная Ollama (бесплатно, без ключа, любая ОС).
# Groq/OpenAI/LM Studio/together и пр. - просто смените LLM_BASE_URL/LLM_MODEL и
# при необходимости укажите ключ (LLM_API_KEY или LLM_API_KEY_FILE со списком).
LLM_BASE_URL = CFG.get("LLM_BASE_URL", "http://localhost:11434/v1")
LLM_MODEL = CFG.get("LLM_MODEL", "llama3.1")

# --- режим "иголка в сене" (эталон @GitHubRadar) ---------------------------
# Ищем не самые звёздные, а НЕДООЦЕНЁННЫЕ: узкое окно звёзд + скор по скорости
# роста (ракеты) или живости (вечнозелёные) + LLM-судья от мусора.
STARS_MIN = int(CFG.get("NEEDLE_STARS_MIN", CFG.get("MIN_STARS", "150")))
STARS_MAX = int(CFG.get("NEEDLE_STARS_MAX", "2500"))
ROCKET_MAX_AGE = int(CFG.get("NEEDLE_ROCKET_AGE", "60"))       # дней с created для "ракет"
ROCKET_STARS_MAX = int(CFG.get("NEEDLE_ROCKET_STARS_MAX", "1500"))
EVERGREEN_PUSH_DAYS = int(CFG.get("NEEDLE_EVERGREEN_PUSH", "90"))  # свежесть коммитов
JUDGE_TOPK = int(CFG.get("NEEDLE_JUDGE_TOPK", "6"))           # сколько верхних судить
JUDGE_MIN = int(CFG.get("NEEDLE_JUDGE_MIN", "7"))            # порог балла судьи 1-10
USE_JUDGE = CFG.get("NEEDLE_USE_JUDGE", "1") == "1"
USE_SCREENSHOT = CFG.get("NEEDLE_SCREENSHOT", "1") == "1"    # скрин из README на карточку

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _load_gh_token():
    """GitHub-токен: из .env (GITHUB_TOKEN), либо из файла, либо пусто (без auth)."""
    direct = CFG.get("GITHUB_TOKEN", "").strip()
    if direct:
        return direct
    path = CFG.get("GITHUB_TOKEN_FILE", DEFAULT_GH_TOKEN_FILE)
    return read_secret(path)


def _load_llm_keys():
    """Ключ(и) LLM: прямой LLM_API_KEY, либо файл LLM_API_KEY_FILE (несколько строк
    = ротация), либо пусто (Ollama/локальные ключа не требуют)."""
    direct = CFG.get("LLM_API_KEY", "").strip()
    if direct:
        return [direct]
    path = CFG.get("LLM_API_KEY_FILE", "").strip()
    if path:
        keys = [k.strip() for k in read_secret(path).splitlines() if k.strip()]
        if keys:
            return keys
    return [""]


GH_PAT = _load_gh_token()
LLM_KEYS = _load_llm_keys()


def esc(s):
    return _html.escape(str(s or ""), quote=True)


def die(msg, code=2):
    sys.stderr.write(f"[ERROR] {msg}\n")
    sys.exit(code)


# ---------------------------------------------------------------- рубрики
def days_ago(n):
    d = datetime.date.today() - datetime.timedelta(days=n)
    return d.isoformat()


def days_since(iso):
    """Сколько дней прошло с ISO-даты (created_at/pushed_at). None если не распарсить."""
    if not iso:
        return None
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return max((datetime.date.today() - d).days, 0)
    except Exception:
        return None


# Рубрика = тема/язык (селектор поиска) + оформление. Ротация по кругу (курсор в БД).
# Окно звёзд и даты НЕ зашиты в рубрику - их задаёт РЕЖИМ (rocket/evergreen) в
# build_query, чтобы ловить недооценённые репо, а не гигантов.
#   sel: "topic:X" | "language:Y" | "" (общий трендовый)
RUBRICS = [
    # --- свежее трендовое ---
    {"key": "overall", "label": "Трендовое сейчас", "emoji": "\U0001F525", "hashtag": "#trending", "sel": ""},
    {"key": "ai", "label": "AI / ML", "emoji": "\U0001F916", "hashtag": "#ai", "sel": "topic:ai"},
    {"key": "llm", "label": "LLM / агенты", "emoji": "\U0001F9E0", "hashtag": "#llm", "sel": "topic:llm"},
    # --- инфра / devops / self-host ---
    {"key": "devtools", "label": "Dev-инструменты", "emoji": "\U0001F6E0️", "hashtag": "#tools", "sel": "topic:developer-tools"},
    {"key": "selfhosted", "label": "Self-hosted", "emoji": "\U0001F3E0", "hashtag": "#selfhosted", "sel": "topic:self-hosted"},
    {"key": "devops", "label": "DevOps / инфра", "emoji": "⚙️", "hashtag": "#devops", "sel": "topic:devops"},
    {"key": "docker", "label": "Docker / контейнеры", "emoji": "\U0001F433", "hashtag": "#docker", "sel": "topic:docker"},
    {"key": "kubernetes", "label": "Kubernetes", "emoji": "☸️", "hashtag": "#k8s", "sel": "topic:kubernetes"},
    # --- security / privacy ---
    {"key": "security", "label": "Безопасность", "emoji": "\U0001F510", "hashtag": "#security", "sel": "topic:security"},
    {"key": "privacy", "label": "Приватность", "emoji": "\U0001F6E1️", "hashtag": "#privacy", "sel": "topic:privacy"},
    # --- cli / git / продуктивность ---
    {"key": "cli", "label": "CLI / терминал", "emoji": "⌨️", "hashtag": "#cli", "sel": "topic:cli"},
    {"key": "terminal", "label": "Терминал", "emoji": "\U0001F41A", "hashtag": "#terminal", "sel": "topic:terminal"},
    {"key": "git", "label": "Git", "emoji": "\U0001F500", "hashtag": "#git", "sel": "topic:git"},
    {"key": "productivity", "label": "Продуктивность", "emoji": "\U0001F4C8", "hashtag": "#productivity", "sel": "topic:productivity"},
    {"key": "automation", "label": "Автоматизация", "emoji": "\U0001F501", "hashtag": "#automation", "sel": "topic:automation"},
    # --- мобилка / десктоп ---
    {"key": "android", "label": "Android", "emoji": "\U0001F4F1", "hashtag": "#android", "sel": "topic:android"},
    {"key": "ios", "label": "iOS", "emoji": "\U0001F34F", "hashtag": "#ios", "sel": "topic:ios"},
    {"key": "desktop", "label": "Десктоп-приложения", "emoji": "\U0001F4BB", "hashtag": "#desktop", "sel": "topic:electron"},
    # --- web / ui / графика ---
    {"key": "react", "label": "React", "emoji": "⚛️", "hashtag": "#react", "sel": "topic:react"},
    {"key": "web", "label": "Web / TypeScript", "emoji": "\U0001F310", "hashtag": "#web", "sel": "language:TypeScript"},
    {"key": "graphics", "label": "Графика / UI", "emoji": "\U0001F3A8", "hashtag": "#graphics", "sel": "topic:graphics"},
    # --- по языкам ---
    {"key": "python", "label": "Python", "emoji": "\U0001F40D", "hashtag": "#python", "sel": "language:Python"},
    {"key": "rust", "label": "Rust", "emoji": "\U0001F980", "hashtag": "#rust", "sel": "language:Rust"},
    {"key": "go", "label": "Go", "emoji": "\U0001F439", "hashtag": "#go", "sel": "language:Go"},
    {"key": "cpp", "label": "C / C++", "emoji": "\U0001F527", "hashtag": "#cpp", "sel": "language:C++"},
]


def build_query(rubric, mode):
    """Поисковый запрос под режим. rocket - молодые быстрорастущие; evergreen -
    старые, но живые и малозвёздные (недооценённые вечнозелёные утилиты)."""
    sel = rubric.get("sel", "")
    if mode == "rocket":
        parts = [sel, f"created:>{days_ago(ROCKET_MAX_AGE)}",
                 f"stars:{STARS_MIN}..{ROCKET_STARS_MAX}"]
    else:  # evergreen
        parts = [sel, f"pushed:>{days_ago(EVERGREEN_PUSH_DAYS)}",
                 f"stars:{STARS_MIN}..{STARS_MAX}"]
    return " ".join(p for p in parts if p).strip()


def mode_for(idx):
    """Чередование по проходам: чётный курсор - ракеты, нечётный - вечнозелёные."""
    return "rocket" if idx % 2 == 0 else "evergreen"

# Узкий blocklist для безопасности "полного автомата": явный скам/адалт.
# Порог звёзд и так отсекает основной мусор; тут только грубые слова.
BLOCKLIST = {
    "casino", "gambling", "porn", "porno", "nsfw", "sex", "escort",
    "warez", "crackz", "pirate", "1xbet", "onlyfans",
}


# ---------------------------------------------------------------- БД
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS posted ("
        "repo_id INTEGER PRIMARY KEY, full_name TEXT, rubric TEXT, posted_at INTEGER)"
    )
    # topics постов - для антидубля по смыслу (мягкий ALTER к существующей БД).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(posted)").fetchall()]
    if "topics" not in cols:
        conn.execute("ALTER TABLE posted ADD COLUMN topics TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)"
    )
    # История звёзд: снапшот пула каждый прогон -> измеряем реальный прирост.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS star_seen ("
        "repo_id INTEGER PRIMARY KEY, full_name TEXT, "
        "first_stars INTEGER, first_seen INTEGER, stars INTEGER, last_seen INTEGER)"
    )
    return conn


def measured_velocity(conn, repo_id, cur_stars):
    """Реальный прирост звёзд/день по своей истории. None если репо ещё не видели
    или интервал слишком мал (шум). Точнее аппроксимации stars/возраст."""
    row = conn.execute(
        "SELECT first_stars, first_seen FROM star_seen WHERE repo_id=?", (repo_id,)
    ).fetchone()
    if not row:
        return None
    first_stars, first_seen = row
    dt_days = (time.time() - first_seen) / 86400.0
    if dt_days < 0.7:                      # меньше ~17ч - слишком свежо
        return None
    delta = (cur_stars or 0) - (first_stars or 0)
    if delta <= 0:
        return None
    return delta / dt_days


def record_snapshots(conn, repos):
    """Зафиксировать текущие звёзды пула. first_* пишется один раз (при первой
    встрече), stars/last_seen обновляются каждый прогон."""
    now = int(time.time())
    for r in repos:
        st = r.get("stargazers_count") or 0
        conn.execute(
            "INSERT INTO star_seen(repo_id,full_name,first_stars,first_seen,stars,last_seen) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(repo_id) DO UPDATE SET "
            "stars=excluded.stars, last_seen=excluded.last_seen",
            (r["id"], r.get("full_name", ""), st, now, st, now),
        )
    conn.commit()


def get_cursor(conn):
    row = conn.execute("SELECT v FROM meta WHERE k='rubric_cursor'").fetchone()
    return int(row[0]) if row else 0


def set_cursor(conn, val):
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('rubric_cursor',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (str(val),),
    )
    conn.commit()


def already_posted(conn, repo_id):
    return conn.execute(
        "SELECT 1 FROM posted WHERE repo_id=?", (repo_id,)
    ).fetchone() is not None


def mark_posted(conn, repo, rubric_key):
    topics = json.dumps((repo.get("topics") or [])[:10])
    conn.execute(
        "INSERT OR IGNORE INTO posted(repo_id,full_name,rubric,posted_at,topics) "
        "VALUES(?,?,?,?,?)",
        (repo["id"], repo["full_name"], rubric_key, int(time.time()), topics),
    )
    conn.commit()


def recent_topics(conn, n=6):
    """Множество тем последних n постов - для отсечения тематических дублей."""
    rows = conn.execute(
        "SELECT topics FROM posted WHERE topics IS NOT NULL "
        "ORDER BY posted_at DESC LIMIT ?", (n,)
    ).fetchall()
    seen = set()
    for (t,) in rows:
        try:
            seen.update(json.loads(t or "[]"))
        except Exception:
            pass
    return seen


# ---------------------------------------------------------------- GitHub
def gh_search(query, per_page=30, sort="stars"):
    headers = {"Accept": "application/vnd.github+json"}
    if GH_PAT:
        headers["Authorization"] = f"Bearer {GH_PAT}"
    params = {"q": query, "sort": sort, "order": "desc", "per_page": per_page}
    r = requests.get(
        "https://api.github.com/search/repositories",
        headers=headers, params=params, timeout=30,
    )
    if r.status_code == 403 and "rate limit" in r.text.lower():
        die("GitHub rate limit. Добавьте PAT в /root/.config/github/pat", 4)
    r.raise_for_status()
    return r.json().get("items", [])


def is_clean(repo):
    if repo.get("fork") or repo.get("archived"):
        return False
    if not (repo.get("description") or "").strip():
        return False
    if (repo.get("stargazers_count") or 0) < STARS_MIN:
        return False
    blob = f"{repo.get('full_name','')} {repo.get('description','')}".lower()
    words = set(blob.replace("/", " ").replace("-", " ").replace("_", " ").split())
    if words & BLOCKLIST:
        return False
    return True


def score(repo, mode, mvel=None):
    """Скор "иголочности". Если есть measured velocity (реальный прирост звёзд/день
    по своей истории) - он главный сигнал для обоих режимов. Иначе fallback:
    rocket - аппроксимация stars/возраст; evergreen - живость + тяга форков."""
    stars = repo.get("stargazers_count") or 0
    age = days_since(repo.get("created_at")) or 7
    push = days_since(repo.get("pushed_at"))
    fresh = 1.0 / (1.0 + (push if push is not None else 999) / 30.0)  # 1=только пушили
    if mvel is not None:
        # +1000: измеренный прирост всегда выше любой аппроксимации в ранжировании
        return round(1000.0 + mvel * (0.6 + 0.4 * fresh), 2)
    if mode == "rocket":
        vel = stars / max(age, 7)                      # звёзд/день
        return round(vel * (0.5 + 0.5 * fresh), 2)
    forks = repo.get("forks_count") or 0
    pull = min(forks / stars, 1.0) if stars else 0.0   # форки/звёзды = вовлечённость
    return round(100.0 * fresh + 20.0 * pull + stars ** 0.25, 2)


def parse_judge(raw):
    """Достаёт {score,reason} из ответа LLM (иногда с мусором вокруг JSON)."""
    if not raw:
        return {"score": 5, "reason": ""}
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            j = json.loads(m.group(0))
            sc = int(float(j.get("score", 5)))
            return {"score": max(1, min(10, sc)), "reason": str(j.get("reason", ""))[:200]}
    except Exception:
        pass
    m = re.search(r"\b(10|[1-9])\b", raw)
    return {"score": int(m.group(1)) if m else 5, "reason": raw[:120]}


def judge(repo, readme):
    """LLM-судья: недооценённая полезная находка или проходняк/учебка/пустышка.
    Возвращает {score 1-10, reason}. Если судья выключен/недоступен - пропускает."""
    if not USE_JUDGE or not USE_AI:
        return {"score": 10, "reason": "судья выключен"}
    stars = repo.get("stargazers_count") or 0
    topics = ", ".join((repo.get("topics") or [])[:8])
    system = (
        "Ты придирчивый технический редактор Telegram-канала про находки на GitHub. "
        "Оцени по шкале 1-10, стоит ли репозиторий внимания ОПЫТНОГО разработчика "
        "как НЕДООЦЕНЁННАЯ ПОЛЕЗНАЯ находка - рабочий инструмент, библиотека или "
        "приложение с реальной ценностью. "
        "Ставь 8-10 только зрелым, самодостаточным, практичным проектам с внятным "
        "README и реальной функциональностью. "
        "Ставь 1-5 (отсев) за: учебный/туториал/пример, шутку или мем, пустой или "
        "заготовку (boilerplate/template), сугубо личный проект или дотфайлы, набор "
        "конфигов или ссылок, ещё один клон-дубль известного инструмента без своей "
        "идеи, README из одних общих слов без конкретики, признаки накрутки звёзд "
        "(много звёзд при пустом/шаблонном README). "
        "Малое число звёзд - это ПЛЮС, если проект реально полезный (ищем скрытые "
        "жемчужины, а не популярное). Будь строг: при сомнении ставь ниже. "
        "Верни СТРОГО JSON без пояснений вокруг: "
        '{"score": <целое 1-10>, "reason": "<до 15 слов по-русски>"}.')
    user = (f"Репозиторий: {repo.get('full_name')}\n"
            f"Описание: {(repo.get('description') or '')[:200]}\n"
            f"Звёзды: {stars}, язык: {repo.get('language')}, темы: {topics}\n\n"
            f"README (фрагмент):\n{(readme or '')[:3000]}")
    return parse_judge(llm_chat(system, user, max_tokens=160, temperature=0.2))


def pick_needle(conn, rubric, mode):
    """Набрать пул, отранжировать по скору иголочности, пропустить верхних через
    судью и вернуть первого одобренного (README кладётся в repo['_readme'])."""
    sort = "stars" if mode == "rocket" else "updated"
    repos = gh_search(build_query(rubric, mode), per_page=100, sort=sort)
    cands = [r for r in repos if is_clean(r) and not already_posted(conn, r["id"])]
    # measured velocity считаем ДО записи снапшота (по прошлой истории), затем
    # фиксируем текущие звёзды всего пула (не только чистых) для будущих замеров.
    for r in cands:
        r["_mvel"] = measured_velocity(conn, r["id"], r.get("stargazers_count") or 0)
    record_snapshots(conn, repos)
    if not cands:
        return None
    cands.sort(key=lambda r: score(r, mode, r.get("_mvel")), reverse=True)
    recent = recent_topics(conn, 6)
    for r in cands[:JUDGE_TOPK]:
        # антидубль: пропускаем репо, чья тема сильно совпадает с недавними постами
        if len(set(r.get("topics") or []) & recent) >= 2:
            continue
        readme = enrich.fetch_readme(r["full_name"], GH_PAT)
        v = judge(r, readme)
        if v["score"] >= JUDGE_MIN:
            r["_readme"] = readme
            r["_judge"] = v
            r["_mode"] = mode
            r["_score"] = score(r, mode, r.get("_mvel"))
            r["_langs"] = enrich.fetch_languages(r["full_name"], GH_PAT)
            return r
    return None


# ---------------------------------------------------------------- LLM (AI)
def llm_chat(system, user, max_tokens=400, temperature=0.3):
    """Запрос к любому OpenAI-совместимому LLM (/chat/completions) с ротацией
    ключей. Возвращает текст или '' при неудаче/выключенном AI."""
    if not USE_AI:
        return ""
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": LLM_MODEL, "temperature": temperature, "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    for key in LLM_KEYS:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=90)
            if r.status_code in (401, 403, 429):
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            continue
    return ""


def describe(repo, readme):
    """Подробное описание на русском в 2-3 абзаца по README (стиль @GitHub Radar).
    Fallback - краткое описание из GitHub."""
    original = (repo.get("description") or "").strip()
    if not USE_AI:
        return original
    topics = ", ".join((repo.get("topics") or [])[:8])
    ctx = (f"Репозиторий: {repo.get('full_name')}\n"
           f"Краткое описание GitHub: {original}\n"
           f"Язык: {repo.get('language')}\nТемы: {topics}\n\n"
           f"README (фрагмент):\n{(readme or '')[:4500]}")
    system = (
        "Ты технический редактор Telegram-канала о GitHub-проектах для опытных "
        "разработчиков. По данным и README напиши плотное описание на русском в 2-3 "
        "коротких абзаца через пустую строку, суммарно 350-550 символов. "
        "Структура: абзац 1 - что инструмент делает КОНКРЕТНО (какую задачу решает, "
        "как технически); абзац 2 - чем он выделяется или что умеет необычного "
        "(конкретные факты/технологии из README); абзац 3 (если есть чем) - для кого "
        "и когда пригодится. "
        "ЗАПРЕЩЕНО: вода и общие фразы ('позволяет легко', 'широкий спектр "
        "возможностей', 'мощный инструмент', 'необходимо выполнить определённые "
        "шаги', 'для различных задач'); инструкции по установке и команды; "
        "пересказ пунктов лицензии; маркетинг; выдумки за пределами README. "
        "Каждое предложение должно нести конкретный факт - если факта нет, не пиши "
        "предложение. Без эмодзи, без markdown, без заголовков и списков. "
        "Пиши строго и только на русском языке, без иностранных слов и вставок "
        "(технические названия и имена продуктов оставляй как есть).")
    return llm_chat(system, ctx, max_tokens=430, temperature=0.3) or original


# ---------------------------------------------------------------- вёрстка
def fmt_stars(n):
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


def build_caption(repo, rubric, summary):
    """HTML-подпись под картинку (parse_mode=HTML, лимит Telegram 1024).
    Подробное описание в blockquote, снизу мета и ссылка."""
    name = esc(repo.get("name", ""))
    owner = esc((repo.get("owner") or {}).get("login", ""))
    stars = fmt_stars(repo.get("stargazers_count") or 0)
    url = esc(repo.get("html_url", ""))
    topics = repo.get("topics", []) or []
    tags = " ".join("#" + t.replace("-", "_") for t in topics[:4])

    # Языки: топ-3 реальных (repo['_langs']), fallback - primary repo['language'].
    langs = repo.get("_langs") or []
    if langs:
        lang_str = "  ".join(f"<code>{esc(n)}</code>" for n, _ in langs[:3])
    else:
        lang_str = f"<code>{esc(repo.get('language') or '—')}</code>"

    head = f"{rubric['emoji']} <b>{owner}/{name}</b>"
    meta = f"★ <b>{stars}</b>   {lang_str}"
    if tags:
        meta += f"   {esc(tags)}"
    link = f'<a href="{url}">Открыть на GitHub</a>'

    # Бюджет под описание, чтобы мета и ссылка всегда влезли в 1024.
    budget = 1024 - len(head) - len(meta) - len(link) - 30
    desc = esc((summary or "").strip())
    if len(desc) > budget:
        desc = desc[:budget].rsplit(" ", 1)[0].rstrip(",.;:") + "…"

    parts = [head]
    if desc:
        parts.append(f"<blockquote>{desc}</blockquote>")
    parts.append(meta)
    parts.append(link)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- Telegram
def tg_call(method, payload):
    r = requests.post(f"{API}/{method}", json=payload, timeout=60)
    data = r.json()
    if not data.get("ok"):
        die(f"{method}: {data.get('error_code')} {data.get('description')}", 5)
    return data["result"]


def post_photo(img_path, caption, url):
    data = {
        "chat_id": CHANNEL_ID,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [[
            {"text": "⭐ Открыть на GitHub", "url": url}]]}),
    }
    with open(img_path, "rb") as fh:
        r = requests.post(f"{API}/sendPhoto", data=data,
                          files={"photo": fh}, timeout=120)
    d = r.json()
    if not d.get("ok"):
        die(f"sendPhoto: {d.get('error_code')} {d.get('description')}", 5)
    return d["result"]


# ---------------------------------------------------------------- карточка
IMG_MAX_BYTES = 15_000_000                     # не тянуть гигантские демки-гифки


def _is_image_bytes(b):
    """Проверка по сигнатуре: PNG/JPEG/GIF/WebP (а не HTML-404 или SVG)."""
    return (b[:8].startswith(b"\x89PNG") or b[:3] == b"\xff\xd8\xff"
            or b[:6] in (b"GIF87a", b"GIF89a")
            or (b[:4] == b"RIFF" and b[8:12] == b"WEBP"))


def download_image(url, dest):
    """Скачать картинку README. GitHub raw часто отдаёт octet-stream, поэтому тип
    определяем по расширению+сигнатуре, а не по Content-Type. None если не
    картинка / SVG / вне лимита размера."""
    url_l = url.lower().split("?")[0]
    if url_l.endswith(".svg"):
        return None
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"},
                         stream=True)
        if r.status_code != 200:
            return None
        clen = int(r.headers.get("Content-Length") or 0)
        if clen and clen > IMG_MAX_BYTES:
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > IMG_MAX_BYTES:      # оборвать раздутый файл
                return None
        if len(data) < 2000 or not _is_image_bytes(data):
            return None
        with open(dest, "wb") as f:
            f.write(data)
        return dest
    except Exception:
        return None


def build_card(repo, rubric, card_summary, img_path):
    """Собрать карточку: гибрид со скриншотом из README, иначе своя карточка.
    Возвращает 'hybrid' или 'card' (для лога)."""
    if USE_SCREENSHOT:
        url = enrich.find_readme_image(
            repo["full_name"], repo.get("default_branch") or "main",
            GH_PAT, repo.get("_readme"))
        if url:
            shot = download_image(url, os.path.join(BASE, "shot_raw"))
            if shot:
                card_image.render_hybrid(repo, rubric, card_summary, shot, img_path)
                return "hybrid"
    card_image.render_card(repo, rubric, card_summary, img_path)
    return "card"


# ---------------------------------------------------------------- команды
def need_config():
    if not BOT_TOKEN:
        die("GH_TREND_BOT_TOKEN не задан в .env", 3)
    if not CHANNEL_ID:
        die("GH_TREND_CHANNEL_ID не задан в .env (узнать: python3 bot.py chatid)", 3)


def cmd_run(_):
    need_config()
    conn = db()
    start = get_cursor(conn)
    # Пробуем текущую рубрику; если пусто - идём по кругу (не оставлять запуск без поста).
    for step in range(len(RUBRICS)):
        idx = start + step
        rubric = RUBRICS[idx % len(RUBRICS)]
        mode = mode_for(idx)
        repo = pick_needle(conn, rubric, mode)
        if not repo:
            continue
        readme = repo.get("_readme") or enrich.fetch_readme(repo["full_name"], GH_PAT)
        summary = describe(repo, readme)
        caption = build_caption(repo, rubric, summary)
        card_summary = summary.split("\n\n")[0] if summary else ""
        img_path = os.path.join(BASE, "card.png")
        kind = build_card(repo, rubric, card_summary, img_path)
        res = post_photo(img_path, caption, repo["html_url"])
        mark_posted(conn, repo, rubric["key"])
        set_cursor(conn, idx + 1)
        jv = repo.get("_judge", {})
        print(f"SENT [{rubric['key']}/{mode}] {repo['full_name']} "
              f"({repo['stargazers_count']}*, score {repo.get('_score')}, "
              f"judge {jv.get('score')}, {kind}) message_id={res.get('message_id')}")
        return
    set_cursor(conn, start + 1)
    print("Иголок не нашлось ни в одной рубрике за проход.")


def cmd_fetch(args):
    conn = db()
    idx = get_cursor(conn) + getattr(args, "offset", 0)
    rubric = RUBRICS[idx % len(RUBRICS)]
    mode = mode_for(idx)
    q = build_query(rubric, mode)
    sort = "stars" if mode == "rocket" else "updated"
    print(f"Рубрика: {rubric['key']} | режим: {mode} | запрос: {q}\n")
    repos = gh_search(q, per_page=100, sort=sort)
    cands = [r for r in repos if is_clean(r) and not already_posted(conn, r["id"])]
    cands.sort(key=lambda r: score(r, mode), reverse=True)
    if not cands:
        print("  (кандидатов нет)")
        return
    ranked = []
    for r in cands:
        mv = measured_velocity(conn, r["id"], r.get("stargazers_count") or 0)
        ranked.append((score(r, mode, mv), mv, r))
    ranked.sort(key=lambda t: t[0], reverse=True)
    for sc, mv, r in ranked[:15]:
        age = days_since(r.get("created_at"))
        mvs = f"+{mv:.0f}/д" if mv is not None else "—"
        print(f"  score {sc:>8}  {r['stargazers_count']:>6}*  age {str(age)+'д':>6}  "
              f"mv {mvs:>7}  {r['full_name']:<40} {r.get('language') or '-'}")


def cmd_preview(args):
    conn = db()
    idx = get_cursor(conn) + getattr(args, "offset", 0)
    rubric = RUBRICS[idx % len(RUBRICS)]
    mode = mode_for(idx)
    repo = pick_needle(conn, rubric, mode)
    if not repo:
        print(f"Нет иголок в рубрике {rubric['key']}/{mode}.")
        return
    readme = repo.get("_readme") or enrich.fetch_readme(repo["full_name"], GH_PAT)
    summary = describe(repo, readme)
    out = os.path.join(BASE, "preview.png")
    card_summary = summary.split("\n\n")[0] if summary else ""
    kind = build_card(repo, rubric, card_summary, out)
    print(f"# {rubric['label']} / {mode} -> {repo['full_name']} "
          f"({repo['stargazers_count']}*, score {repo.get('_score')}, "
          f"judge {repo.get('_judge')}, card={kind})")
    print(f"IMAGE: {out}\n")
    print(build_caption(repo, rubric, summary))


def cmd_chatid(args):
    if not BOT_TOKEN:
        die("GH_TREND_BOT_TOKEN не задан в .env", 3)
    offset = None
    found = {}
    deadline = time.time() + args.wait
    sys.stderr.write(
        f"Слушаю getUpdates до {args.wait}s. Добавьте бота АДМИНОМ в канал и "
        "запостите там что-нибудь (или перешлите пост в @userinfobot)...\n")
    while time.time() < deadline:
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(f"{API}/getUpdates", params=params,
                         data={"allowed_updates": json.dumps(["channel_post", "message", "my_chat_member"])},
                         timeout=30)
        for upd in r.json().get("result", []):
            offset = upd["update_id"] + 1
            for key in ("channel_post", "message", "my_chat_member"):
                if key in upd and "chat" in upd[key]:
                    c = upd[key]["chat"]
                    found[c["id"]] = c
        if found:
            break
    if not found:
        print("Каналов не найдено. Бот добавлен админом? Был ли пост после добавления?")
        return
    for cid, c in found.items():
        print(f"chat_id={cid}  type={c.get('type')}  title={c.get('title') or c.get('username')!r}")


def cmd_whoami(_):
    if not BOT_TOKEN:
        die("GH_TREND_BOT_TOKEN не задан в .env", 3)
    me = tg_call("getMe", {})
    print(f"@{me['username']}  id={me['id']}  name={me.get('first_name')}")


def main():
    p = argparse.ArgumentParser(prog="bot.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    pf = sub.add_parser("fetch")
    pf.add_argument("--offset", type=int, default=0, help="сдвиг рубрики от курсора (для теста)")
    pp = sub.add_parser("preview")
    pp.add_argument("--offset", type=int, default=0, help="сдвиг рубрики от курсора (для теста)")
    sub.add_parser("whoami")
    pc = sub.add_parser("chatid")
    pc.add_argument("--wait", type=int, default=60)
    args = p.parse_args()
    {
        "run": cmd_run, "fetch": cmd_fetch, "preview": cmd_preview,
        "chatid": cmd_chatid, "whoami": cmd_whoami,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
