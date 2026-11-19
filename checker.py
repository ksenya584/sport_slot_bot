"""
Бот, который проверяет страницы записи и пишет в Telegram,
когда появляются свободные слоты.

Список страниц — в файле config.json.
Что бот видел в прошлый раз — в файле state.json (его обновляет сам бот).
"""

import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

# Сколько неудачных проверок подряд терпим, прежде чем сообщить об ошибке
# (при запуске раз в 5 минут 6 ошибок ≈ 30 минут).
ERRORS_BEFORE_ALERT = 6

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ---------- Загрузка страницы и превращение HTML в текст ----------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "header", "footer", "button", "a", "td", "span"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def page_lines(html):
    parser = _TextExtractor()
    parser.feed(html)
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return [line for line in lines if line]


def download(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def norm(s):
    return s.lower().replace("ё", "е")


# ---------- Проверщики для разных сайтов ----------
# Каждый проверщик возвращает (есть_слоты: bool, описание: str).
# Чтобы добавить новый сайт с другим устройством страницы,
# достаточно написать здесь новую функцию и добавить её в CHECKERS.

def check_timepad(page):
    """
    Timepad: под заголовком «Регистрация» либо написано
    «Извините, регистрация закрыта», либо выложены слоты.
    """
    lines = page_lines(download(page["url"]))
    closed_text = norm(page.get("closed_text", "регистрация закрыта"))

    # Ищем блок под заголовком «Регистрация»
    start = None
    for i, line in enumerate(lines):
        if norm(line) == "регистрация":
            start = i + 1
    section = lines[start:start + 40] if start is not None else []

    if any(closed_text in norm(line) for line in (section or lines)):
        return False, "регистрация закрыта"

    details = "\n".join(section)[:1500] if section else ""
    return True, details


def check_text(page):
    """
    Универсальный вариант для любого сайта: слотов нет, пока на странице
    есть текст из поля "closed_text" в config.json.
    """
    lines = page_lines(download(page["url"]))
    closed_text = norm(page["closed_text"])
    if any(closed_text in norm(line) for line in lines):
        return False, "нет слотов"
    return True, ""


CHECKERS = {
    "timepad": check_timepad,
    "text": check_text,
}


# ---------- Telegram ----------

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("!! Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — сообщение не отправлено:")
        print(text)
        return
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        resp.read()


# ---------- Основная логика ----------

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    test_mode = "--test" in sys.argv
    config = load_json(CONFIG_FILE, {"pages": []})
    state = load_json(STATE_FILE, {})
    report = []

    for page in config["pages"]:
        url, name = page["url"], page.get("name", page["url"])
        prev = state.get(url, {})
        checker = CHECKERS[page.get("type", "timepad")]

        try:
            is_open, details = checker(page)
        except Exception as e:  # сайт недоступен и т.п.
            errors = prev.get("errors", 0) + 1
            print(f"[ОШИБКА] {name}: {e}")
            report.append(f"⚠️ {name}: ошибка — {e}")
            if errors == ERRORS_BEFORE_ALERT:
                send_telegram(f"⚠️ Не получается проверить страницу уже "
                              f"{errors} раз подряд:\n{name}\n{url}\n\nОшибка: {e}")
            state[url] = {**prev, "errors": errors}
            continue

        digest = hashlib.sha256(details.encode()).hexdigest()[:16]
        print(f"[{'ЕСТЬ СЛОТЫ' if is_open else 'нет слотов'}] {name}")
        report.append(f"{'🟢' if is_open else '🔴'} {name}: "
                      f"{'есть слоты' if is_open else 'слотов нет'}")

        if is_open and not prev.get("open"):
            send_telegram(f"🟢 Появились свободные слоты!\n{name}\n{url}"
                          + (f"\n\n{details}" if details else ""))
        elif is_open and prev.get("digest") != digest:
            send_telegram(f"🔄 Слоты изменились:\n{name}\n{url}"
                          + (f"\n\n{details}" if details else ""))
        elif not is_open and prev.get("open"):
            send_telegram(f"🔴 Регистрация снова закрыта:\n{name}\n{url}")

        state[url] = {"open": is_open, "digest": digest, "errors": 0}

    if test_mode:
        send_telegram("✅ Бот работает. Сейчас:\n\n" + "\n".join(report))

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
