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
3. Рекласс 97 с частично отсутствующими в ОСВ значениями (реклассдолгкорт_97) ->
   рекласс применяется к совпавшим РБП, отсутствующие отбрасываются по одной:
   problem_data в mismatches/, WARNING, шаг НЕ падает и НЕ теряет детализацию.
4. Тот же отчёт, где в ОСВ нет НИ ОДНОГО РБП -> пустая маска, шаг идёт дальше.
5. Регресс баланса: частичное применение не ломает суммы — по каждому РБП
   сумма «короткая + долгая» равна исходной.
6. Успешный кейс подготовки рекласса 97 -> (mask, reclass_dict).
7. Флаг SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR=False -> исключение
   пробрасывается (прежнее жёсткое поведение).
8. Успешный processor — результат возвращается как есть.

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
from pipeline.errors import InputDataError
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
PASSED = 0


def check(condition: bool, message: str) -> None:
    """Мини-ассерт с накоплением результата."""
    global PASSED
    if condition:
        PASSED += 1
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
# Тест 3: рекласс 97 — часть РБП отсутствует в ОСВ (частичная обработка)
# ==========================================================================
print("\n--- Тест 3: рекласс 97, часть значений нет в ОСВ ---")
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

# Прежде здесь ожидался ReferenceMismatchError и полный пропуск отчёта.
# Теперь совпавшие значения обрабатываются, отсутствующие отбрасываются по одной.
mask, reclass_dict = step7._prepare_97_reclass(osv_df, reclass_bad)
check(bool(mask.any()), "Реклас применён к совпавшему РБП (отчёт не отброшен целиком)")
check(
    reclass_dict == {"РБП-А": 1.0},
    f"Словарь рекласса содержит только совпавшее значение (факт: {reclass_dict})",
)
check(
    "в сводной ОСВ нет 1 из 2 РБП из отчёта" in warnings_text(),
    "WARNING называет число пропущенных и обработанных РБП",
)
check(
    "проверьте файл на полноту" not in warnings_text(),
    "WARNING «проверьте файл на полноту» не появляется — файл не сломан",
)
mismatch_files = list(get_output_dir("mismatches").glob("mismatch_step*.xlsx"))
check(len(mismatch_files) >= 1, "problem_data сохранён в mismatches/")
if mismatch_files:
    saved = pd.read_excel(mismatch_files[-1])
    check(
        len(saved) == 1 and saved["Субконто1"].iloc[0] == "РБП-НЕТ-В-ОСВ",
        f"В диагностику попал только пропущенный РБП (факт: {saved.to_dict('records')})",
    )

# ==========================================================================
# Тест 4: ни одного РБП из отчёта нет в ОСВ — шаг всё равно идёт дальше
# ==========================================================================
print("\n--- Тест 4: рекласс 97, в ОСВ нет ни одного РБП ---")
reclass_none = tmp_dir / "ТБ_реклассдолгкорт_97_чуждой_.xlsx"
pd.DataFrame([
    ["Титул отчета", None],
    ["Субконто1", "Итого"],
    ["РБП-НЕТ-1", 1000],
    ["РБП-НЕТ-2", 3000],
]).to_excel(reclass_none, index=False, header=False)

mask_none, dict_none = step7._prepare_97_reclass(osv_df, reclass_none)
check(
    not bool(mask_none.any()) and dict_none == {},
    "Пустая маска и пустой словарь — шаг продолжает штатно (_process_97_reclass)",
)
check(
    "в сводной ОСВ нет 2 из 2 РБП из отчёта" in warnings_text(),
    "WARNING сообщает, что не обработано ни одного значения",
)

# ==========================================================================
# Тест 5: регресс — частичное применение не ломает суммы (сходимость)
# ==========================================================================
print("\n--- Тест 5: суммы РБП после частичного рекласа ---")
reclass_mixed = tmp_dir / "ТБ_реклассдолгкорт_97_смешанный_.xlsx"
pd.DataFrame([
    ["Титул отчета", None],
    ["Субконто1", "Итого"],
    ["РБП-А", 4000],
    ["РБП-Б", 2000],
    ["РБП-НЕТ-В-ОСВ", 2000],
]).to_excel(reclass_mixed, index=False, header=False)

osv_saldo = pd.DataFrame({
    "допсубконто": ["РБП-А", "РБП-Б"],
    "долгая_короткая_часть": ["короткая часть", "короткая часть"],
    "сальдо, тыс.ед.": [10.0, 5.0],
})
mask_mixed, dict_mixed = step7._prepare_97_reclass(osv_saldo.copy(), reclass_mixed)
after = step7._apply_97_reclass(osv_saldo.copy(), mask_mixed, dict_mixed)

sums_after = after.groupby("допсубконто", observed=True)["сальдо, тыс.ед."].sum()
check(
    round(float(sums_after.get("РБП-А", 0.0)), 2) == 10.0
    and round(float(sums_after.get("РБП-Б", 0.0)), 2) == 5.0,
    f"Сумма каждого РБП сохранилась: {sums_after.to_dict()}",
)
check(
    round(float(after['сальдо, тыс.ед.'].sum()), 2) == 15.0,
    "Общая сумма сальдо не изменилась — баланс сойдётся",
)
check(
    int((after["долгая_короткая_часть"] == "долгая часть").sum()) == 2,
    "Созданы только две строки «долгая часть» — пропущенный РБП в отчёт не попал",
)
check(
    "РБП-НЕТ-В-ОСВ" not in set(after["допсубконто"]),
    "РБП, отсутствующий в ОСВ, не создал строк в сводной ОСВ",
)

# ==========================================================================
# Тест 6: успешная подготовка рекласса 97
# ==========================================================================
print("\n--- Тест 6: валидный рекласс 97 ---")
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
# Тест 7: жёсткий режим (SKIP_OPTIONAL_SPECIAL_REPORTS_ON_ERROR=False)
# ==========================================================================
print("\n--- Тест 7: флаг False — прежнее жёсткое поведение ---")
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
# Тест 8: успешный processor — результат возвращается как есть
# ==========================================================================
print("\n--- Тест 8: happy path хелпера ---")
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
total = PASSED + len(failures)
label = "необязательные спецотчёты: мягкий режим и жёсткий"
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{total}) — {label}")
    for f in failures:
        print(f"  [FAIL] {f}")
    exit_code = 1
else:
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    exit_code = 0

# Уборка: папка запуска и временные файлы
shutil.rmtree(run_dir, ignore_errors=True)
shutil.rmtree(tmp_dir, ignore_errors=True)

sys.exit(exit_code)

