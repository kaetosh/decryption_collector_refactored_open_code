# -*- coding: utf-8 -*-
"""
Смоук заставки при запуске (cli/splash.py).

Проверяемые ветки рендера и интеграции:
  1. Глифы: все буквы TITLE_WORDS покрыты, каждый глиф — 5 строк по 5 символов.
  2. build_splash_lines: строки одинаковой ширины, ≤ 80-4 колонок (влезает
     в панель с рамкой), подвал содержит версию и описание.
  3. _to_ascii убирает блочные символы; _stdout_encodable ловит cp1251.
  4. Rich-ветка: панель с ╔═ печатается в подменённую Console, содержит
     █ и версию приложения.
  5. Фолбэки: ошибка rich глотается show_splash (никаких исключений).
  6. CLI: --no-splash парсится; cli/main.main принимает no_splash и
     передаёт его из entry_point.

Запуск: python _smoke_splash.py
"""
import io
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

# При перенаправлении вывода в файл консоль Windows пишет в cp1251 —
# фиксируем UTF-8, иначе символы вроде «≤» роняют print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

import cli.splash as splash

PASSED = 0
FAILED = 0


def check(condition: bool, message: str) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK] {message}")
    else:
        FAILED += 1
        print(f"  [FAIL] {message}")


def main() -> int:
    # --- 1. Глифы ---
    letters = set("".join(splash.TITLE_WORDS))
    missing = sorted(letters - set(splash._GLYPHS))
    check(not missing, f"глифы есть для всех букв названия (нет: {missing or '—'})")

    bad_shapes = {
        ch: (len(g), {len(row) for row in g})
        for ch, g in splash._GLYPHS.items()
        if len(g) != splash.GLYPH_HEIGHT
        or {len(row) for row in g} != {splash.GLYPH_WIDTH}
    }
    check(not bad_shapes, f"каждый глиф — {splash.GLYPH_HEIGHT}x{splash.GLYPH_WIDTH} (кривые: {bad_shapes or '—'})")

    chars_ok = all(set(row) <= {"█", " "} for g in splash._GLYPHS.values() for row in g)
    check(chars_ok, "в глифах только блок U+2588 и пробел")

    # --- 2. Сборка строк ---
    now = datetime(2026, 9, 26, 12, 34, 56)
    lines = splash.build_splash_lines(show_traceback=True, verbose=True, now=now)
    # Пустая строка-разделитель между титулом и подвалом шириной 0 — не ширина блока
    widths = {len(line) for line in lines if line}
    check(len(widths) == 1, f"непустые строки одной ширины: {sorted(widths)}")

    width = widths.pop()
    check(width + 4 <= 80, f"ширина блока {width} + рамка ≤ 80 колонок")
    check(width >= 59, f"ширина достаточна для глифового титула: {width}")

    title_rows = lines[: splash.TITLE_ROWS]
    check(any("█" in row for row in title_rows), "титул содержит блочные символы")

    # Разделитель между словами: без него низ одного слова сливался с верхом следующего
    check(
        lines[splash.GLYPH_HEIGHT] == "",
        "пустая строка-разделитель между двумя словами титула",
    )
    check(
        splash.TITLE_ROWS == splash.GLYPH_HEIGHT * len(splash.TITLE_WORDS) + len(splash.TITLE_WORDS) - 1,
        f"TITLE_ROWS учитывает разделитель: {splash.TITLE_ROWS}",
    )

    # Регресс центрирования: строки каждого слова должны иметь
    # одинаковый отступ (иначе верхняя часть слов «уезжает» вправо)
    word1 = title_rows[: splash.GLYPH_HEIGHT]
    word2 = title_rows[splash.GLYPH_HEIGHT + 1:]
    leads1 = {len(r) - len(r.lstrip(" ")) for r in word1}
    leads2 = {len(r) - len(r.lstrip(" ")) for r in word2}
    check(len(leads1) == 1, f"строки первого слова с одинаковым отступом: {leads1}")
    check(len(leads2) == 1, f"строки второго слова с одинаковым отступом: {leads2}")
    check(
        all(r.rstrip() for r in title_rows[: splash.GLYPH_HEIGHT]),
        "строки первого слова не пустые",
    )

    footer = "\n".join(lines[splash.TITLE_ROWS:])
    check("1.0.0" in footer, "в подвале версия приложения")
    check("26.09.2026 12:34:56" in footer, f"в подвале дата запуска: {footer.splitlines()[-2:]}")
    check(splash._DESCRIPTION in footer, f"в подвале описание: {splash._DESCRIPTION}")
    check("трассировка стека" in footer and "DEBUG" in footer, "режимы отражены в подвале")

    plain = splash.build_splash_lines(now=now)
    check(
        "Режим:" not in "\n".join(plain),
        "без флагов строка «Режим:» в подвале не добавляется",
    )

    # --- 3. ASCII / кодировки ---
    ascii_lines = splash._to_ascii(lines)
    check(
        not any("█" in line for line in ascii_lines),
        "после _to_ascii блочных символов не остаётся",
    )
    check(all("#" in line for line in ascii_lines[:2]), "блок U+2588 заменён на #")

    check(splash._stdout_encodable("█", "cp1251") is False, "cp1251 не тянет блочные символы")
    check(splash._stdout_encodable("█", "utf-8") is True, "utf-8 тянет блочные символы")

    # --- 4. Rich-ветка ---
    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, width=80, no_color=True)
    with mock.patch.object(splash, "_stdout_encodable", return_value=True):
        splash.show_splash(show_traceback=False, verbose=False, console=console)
    out = buf.getvalue()
    check("█" in out, "rich: блочные символы попали в вывод")
    check("╔" in out and "╚" in out, "rich: двойная рамка панели")
    check("1.0.0" in out, "rich: версия в подвале панели")
    check(splash._DESCRIPTION in out, "rich: описание в подвале панели")

    # Регресс: rich при justify='center' центрирует строки заново после
    # отбрасывания хвостовых пробелов — верх слов сдвигался вправо.
    # Отступ глифовых строк внутри каждого слова должен быть одинаковым.
    glyph_leads = [
        len(body := line[1:]) - len(body.lstrip(" "))
        for line in out.splitlines()
        if line.startswith("║") and "█" in line
    ]
    check(len(glyph_leads) == splash.GLYPH_HEIGHT * len(splash.TITLE_WORDS),
          f"rich: все глифовые строки в панели ({len(glyph_leads)})")
    per_word = [glyph_leads[i:i + splash.GLYPH_HEIGHT]
                for i in range(0, len(glyph_leads), splash.GLYPH_HEIGHT)]
    check(
        all(len(set(leads)) == 1 for leads in per_word),
        f"rich: отступ строк внутри каждого слова одинаков: {per_word}",
    )

    # --- 5. Фолбэк: rich падает — show_splash не бросает ---
    with mock.patch.object(splash, "_render_rich", side_effect=RuntimeError("boom")), \
            mock.patch.object(splash, "_stdout_encodable", return_value=True):
        with mock.patch.object(splash, "logger") as fake_logger:
            try:
                splash.show_splash()
                swallowed = True
            except Exception:
                swallowed = False
            check(swallowed, "ошибка рендера глотается show_splash")
            check(
                fake_logger.debug.called,
                "сбой заставки уходит в DEBUG-лог, а не в консоль",
            )

    # rich не установлен (ImportError) — голый print без рамки
    with mock.patch.object(
        splash, "_import_rich", side_effect=ImportError("no rich")
    ), mock.patch.object(splash, "_stdout_encodable", return_value=True):
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            splash.show_splash()
        plain_out = captured.getvalue()
        check("█" in plain_out and "╔" not in plain_out, "без rich: те же строки без рамки")

    # Кодировка не тянет юникод — ASCII-фолбэк
    with mock.patch.object(splash, "_stdout_encodable", return_value=False):
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            splash.show_splash()
        ascii_out = captured.getvalue()
        check("█" not in ascii_out and "#" in ascii_out, "узкая кодировка: ASCII-фолбэк с рамкой +-|")
        check("+" in ascii_out and "|" in ascii_out, "ASCII-фолбэк: рамка из +-|")

    # --- 6. CLI ---
    from cli.arguments import parse_arguments

    argv_backup = sys.argv
    try:
        sys.argv = ["main.py", "--no-splash", "--no-interactive"]
        args = parse_arguments()
        check(args.no_splash is True, "--no-splash распознан флагом")
        check(args.no_interactive is True, "соседний --no-interactive не сломан")

        sys.argv = ["main.py"]
        args = parse_arguments()
        check(args.no_splash is False, "без флага no_splash равен False")
    finally:
        sys.argv = argv_backup

    # importlib: пакет cli в __init__ экспортирует функцию main,
    # и атрибут cli.main затеняет одноимённый модуль
    import importlib
    import inspect
    cli_main = importlib.import_module("cli.main")

    sig = inspect.signature(cli_main.main)
    check("no_splash" in sig.parameters, "cli/main.main принимает no_splash")

    source = inspect.getsource(cli_main.entry_point)
    check("no_splash=args.no_splash", "entry_point передаёт no_splash в main()")

    source_main = inspect.getsource(cli_main.main)
    check(
        "SHOW_SPLASH and not no_splash" in source_main,
        "main() учитывает SHOW_SPLASH и флаг --no-splash",
    )
    check(
        "show_splash(" in source_main,
        "main() вызывает show_splash до loguru-шапки",
    )

    total = PASSED + FAILED
    verdict = "SMOKE_OK" if not FAILED else "SMOKE_FAIL"
    print(f"\n{verdict} ({PASSED}/{total} проверок)")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
