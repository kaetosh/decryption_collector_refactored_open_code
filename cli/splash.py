# -*- coding: utf-8 -*-
"""
Заставка при запуске приложения «Собиратель расшифровок».

Титул набирается блочными глифами █ по таблице только для тех 14 букв,
что встречаются в названии (СОБИРАТЕЛЬ / РАСШИФРОВОК) — pyfiglet и
прочие FIGlet-шрифты латинские, кириллицу рисуют мусором, поэтому
глифы описаны здесь вручную. Два слова по 59 и 65 знаков влезают
в 80-колоночную консоль вместе с рамкой (65 + 4 = 69).

Рендер по приоритету:
  1. rich-панель с двойной рамкой ╔═╗ (rich==15.0.0, requirements.txt);
  2. те же строки голым print(), если rich не установлен (ImportError);
  3. ASCII-замена (█ -> #, рамка из +-|), если кодировка stdout
     не тянет блочные символы (редирект в файл на Windows с cp1251).

Заставка декоративная и не должна останавливать конвейер: show_splash()
глотает любую ошибку и пишет её в DEBUG-лог (app.log). Управление:
SHOW_SPLASH в config/settings.py и CLI-флаг --no-splash (см. cli/main.py).

Блок печатается в stdout, как и блоки пауз в pipeline/executors.py;
лог-запись шапки «Запуск приложения» уходит в loguru (stderr/app.log)
и остаётся без изменений.
"""
import sys
from datetime import datetime

from loguru import logger

from config.settings import APP_NAME, APP_VERSION

# Слова титула. Смоук сверяет их с APP_NAME — при смене имени приложения
# проверка укажет, что нужно дорисовать глифы новых букв.
TITLE_WORDS = ("СОБИРАТЕЛЬ", "РАСШИФРОВОК")

GLYPH_HEIGHT = 5
GLYPH_WIDTH = 5

# Каждый глиф — ровно GLYPH_HEIGHT строк по GLYPH_WIDTH символов
# из набора {"█", " "}. Пирамидки/хвосты выровнены так, чтобы слова
# после склейки через пробел читались без внутренних «швов».
_GLYPHS: dict[str, tuple[str, ...]] = {
    "А": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "Б": ("█████", "█    ", "█████", "█   █", "█████"),
    "В": ("████ ", "█   █", "████ ", "█   █", "████ "),
    "С": ("█████", "█    ", "█    ", "█    ", "█████"),
    "Е": ("█████", "█    ", "█████", "█    ", "█████"),
    "И": ("█   █", "█  ██", "█ █ █", "██  █", "█   █"),
    "К": ("█   █", "█  █ ", "███  ", "█  █ ", "█   █"),
    "Л": ("  ███", " ██ █", "██  █", "█   █", "█   █"),
    "О": ("█████", "█   █", "█   █", "█   █", "█████"),
    "Р": ("█████", "█   █", "█████", "█    ", "█    "),
    "Т": ("█████", "  █  ", "  █  ", "  █  ", "  █  "),
    "Ш": ("█ █ █", "█ █ █", "█ █ █", "█ █ █", "█████"),
    "Ф": ("  █  ", "█████", "█ █ █", "█████", "  █  "),
    "Ь": ("█    ", "█    ", "█████", "█   █", "█████"),
}

_DESCRIPTION = "Сбор расшифровки баланса и ОПУ из выгрузок 1С УПП"

# Число строк титула (два слова + разделитель между ними) — граница
# «титул/подвал» в build_splash_lines
TITLE_ROWS = GLYPH_HEIGHT * len(TITLE_WORDS) + (len(TITLE_WORDS) - 1)

# Символы рамки rich box.DOUBLE: проверяются вместе с текстом, потому что
# панель печатается только если кодировка тянет и блоки █, и рамку.
_PANEL_SAMPLE = "╔═╗╚╝║"

_ASCII_MAP = str.maketrans({"█": "#"})


def _render_word(word: str) -> list[str]:
    """
    Собирает слово из глифов: GLYPH_HEIGHT строк, буквы через пробел.

    Raises:
        ValueError: в слове есть буква без глифа (проверяет смоук
            для TITLE_WORDS — при смене имени приложения дорисуйте глиф).
    """
    glyphs: list[tuple[str, ...]] = []
    for char in word:
        glyph = _GLYPHS.get(char)
        if glyph is None:
            raise ValueError(f"нет глифа для буквы {char!r}")
        glyphs.append(glyph)
    return [
        " ".join(glyph[row] for glyph in glyphs)
        for row in range(GLYPH_HEIGHT)
    ]


