# -*- coding: utf-8 -*-
"""
Смоук-тест автосортировки inbox (io_module/auto_sort.py).

Проверяет обе волны на временных папках (реальные _INPUT_DATA не затрагиваются):

1. normalize_name: регистр, пробелы, отсутствующий завершающий '_'.
2. Волна 1: общая ОСВ переносится из inbox в general_osv;
   левый файл general_osv — в архив; чужой файл inbox не тронут.
3. Волна 2 (sort_all + build_expected_map):
   - точное имя — перенос по назначению;
   - нормализованное имя (пробел/регистр/без хвостового '_') — перенос;
   - спецотчёт -> special_reports, .txt-карточка -> transaction_report;
   - файл, уже лежащий в нужной папке, остаётся на месте ('уже на месте');
   - дубликат с идентичным содержимым — копия из inbox удалена;
   - то же имя с другим содержимым — заменён, старый в архиве;
   - нераспознанный файл inbox — архив/unsorted;
   - хвост прошлой сессии в целевой папке — в архив;
   - файл из списка в неверной папке — перекладывается по назначению.
4. Отчёт sort_report.xlsx записан в папку запуска.

Запуск: conda run -n fl_acc_card python -u _smoke_auto_sort.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
from loguru import logger

from io_module.output_manager import configure_run, get_run_dir
from io_module.auto_sort import (
    build_expected_map,
    normalize_name,
    prepare_general_osv_from_inbox,
    sort_all,
)

configure_run()
run_dir = get_run_dir()
tmp_dir = Path(tempfile.mkdtemp(prefix="_smoke_auto_sort_"))
failures: list = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def make_xlsx(path: Path, value: int = 1) -> None:
    pd.DataFrame({"a": [value]}).to_excel(path, index=False)


def names_in(folder: Path) -> set:
    if not folder.exists():
        return set()
    return {f.name for f in folder.iterdir() if f.is_file() and not f.name.startswith("~$")}


# ═════════════════════════════════════════════════════════════════════════
# Тест 0: normalize_name
# ═════════════════════════════════════════════════════════════════════════
print("\n--- Тест 0: normalize_name ---")
check(
    normalize_name("СМПК_осв_01_6мес2026_.xlsx") == "смпк_осв_01_6мес2026.xlsx",
    "Хвостовое '_' и регистр схлопываются",
)
check(
    normalize_name("смпк_осв 60_6мес2026.xlsx")
    == normalize_name("СМПК_осв_60_6мес2026_.xlsx"),
    "Пробел вместо '_' даёт тот же ключ, что и эталонное имя",
)
check(
    normalize_name("СМПК_осв_76.07_6мес2026_.xlsx") != normalize_name("СМПК_осв_76_6мес2026_.xlsx"),
    "Точки в номере счета не схлопываются (76.07 != 76)",
)
check(
    normalize_name("СМПК_отчпровод_26_6мес2026_.TXT")
    != normalize_name("СМПК_отчпровод_26_6мес2026_.xlsx"),
    "Расширение различает типы выгрузок (txt vs xlsx)",
)

# ═════════════════════════════════════════════════════════════════════════
# Тест 1: волна 1 — общая ОСВ
# ═════════════════════════════════════════════════════════════════════════
print("\n--- Тест 1: волна 1 (общая ОСВ) ---")
w1_inbox = tmp_dir / "w1_inbox"
w1_general = tmp_dir / "w1_general_osv"
w1_archive = tmp_dir / "w1_archive"
w1_inbox.mkdir(parents=True)
w1_general.mkdir(parents=True)

make_xlsx(w1_inbox / "СМПК_общаяосв_нд_6мес2026_.xlsx")
(w1_inbox / "лишний_файл.txt").write_text("не трогать", encoding="utf-8")
make_xlsx(w1_general / "старый_левый_файл_.xlsx")

moved = prepare_general_osv_from_inbox(
    inbox_dir=w1_inbox, target_dir=w1_general, archive_dir=w1_archive / "general_osv",
)
check("СМПК_общаяосв_нд_6мес2026_.xlsx" in names_in(w1_general), "Общая ОСВ перенесена в general_osv")
check("СМПК_общаяосв_нд_6мес2026_.xlsx" not in names_in(w1_inbox), "Общая ОСВ ушла из inbox")
check("лишний_файл.txt" in names_in(w1_inbox), "Чужой файл inbox не тронут (ждёт волну 2)")
check("старый_левый_файл_.xlsx" in names_in(w1_archive / "general_osv"), "Левый файл general_osv в архиве")
check(len(moved) == 1, "Функция вернула список перенесённых файлов")

# ═════════════════════════════════════════════════════════════════════════
# Тест 2: волна 2 — раскладка по списку выгрузок
# ═════════════════════════════════════════════════════════════════════════
print("\n--- Тест 2: волна 2 (раскладка по списку) ---")
base = tmp_dir / "w2"
inbox = base / "00_inbox"
accounts = base / "accounts_osv"
lease = base / "accounts_osv_lease"
special = base / "special_reports"
cards = base / "transaction_report"
archive = base / "_archive"
for d in (inbox, accounts, lease, special, cards):
    d.mkdir(parents=True)

# Список выгрузок в ФАКТИЧЕСКОМ формате шага 1a: на листе 1 колонка
# 'куда класть' с маленькой буквы (приходит из справочника «Выгрузки»),
# на листе 2 — две служебные строки сверху, шапка в строке 3
expected_rows = [
    ("СМПК_осв_01_6мес2026_.xlsx", str(accounts)),
    ("СМПК_осв_02_6мес2026_.xlsx", str(accounts)),
    ("СМПК_осв_04_6мес2026_.xlsx", str(accounts)),
    ("СМПК_осв_60_6мес2026_.xlsx", str(accounts)),
    ("СМПК_осв_76.07_6мес2026_.xlsx", str(lease)),
    ("СМПК_отчпровод_26_6мес2026_.txt", str(cards)),
    ("СМПК_отчпровод_44_6мес2026_.txt", str(cards)),
]
special_rows = [
    ("СМПК_ведамор_0102_6мес2026_.xlsx", str(special)),
    ("СМПК_реклассдолгкорт_97_6мес2026_.xlsx", str(special)),
]
expected_path = run_dir / "Выгрузить_СМПК_6мес2026.xlsx"
with pd.ExcelWriter(expected_path, engine="openpyxl") as writer:
    pd.DataFrame(expected_rows, columns=["Имя файла для сохранения", "куда класть"]).to_excel(
        writer, sheet_name="Обязательные выгрузки", index=False,
    )
    spec_df = pd.DataFrame(special_rows, columns=["Имя файла для сохранения", "Куда класть"])
    spec_df.to_excel(writer, sheet_name="Спецотчеты", index=False, startrow=2)

expected_map = build_expected_map("СМПК", "6мес2026", run_dir)
check(len(expected_map) == 9, f"Карта ожидаемых файлов из двух листов: {len(expected_map)} == 9")

# --- Предустановка ---
# Точный файл из inbox
make_xlsx(inbox / "СМПК_осв_01_6мес2026_.xlsx")
# «Кривое» имя: пробел, нижний регистр, без хвостового '_'
make_xlsx(inbox / "смпк_осв 60_6мес2026.xlsx")
# Спецотчёт и карточка
make_xlsx(inbox / "СМПК_ведамор_0102_6мес2026_.xlsx")
(cards_expected := inbox / "СМПК_отчпровод_26_6мес2026_.txt").write_text("26;проводки", encoding="utf-8")
# Нераспознанный
make_xlsx(inbox / "мусор_файл.xlsx")
# Дубликат: идентичные копии в accounts_osv и в inbox (байт-в-байт)
make_xlsx(accounts / "СМПК_осв_02_6мес2026_.xlsx", value=7)
shutil.copyfile(accounts / "СМПК_осв_02_6мес2026_.xlsx", inbox / "СМПК_осв_02_6мес2026_.xlsx")
# Конфликт: то же имя, другое содержимое (old в папке, new в inbox)
make_xlsx(accounts / "СМПК_осв_04_6мес2026_.xlsx", value=111)
make_xlsx(inbox / "СМПК_осв_04_6мес2026_.xlsx", value=222)
# Хвост прошлой сессии (не в списке)
make_xlsx(accounts / "СМПК_осв_99_12мес2025_.xlsx")
# Файл из списка, но в неверной папке
(cards / "misplaced_tmp.txt").write_text("", encoding="utf-8")  # шум, останется
shutil.copyfile(cards_expected, accounts / "СМПК_отчпровод_44_6мес2026_.txt")
# Уже на месте (правильная папка)
make_xlsx(lease / "СМПК_осв_76.07_6мес2026_.xlsx", value=5)
# Файл списка выгрузок, скопированный пользователем в inbox, — должен остаться
shutil.copyfile(expected_path, inbox / "Выгрузить_СМПК_6мес2026.xlsx")
# Общая ОСВ в inbox (в списке выгрузок её нет): идентичная копия уже в general_osv
general = base / "general_osv"
general.mkdir(parents=True)
make_xlsx(general / "СМПК_общаяосв_нд_6мес2026_.xlsx", value=42)
shutil.copyfile(general / "СМПК_общаяосв_нд_6мес2026_.xlsx", inbox / "СМПК_общаяосв_нд_6мес2026_.xlsx")

actions = sort_all(expected_map, inbox_dir=inbox, archive_dir=archive, general_osv_dir=general)
actions_by_file = {a["файл"]: a["действие"] for a in actions}

check("СМПК_осв_01_6мес2026_.xlsx" in names_in(accounts), "Точное имя -> accounts_osv")
check(
    "СМПК_осв_60_6мес2026_.xlsx" in names_in(accounts)
    and "смпк_осв 60_6мес2026.xlsx" not in names_in(accounts)
    and "смпк_осв 60_6мес2026.xlsx" not in names_in(inbox),
    "Кривое имя распознано, перенесено и переименовано к эталону",
)
check("СМПК_ведамор_0102_6мес2026_.xlsx" in names_in(special), "Спецотчёт -> special_reports")
check("СМПК_отчпровод_26_6мес2026_.txt" in names_in(cards), ".txt-карточка -> transaction_report")
check("СМПК_отчпровод_44_6мес2026_.txt" in names_in(cards), "Файл из неверной папки переложен по назначению")
check(
    "СМПК_отчпровод_44_6мес2026_.txt" not in names_in(accounts),
    "В старой (неверной) папке файла больше нет",
)
check("СМПК_осв_76.07_6мес2026_.xlsx" in names_in(lease), "Файл в правильной папке не тронут")
check(actions_by_file.get("СМПК_осв_76.07_6мес2026_.xlsx") == "уже на месте", "Действие 'уже на месте' записано")
check("СМПК_осв_02_6мес2026_.xlsx" not in names_in(inbox), "Дубликат удалён из inbox")
check(actions_by_file.get("СМПК_осв_02_6мес2026_.xlsx") == "дубликат удалён (идентичен уже лежащему)", "Действие 'дубликат удалён' записано")
check(pd.read_excel(accounts / "СМПК_осв_02_6мес2026_.xlsx")["a"].iloc[0] == 7, "При дубликате в папке остался исходный файл")
check(pd.read_excel(accounts / "СМПК_осв_04_6мес2026_.xlsx")["a"].iloc[0] == 222, "Конфликт: в папке новая версия")
check(actions_by_file.get("СМПК_осв_04_6мес2026_.xlsx") == "перезаписан (старый в архиве)", "Действие 'перезаписан' записано")
check("СМПК_осв_04_6мес2026_.xlsx" in names_in(archive / "accounts_osv"), "Старая версия конфликта в архиве")
check("СМПК_осв_99_12мес2025_.xlsx" in names_in(archive / "accounts_osv"), "Хвост прошлой сессии заархивирован")
check("СМПК_осв_99_12мес2025_.xlsx" not in names_in(accounts), "Хвост удалён из целевой папки")
check("мусор_файл.xlsx" in names_in(archive / "unsorted"), "Нераспознанный файл в архив/unsorted")
check(
    "СМПК_общаяосв_нд_6мес2026_.xlsx" in names_in(general)
    and "СМПК_общаяосв_нд_6мес2026_.xlsx" not in names_in(inbox),
    "Копия общей ОСВ из inbox распознана, дубликат удалён (общая ОСВ осталась в general_osv)",
)
check(
    "Выгрузить_СМПК_6мес2026.xlsx" in names_in(inbox),
    "Файл списка выгрузок оставлен в 00_inbox",
)
check(
    names_in(inbox) == {"Выгрузить_СМПК_6мес2026.xlsx"},
    "Inbox после сортировки содержит только файл списка выгрузок",
)
check((run_dir / "sort_report.xlsx").is_file(), "Отчёт sort_report.xlsx записан в папку запуска")

# ═════════════════════════════════════════════════════════════════════════
# Итог
# ═════════════════════════════════════════════════════════════════════════
print("\n=== ИТОГ ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    exit_code = 1
else:
    print("Все проверки пройдены [OK]")
    exit_code = 0

shutil.rmtree(run_dir, ignore_errors=True)
shutil.rmtree(tmp_dir, ignore_errors=True)
sys.exit(exit_code)
