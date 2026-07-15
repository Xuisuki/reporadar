#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
first_start.py — мастер первичной настройки RepoRadar.

Кастомный терминал-установщик (Rich): пошагово собирает .env — Telegram-бот и
канал, LLM-провайдер, GitHub-токен, параметры отбора «иглы в стоге». Показывает,
где взять каждое значение, проверяет ввод, делает бэкап старого .env и в конце
предлагает проверить бота.

Запуск: python first_start.py   (или ./install.sh, который поставит зависимости)

Проект: ProdX (https://prodx.pro)
Разработчики: @Xuisuki + @mawlikow — github.com/Xuisuki, github.com/mawlikow
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys

import _wizard_ui as ui

ENV_PATH = ".env"
BACKUP_DIR = "backup_env"
TOTAL = 4

# Полный набор ключей RepoRadar с дефолтами (то, что не спрашиваем — пишется как есть).
DEFAULTS = {
    "GH_TREND_BOT_TOKEN": "", "GH_TREND_CHANNEL_ID": "",
    "USE_AI": "1", "LLM_BASE_URL": "http://localhost:11434/v1",
    "LLM_MODEL": "llama3.1", "LLM_API_KEY": "",
    "GITHUB_TOKEN": "",
    "NEEDLE_STARS_MIN": "150", "NEEDLE_STARS_MAX": "2500",
    "NEEDLE_ROCKET_AGE": "60", "NEEDLE_ROCKET_STARS_MAX": "1500",
    "NEEDLE_EVERGREEN_PUSH": "90", "NEEDLE_JUDGE_TOPK": "6",
    "NEEDLE_JUDGE_MIN": "7", "NEEDLE_USE_JUDGE": "1", "NEEDLE_SCREENSHOT": "1",
    "TZ": "Europe/Moscow",
}

# Пресеты LLM-провайдеров: base_url, дефолтная модель, нужен ли ключ, где взять ключ.
LLM_PRESETS = {
    "ollama": ("http://localhost:11434/v1", "llama3.1", False,
               "Локальная Ollama — бесплатно, без ключа. Поставьте с https://ollama.com,"
               " затем `ollama pull llama3.1`."),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", True,
             "Бесплатный ключ: console.groq.com/keys"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", True,
               "Ключ: platform.openai.com/api-keys"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.1-8b-instruct", True,
                   "Ключ: openrouter.ai/keys (есть бесплатные модели)"),
}


# ---------------------------------------------------------------- валидаторы
def v_token(s: str):
    if not re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", s):
        return "Похоже, это не токен бота. Формат: 123456789:AA...(35+ символов)."
    return None


def v_channel(s: str):
    if s.startswith("@") and len(s) > 1:
        return None
    if s.lstrip("-").isdigit():
        return None
    return "Укажите @username канала или числовой id вида -1001234567890."


def v_int(s: str):
    return None if s.lstrip("-").isdigit() else "Нужно целое число."


