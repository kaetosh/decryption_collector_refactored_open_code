# -*- coding: utf-8 -*-
"""
Смоук: пути в логах и сообщениях сокращаются до BASE_DIR (config.settings.to_relative).

Проблема, которую решает: [SORT] печатал абсолютный путь архива —
"старая версия в архиве (C:\\Users\\...\\decryption_collector_refactored_open_code\\_INPUT_DATA\\_archive\\20261001_111851\\special_reports\\РБ_ведамор_0102_8мес2026_.xlsx)".
Строка не влезает в экран, и ради того, чтобы понять, что произошло, приходилось
открывать Проводник. Вся остальная выгрузка путей в логах давно идёт через
to_relative() — автосортировка осталась единственным исключением.

Проверяет:
1. to_relative: путь внутри BASE_DIR -> строка от '_INPUT_DATA'; без префикса BASE_DIR.
2. to_relative: путь вне BASE_DIR возвращается как есть (обрезать нечего).
3. to_relative: принимает и str, и Path (в логах встречаются оба).
4. Реальный вызов _move_to_dir() пишет в лог сокращённый путь архива —
   именно тот WARNING из лога прогона.
5. Волна 1 по-прежнему возвращает АБСОЛЮТНЫЕ пути (из a["куда"] строятся Path) —
   сокращать в отчёте нельзя, это сломало бы загрузку общей ОСВ.

Данные синтетические, реальные _INPUT_DATA/_OUTPUT_DATA не затрагиваются.

Запуск: conda run -n fl_acc_card python -u _smoke_log_paths.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

from loguru import logger

from config.settings import BASE_DIR, INPUT_DATA_DIR, to_relative
from io_module.auto_sort import _move_to_dir

sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PASSED = 0
FAILED = 0
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [OK] {message}")
    else:
        FAILED += 1
        failures.append(message)
        print(f"  [FAIL] {message}")


# ═════════════════════════════════════════════════════════════════════════
# Тест 1: to_relative — сам хелпер
# ═════════════════════════════════════════════════════════════════════════
print("\n--- Тест 1: to_relative ---")

archive_path = INPUT_DATA_DIR / "_archive" / "20261001_111851" / "special_reports" / "РБ.xlsx"
archive_text = to_relative(archive_path)

check(
    archive_text.startswith("_INPUT_DATA"),
    "Путь в архиве сокращается до строки, начинающейся с '_INPUT_DATA'",
)
check(
    "_archive" in archive_text and "special_reports" in archive_text,
    f"Структура пути сохранена полностью: {archive_text}",
)
check(
    str(BASE_DIR) not in archive_text,
    "Префикс BASE_DIR (с Users\\...\\Documents) из строки исчез",
)
check(
    not archive_text.startswith("C:"),
    "Абсолютный путь с буквой диска не утёк в строку",
)

outside = to_relative(Path(tempfile.gettempdir()) / "что-то" / "файл.xlsx")
check(
    "что-то" in outside,
    "Путь вне BASE_DIR возвращается как есть (обрезать нечего, терять нельзя)",
)

check(
    to_relative(str(archive_path)) == archive_text,
    "Принимает и Path, и str — результат одинаковый",
)

# ═════════════════════════════════════════════════════════════════════════
# Тест 2: реальный вызов _move_to_dir — лог автосортировки
# ═════════════════════════════════════════════════════════════════════════
print("\n--- Тест 2: _move_to_dir пишет сокращённый путь ---")

# Временные папки — ВНУТРИ проекта: to_relative() обрезает только путь
# относительно BASE_DIR, и папка в %TEMP% (вне BASE_DIR) проверку
# сокращения не прошла бы вовсе — вернулась бы как есть. Папка удаляется
# в конце смоука, реальные _INPUT_DATA/_OUTPUT_DATA не затрагиваются.
tmp_dir = BASE_DIR / "_smoke_log_paths_tmp"
shutil.rmtree(tmp_dir, ignore_errors=True)
records: list = []
sink_id = logger.add(
    lambda m: records.append(m.record["message"]),
    level="DEBUG",
)


def make_xlsx(path: Path, value: int) -> None:
    import pandas as pd
    pd.DataFrame({"a": [value]}).to_excel(path, index=False)


inbox = tmp_dir / "inbox"
target = tmp_dir / "special_reports"
archive = tmp_dir / "archive"
inbox.mkdir(parents=True)
target.mkdir(parents=True)

# Одноимённый файл с ДРУГИМ содержимым -> замена, старая версия в архиве.
# Это ровно тот случай, что дал WARNING из лога прогона 01.10.2026.
make_xlsx(target / "РБ_ведамор_0102_8мес2026_.xlsx", value=1)
make_xlsx(inbox / "РБ_ведамор_0102_8мес2026_.xlsx", value=2)

actions: list = []
_move_to_dir(
    inbox / "РБ_ведамор_0102_8мес2026_.xlsx",
    target,
    archive,
    actions,
    "перемещён из 00_inbox",
)

logger.remove(sink_id)

warning_text = next(
    (m for m in records if "другим содержимым" in m),
    "",
)
check(bool(warning_text), "WARNING о замене файла дошёл до лога")
check(
    str(BASE_DIR) not in warning_text,
    "В WARNING нет абсолютного префикса проекта (C:\\Users\\...\\Documents\\...)",
)

# Архивный путь в логе — это to_relative(archive / <имя файла>). Сверяем
# с самим архивом, а не с литералом '_INPUT_DATA': в смоуке папки лежат
# во временной папке проекта, а не в настоящем _INPUT_DATA.
archived_file = archive / "РБ_ведамор_0102_8мес2026_.xlsx"
check(
    to_relative(archived_file) in warning_text,
    "В логе путь архива ровно такой же, как to_relative от архива",
)
check(
    str(archived_file) not in warning_text,
    "Абсолютный путь архивного файла в лог не попал",
)

# Колонка 'куда' в отчёте — это НОВЫЙ файл (в целевой папке), архивный путь
# в actions не кладётся: из 'куда' строятся Path (auto_sort.py:596), поэтому
# там путь обязан остаться абсолютным.
new_file_action = next(
    a for a in actions if a["действие"] == "перезаписан (старый в архиве)"
)
new_file_path = Path(new_file_action["куда"])

check(
    new_file_path.is_absolute(),
    "Колонка 'куда' в отчёте осталась АБСОЛЮТНОЙ (из неё строятся Path)",
)
check(
    new_file_path.exists(),
    "Файл по пути из отчёта реально существует (сокращение сломало бы работу)",
)
check(
    new_file_path.name == "РБ_ведамор_0102_8мес2026_.xlsx"
    and new_file_path.parent == target,
    "Отчёт указывает на новый файл в целевой папке, а не на архивную копию",
)

# Смысл задачи — строка перестала быть длинной. Меряем не абсолютную длину
# (она зависит от имени файла), а выигрыш по сравнению с абсолютным вариантом.
absolute_variant = warning_text.replace(
    to_relative(archived_file), str(archived_file)
)
saved = len(absolute_variant) - len(warning_text)
check(
    saved == len(str(BASE_DIR)) + 1,
    f"Лог сокращён ровно на длину префикса проекта ({saved} симв.)",
)
check(
    len(warning_text) < len(absolute_variant),
    f"WARNING короче абсолютного варианта: {len(warning_text)} против {len(absolute_variant)}",
)

# ═════════════════════════════════════════════════════════════════════════
# Итог
# ═════════════════════════════════════════════════════════════════════════
shutil.rmtree(tmp_dir, ignore_errors=True)

print("\n=== ИТОГ ===")
total = PASSED + FAILED
label = "пути в логах сокращаются до BASE_DIR"
if FAILED:
    print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
    for item in failures:
        print(f"  [FAIL] {item}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)