def build_splash_lines(
    show_traceback: bool = False,
    verbose: bool = False,
    now: datetime | None = None,
) -> list[str]:
    """
    Строит все строки заставки: титул из глифов + подвал.

    Каждая строка центрируется по ширине самого длинного слова титула —
    тогда блок остаётся прямоугольным и ровно выглядит и внутри рамки
    rich, и в ASCII-фолбэке без переносов.

    Args:
        show_traceback: показать в подвале режим полной трассировки.
        verbose: показать в подвале режим DEBUG.
        now: момент запуска (для детерминированных проверок в смоуке).
    """
    now = now or datetime.now()
    rendered = [_render_word(word) for word in TITLE_WORDS]
    width = max(len(row) for rows in rendered for row in rows)

    # Пустая строка между словами: без неё низ одного слова сливался
    # с верхом следующего и граница двух слов на глаз не читалась.
    lines: list[str] = []
    for index, rows in enumerate(rendered):
        if index:
            lines.append("")
        lines.extend(row.center(width) for row in rows)
    lines.append("")
    lines.append(_DESCRIPTION.center(width))
    lines.append(
        f"Версия {APP_VERSION} | Запуск {now:%d.%m.%Y %H:%M:%S}".center(width)
    )
    modes = [
        name
        for flag, name in (
            (show_traceback, "трассировка стека"),
            (verbose, "DEBUG"),
        )
        if flag
    ]
    if modes:
        lines.append(("Режим: " + ", ".join(modes)).center(width))
    return lines


def _stdout_encodable(text: str, encoding: str | None = None) -> bool:
    """
    Поместится ли текст в кодировку stdout без потерь.

    encoding=None — кодировка реального sys.stdout (на Windows консоль
    отдаёт UTF-8, редирект в файл — локальную, например cp1251, где
    блочных символов нет). Явный encoding нужен смоуку.
    """
    encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _to_ascii(lines: list[str]) -> list[str]:
    """Заменяет блочные символы на ASCII-аналоги для узких кодировок."""
    return [line.translate(_ASCII_MAP) for line in lines]


def _print_ascii_fallback(lines: list[str]) -> None:
    """Печатает строки в рамке из +-| (замена rich-панели)."""
    width = max(len(line) for line in lines)
    border = "+" + "-" * (width + 2) + "+"
    body = "\n".join(f"| {line.ljust(width)} |" for line in lines)
    print(f"{border}\n{body}\n{border}", flush=True)


def _import_rich():
    """Импортирует rich по требованию — ImportError обрабатывает show_splash."""
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    return Console, Panel, Text, box


def _render_rich(lines: list[str], console=None) -> None:
    """
    Рисует заставку в rich-панели с двойной рамкой.

    Строки уже отцентрированы в build_splash_lines, поэтому Text —
    БЕЗ justify: rich при justify='center' отбрасывает хвостовые пробелы
    каждой строки и центрирует заново, а строки глифов кончаются разным
    числом пробелов (у «Ь» верхние ряды — 4 пробела, нижние — 0) —
    верхняя часть слов сдвигалась вправо относительно нижней.
    Если терминал уже самой широкой строки (+ рамка), рисуем без рамки,
    чтобы rich не переносил глифы.
    """
    Console, Panel, Text, box = _import_rich()
    active = console or Console()
    width = max(len(line) for line in lines)
    if active.width and active.width < width + 4:
        for line in lines:
            active.print(line)
        return

    text = Text()
    text.append("\n".join(lines[:TITLE_ROWS]), style="bold cyan")
    text.append("\n" + "\n".join(lines[TITLE_ROWS:]), style="cyan")
    active.print(Panel(text, box=box.DOUBLE, border_style="cyan", expand=False))
    # stdout может быть буферизован (редирект) — иначе заставка
    # в слитом логе окажется после строк loguru, которые пишутся в stderr
    try:
        active.file.flush()
    except Exception:
        pass


def show_splash(
    show_traceback: bool = False,
    verbose: bool = False,
    console=None,
) -> None:
    """
    Печатает заставку при запуске. Никогда не бросает исключений.

    console — rich-Console для тестов (по умолчанию создаётся свой
    с выводом в sys.stdout).
    """
    try:
        _show_splash(show_traceback, verbose, console)
    except Exception as exc:
        # Декоративный блок: его сбой не должен остановить обработку.
        # В DEBUG-лог (app.log) попадает, консоль INFO — не засоряет.
        logger.debug("Заставка не отрисована: {}", exc)


def _show_splash(
    show_traceback: bool,
    verbose: bool,
    console,
) -> None:
    lines = build_splash_lines(show_traceback, verbose)
    payload = "\n".join(lines)

    if not _stdout_encodable(payload + _PANEL_SAMPLE):
        # Кодировка не тянет блоки и рамку — сразу ASCII-вариант,
        # без попытки печати, чтобы в вывод не попала половина юникода.
        _print_ascii_fallback(_to_ascii(lines))
        return

    try:
        _render_rich(lines, console)
    except ImportError:
        # rich не установлен (требование вынесено отдельно) — печатаем
        # те же строки без рамки, заставка не должна ронять приложение.
        print(payload, flush=True)
