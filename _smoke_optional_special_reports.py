# -*- coding: utf-8 -*-
"""
Смоук-тест: необязательные спецотчёты не роняют конвейер.

Проверяет работу Step._run_optional_special_report() (pipeline/base.py) и
переработанных методов шагов 7/11 без полного запуска пайплайна:

1. Инцидент из задачи: файл расшифровки 1450/1550/1230, где заполнен только
   столбец «Договор», а «Краткосрочные»/«Долгосрочные» пусты ->
   _transform_lease_report даёт InputDataError, но _run_optional_special_report
   возвращает None + WARNING, шаг не падает.
2. Успешный кейс расшифровки 1450/1550/1230 — трансформация проходит насквозь.
3. ReferenceMismatchError (рекласс 97: значение Субконто1 отсутствует в ОСВ) ->
   problem_data сохраняется в mismatches/, результат None, шаг не падает.
4. Успешный кейс подготовки рекласса 97 -> (mask, reclass_dict).
5. Флаг SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR=False -> исключение
   пробрасывается (прежнее жёсткое поведение).
6. Успешный processor — результат возвращается как есть.

Запуск: conda run -n fl_acc_card python -u _smoke_optional_special_reports.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
from loguru import logger

from io_module.output_manager import configure_run, get_output_dir, get_run_dir
from pipeline.base import Step
from pipeline.errors import InputDataError, ReferenceMismatchError
from pipeline.steps.step_07_add_long_short import Step7AddLongShortTermColumnStep

# --- Подготовка лога для проверок WARNING ---------------------------------
log_messages: list = []
_sink_id = logger.add(log_messages.append, level="WARNING")

step7 = Step7AddLongShortTermColumnStep()

# Инициализация папки вывода (нужна для mismatches/)
run_id = configure_run()
run_dir = get_run_dir()

# Временная папка для тестовых xlsx
tmp_dir = Path(tempfile.mkdtemp(prefix="_smoke_optional_reports_"))

failures: list = []


def check(condition: bool, message: str) -> None:
    """Мини-ассерт с накоплением результата."""
    if condition:
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def warnings_text() -> str:
    return "\n".join(str(m) for m in log_messages)


# ==========================================================================
# Тест 1: инцидент — файл с пустыми «Краткосрочные»/«Долгосрочные»
# ==========================================================================
print("\n--- Тест 1: пустой отчёт 1450/1550/1230 (только «Договор») ---")
incident_path = tmp_dir / "ТБ_арендареклассдолгкорт_7697_6мес2026_.xlsx"
# Формат реального отчёта 1С: у колонки договоров НЕТ заголовка в строке
# «Краткосрочные» (поэтому после set_header_from_row она становится Unnamed_0),
# а в строке данных лежат имена договоров.
pd.DataFrame([
    ["Расшифровка строк 1450, 1550, 1230", None, None, None, None],
    [None, "Краткосрочные", "Долгосрочные", "Краткосрочные", "Долгосрочные"],
    ["Договор А", None, None, None, None],
    ["Договор Б", None, None, None, None],
]).to_excel(incident_path, index=False, header=False)

from io_module import DataLoader

raw_df = DataLoader.load_lease_report_decoding(incident_path)

# Прямой вызов трансформации: должно упасть с InputDataError (воспроизведение бага).
# Ловим именно InputDataError: он намеренно НЕ наследует ValueError (AGENTS.md),
# поэтому перехват ValueError пропустил бы ошибку насквозь и обрушил смоук.
raised = None
try:
    step7._transform_lease_report(raw_df)
except InputDataError as e:
    raised = e
check(
    raised is not None,
    "Трансформация пустого отчёта даёт InputDataError (воспроизведение)",
)
check(
    raised is not None and "Отсутствуют столбцы" in str(raised),
    "Текст ошибки — отсутствуют столбцы краткосрочные/долгосрочные",
)

# Оборачиваем в хелпер: шаг не должен упасть
result = step7._run_optional_special_report(
    label="Рекласс долгая/короткая часть (аренда)",
    report_file=incident_path.name,
    processor=lambda: step7._transform_lease_report(raw_df),
)
check(result is None, "Хелпер вернул None — шаг продолжается без детализации")
check(
    "Проверьте файл на полноту" in warnings_text(),
    "WARNING «проверьте файл на полноту и формат» записан в лог",
)
check(
    "Рекласс долгая/короткая часть (аренда)" in warnings_text(),
    "WARNING содержит метку отчёта",
)

# ==========================================================================
# Тест 2: успешная трансформация полного отчёта
# ==========================================================================
print("\n--- Тест 2: валидный отчёт 1450/1550/1230 ---")
valid_path = tmp_dir / "ТБ_лизингреклассдолгкорт_7697_6мес2026_.xlsx"
pd.DataFrame([
    ["Расшифровка строк 1450, 1550, 1230", None, None, None, None],
    [None, "Краткосрочные", "Долгосрочные", "Краткосрочные", "Долгосрочные"],
    ["Договор А", 100, 200, 300, 400],
    ["Договор Б", 500, 600, 700, 800],
    ["Итого", 600, 800, 1000, 1200],
]).to_excel(valid_path, index=False, header=False)

raw_valid = DataLoader.load_lease_report_decoding(valid_path)
transformed = step7._transform_lease_report(raw_valid)
# Результат — длинный формат: договор / группа_счетов / долгая_короткая_часть / сальдо, тыс.ед.
check(
    set(transformed.columns)
    == {"договор", "группа_счетов", "долгая_короткая_часть", "сальдо, тыс.ед."},
    "Длинный формат: 4 столбца после unpivot",
)
check(
    set(transformed["договор"]) == {"Договор А", "Договор Б", "Итого"},
    "Строки договоров сохранены (мелт без потери)",
)
short76 = transformed[
    (transformed["долгая_короткая_часть"] == "короткая часть")
    & (transformed["группа_счетов"] == "76")
]
check(
    sorted(short76["сальдо, тыс.ед."].tolist()) == [-0.6, -0.5, -0.1],
    "Коэффициент -1000 применён к 76 (100 -> -0.1 тыс.ед.)",
)

# ==========================================================================
# Тест 3: ReferenceMismatchError — рекласс 97, значения нет в ОСВ
# ==========================================================================
print("\n--- Тест 3: рекласс 97, отсутствующие значения (ReferenceMismatchError) ---")
reclass_bad = tmp_dir / "ТБ_реклассдолгкорт_97_6мес2026_.xlsx"
pd.DataFrame([
    ["Титул отчета", None],
    ["Субконто1", "Итого"],
    ["РБП-А", 1000],
    ["РБП-НЕТ-В-ОСВ", 2000],
]).to_excel(reclass_bad, index=False, header=False)

osv_df = pd.DataFrame({
    "допсубконто": ["РБП-А"],
    "долгая_короткая_часть": ["короткая часть"],
})

result = step7._run_optional_special_report(
    label="Рекласс РБП 97.21 (долгая/короткая часть)",
    report_file=reclass_bad.name,
    processor=lambda: step7._prepare_97_reclass(osv_df, reclass_bad),
)
check(result is None, "Хелпер вернул None — рекласс 97 пропущен без падения")
check(
    "Спецотчёт 'Рекласс РБП 97.21" in warnings_text(),
    "WARNING о пропуске рекласса 97 записан в лог",
)
mismatch_files = list(get_output_dir("mismatches").glob("mismatch_step*.xlsx"))
check(len(mismatch_files) >= 1, "problem_data сохранён в mismatches/")

# ==========================================================================
# Тест 4: успешная подготовка рекласса 97
# ==========================================================================
print("\n--- Тест 4: валидный рекласс 97 ---")
reclass_ok = tmp_dir / "ТБ_реклассдолгкорт_97_ок_.xlsx"
pd.DataFrame([
    ["Титул отчета", None],
    ["Субконто1", "Итого"],
    ["РБП-А", 1000],
]).to_excel(reclass_ok, index=False, header=False)

mask, reclass_dict = step7._prepare_97_reclass(osv_df, reclass_ok)
check(bool(mask.any()), "Маска рекласса находит строки ОСВ")
check(reclass_dict == {"РБП-А": 1.0}, "Словарь рекласса: 1000 ед. -> 1.0 тыс.ед.")

# ==========================================================================
# Тест 5: жёсткий режим (SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR=False)
# ==========================================================================
print("\n--- Тест 5: флаг False — прежнее жёсткое поведение ---")
import pipeline.base as base_module

base_module.SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR = False
try:
    hard_raised = None
    try:
        step7._run_optional_special_report(
            label="Тест жёсткого режима",
            report_file="тест.xlsx",
            processor=lambda: (_ for _ in ()).throw(ValueError("битый файл")),
        )
    except ValueError as e:
        hard_raised = e
    check(
        hard_raised is not None and "битый файл" in str(hard_raised),
        "При флаге False исключение пробрасывается наверх",
    )
finally:
    base_module.SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR = True

# ==========================================================================
# Тест 6: успешный processor — результат возвращается как есть
# ==========================================================================
print("\n--- Тест 6: happy path хелпера ---")
check(
    step7._run_optional_special_report(
        label="Успешный отчёт",
        report_file="ок.xlsx",
        processor=lambda: {"статус": "готово"},
    ) == {"статус": "готово"},
    "Результат успешного processor проходит насквозь без изменений",
)

# ==========================================================================
# Итог
# ==========================================================================
logger.remove(_sink_id)

print("\n=== ИТОГ ===")
if failures:
    print(f"Провалено проверок: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    exit_code = 1
else:
    print("Все проверки пройдены [OK]")
    exit_code = 0

# Уборка: папка запуска и временные файлы
shutil.rmtree(run_dir, ignore_errors=True)
shutil.rmtree(tmp_dir, ignore_errors=True)

sys.exit(exit_code)