# ---------------------------------------------------------------- .env I/O
def load_env(path):
    data = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_env(path, d):
    groups = [
        ("Telegram", ["GH_TREND_BOT_TOKEN", "GH_TREND_CHANNEL_ID"]),
        ("LLM (любой OpenAI-совместимый провайдер)", ["USE_AI", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"]),
        ("GitHub (опционально — поднимает лимит Search API)", ["GITHUB_TOKEN"]),
        ("Отбор «иглы в стоге»", ["NEEDLE_STARS_MIN", "NEEDLE_STARS_MAX", "NEEDLE_ROCKET_AGE",
                                  "NEEDLE_ROCKET_STARS_MAX", "NEEDLE_EVERGREEN_PUSH", "NEEDLE_JUDGE_TOPK",
                                  "NEEDLE_JUDGE_MIN", "NEEDLE_USE_JUDGE", "NEEDLE_SCREENSHOT"]),
        ("Прочее", ["TZ"]),
    ]
    out = ["# RepoRadar — конфигурация (сгенерировано мастером first_start.py)",
           "# by ProdX · prodx.pro · dev @Xuisuki + @mawlikow", ""]
    for gname, keys in groups:
        out.append(f"# --- {gname} ---")
        for k in keys:
            v = d.get(k, DEFAULTS.get(k, ""))
            if any(c in v for c in (" ", "#")) and not (v.startswith('"') and v.endswith('"')):
                v = f'"{v}"'
            out.append(f"{k}={v}")
        out.append("")
    open(path, "w", encoding="utf-8").write("\n".join(out))


def backup(path):
    if os.path.exists(path):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, f".env.backup-{ts}")
        shutil.copy2(path, dst)
        ui.note(f"Старый .env сохранён -> {dst}")


# ---------------------------------------------------------------- flow
def main():
    ui.intro("RepoRadar", "Скрытые GitHub-гемы -> в ваш Telegram-канал")
    e = load_env(ENV_PATH)
    if e:
        ui.note("Найден существующий .env — его значения предложены по умолчанию.")
    d = dict(DEFAULTS)
    d.update(e)

    # --- 1. Telegram ---
    ui.section(1, TOTAL, "Telegram: бот и канал")
    d["GH_TREND_BOT_TOKEN"] = ui.ask_text(
        "GH_TREND_BOT_TOKEN", "Токен вашего Telegram-бота — от его имени публикуются карточки.",
        where=["Откройте @BotFather в Telegram",
               "Команда /newbot -> задайте имя и @username бота",
               "Скопируйте выданный токен вида 123456789:AA..."],
        default=e.get("GH_TREND_BOT_TOKEN", ""), required=True, secret=True,
        validate=v_token, step="1/2")
    d["GH_TREND_CHANNEL_ID"] = ui.ask_text(
        "GH_TREND_CHANNEL_ID", "Канал, куда бот шлёт посты.",
        where=["Создайте канал и добавьте бота АДМИНИСТРАТОРОМ",
               "Публичный канал: укажите @username",
               "Приватный: числовой id вида -1001234567890 (узнать позже: python bot.py chatid)"],
        default=e.get("GH_TREND_CHANNEL_ID", ""), required=True,
        validate=v_channel, step="2/2")

    # --- 2. LLM ---
    ui.section(2, TOTAL, "LLM: описания и судья")
    use_ai = ui.ask_bool(
        "Включить ИИ?", "ИИ пишет русские описания репозиториев и оценивает кандидатов "
        "(судья 1-10, отсекает мусор). Без ИИ бот тоже работает — постит по метрикам.",
        default=e.get("USE_AI", "1") == "1", step="1/2")
    d["USE_AI"] = "1" if use_ai else "0"
    if use_ai:
        provider = ui.ask_choice(
            "LLM-провайдер", "Любой OpenAI-совместимый. По умолчанию — локальная Ollama (бесплатно).",
            [("ollama", "локально, бесплатно, без ключа"),
             ("groq", "облако, быстрый, бесплатный ключ"),
             ("openai", "облако, gpt-4o-mini"),
             ("openrouter", "облако, много моделей"),
             ("custom", "свой endpoint")],
            default="ollama", step="2/2")
        if provider == "custom":
            d["LLM_BASE_URL"] = ui.ask_text("LLM_BASE_URL", "Базовый URL OpenAI-совместимого API.",
                                            default=e.get("LLM_BASE_URL", ""), required=True)
            d["LLM_MODEL"] = ui.ask_text("LLM_MODEL", "Имя модели.",
                                         default=e.get("LLM_MODEL", ""), required=True)
            d["LLM_API_KEY"] = ui.ask_text("LLM_API_KEY", "Ключ (если провайдер требует).",
                                           default=e.get("LLM_API_KEY", ""), secret=True)
        else:
            base, model, need_key, hint = LLM_PRESETS[provider]
            d["LLM_BASE_URL"], d["LLM_MODEL"] = base, model
            ui.note(hint)
            if need_key:
                d["LLM_API_KEY"] = ui.ask_text(
                    "LLM_API_KEY", f"Ключ провайдера {provider}.", where=[hint],
                    default=e.get("LLM_API_KEY", ""), required=True, secret=True)
            else:
                d["LLM_API_KEY"] = ""
    else:
        d["LLM_API_KEY"] = e.get("LLM_API_KEY", "")

    # --- 3. GitHub token ---
    ui.section(3, TOTAL, "GitHub-токен (опционально)")
    if ui.ask_bool("Добавить GitHub-токен?",
                   "Без токена бот работает на пониженном лимите Search API. Токен снимает ограничение "
                   "(нужен без прав/scopes — только для чтения публичного поиска).",
                   default=bool(e.get("GITHUB_TOKEN")), step="1/1"):
        d["GITHUB_TOKEN"] = ui.ask_text(
            "GITHUB_TOKEN", "Personal Access Token для лимита поиска.",
            where=["github.com/settings/tokens -> Generate new token",
                   "Тип classic без галочек scope (публичный поиск), скопируйте ghp_..."],
            default=e.get("GITHUB_TOKEN", ""), secret=True)
    else:
        d["GITHUB_TOKEN"] = ""

    # --- 4. Отбор ---
    ui.section(4, TOTAL, "Параметры отбора и часовой пояс")
    if ui.ask_bool("Оставить рекомендуемые настройки отбора?",
                   "Окно звёзд 150-2500, судья от 7/10, скриншот продукта на карточке. "
                   "Нет — зададите вручную.", default=True, step="1/1"):
        ui.note("Оставляю рекомендованные значения NEEDLE_*.")
    else:
        d["NEEDLE_STARS_MIN"] = ui.ask_text("NEEDLE_STARS_MIN", "Нижняя граница звёзд.",
                                            default=d["NEEDLE_STARS_MIN"], validate=v_int)
        d["NEEDLE_STARS_MAX"] = ui.ask_text("NEEDLE_STARS_MAX", "Верхняя граница звёзд.",
                                            default=d["NEEDLE_STARS_MAX"], validate=v_int)
        d["NEEDLE_JUDGE_MIN"] = ui.ask_text("NEEDLE_JUDGE_MIN", "Минимальная оценка судьи (1-10).",
                                            default=d["NEEDLE_JUDGE_MIN"], validate=v_int)
    d["TZ"] = ui.ask_text("TZ", "Часовой пояс для дат на карточке.",
                          default=d.get("TZ", "Europe/Moscow"))

    # --- сводка ---
    ui.summary([
        ("Bot token", d["GH_TREND_BOT_TOKEN"], True),
        ("Канал", d["GH_TREND_CHANNEL_ID"], False),
        ("ИИ", "вкл" if d["USE_AI"] == "1" else "выкл", False),
        ("LLM", f"{d['LLM_MODEL']} @ {d['LLM_BASE_URL']}" if d["USE_AI"] == "1" else "—", False),
        ("LLM key", d.get("LLM_API_KEY", ""), True),
        ("GitHub token", d.get("GITHUB_TOKEN", ""), True),
        ("Часовой пояс", d["TZ"], False),
    ])
    if not ui.ask_bool("Сохранить в .env?",
                       "Записать значения в .env. Старый файл, если был, уйдёт в backup_env/.",
                       default=True):
        ui.bye("Отменено. Файл .env не тронут."); return

    backup(ENV_PATH)
    save_env(ENV_PATH, d)
    ui.check("Файл .env записан", True, ENV_PATH)

    # --- live-проверка ---
    if ui.ask_bool("Проверить бота сейчас?", "Вызову getMe через сам бот (python bot.py whoami).",
                   default=True):
        _run_check([sys.executable, "bot.py", "whoami"], "Telegram getMe")

    # --- финал ---
    ui.success(
        "RepoRadar настроен.",
        "./run.sh   (Windows: run.ps1)",
        ["Один запуск = один пост в канал.",
         "Сухой прогон без отправки:  python bot.py preview",
         "Не знаете id канала:        python bot.py chatid",
         "Публиковать по расписанию (каждые ~3ч): см. deploy/ (systemd / launchd / Task Scheduler)."])


def _run_check(cmd, label):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        ui.check(label, r.returncode == 0, out[-1] if out else "")
    except Exception as ex:
        ui.check(label, False, str(ex))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        ui.bye("Прервано.")
