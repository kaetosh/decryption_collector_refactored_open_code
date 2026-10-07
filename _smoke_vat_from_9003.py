# -*- coding: utf-8 -*-
"""
Смоук: выручка 90.01 минус НДС из отчёта по проводкам 90.03 (шаг 14).

Заменяет '_smoke_vat_missing_rates.py': НДС больше не вычисляется из ставки в
субконто 90.01 ('Субконто Кт_2'), а берётся готовой суммой из отчёта 90.03.
Проверяются методы '_process_revenue_9001', '_extract_vat_9003',
'_subtract_vat_from_revenue', '_warn_unmatched_vat' и '_validate_revenue_against_osv'
(pipeline/steps/_step14_accounts.py):

1. НДС вычитается из выручки документа ровно один раз.
2. Регресс RO 07.10.2026: текстовая/пустая ставка в 'Субконто Кт_2'
   ('Exempt from Tax', 'Без НДС', мусор) НЕ влияет на результат — колонка
   вообще не читается, и её отсутствие не роняет шаг.
3. Документ без НДС в 90.03 (освобождение) не делится на (1+ставка):
   выручка остаётся валовой.
4. Документ с несколькими строками 90.01: НДС распределяется по строкам,
   суммарно вычитается ровно один раз (регресс «задвоение»).
5. Рублёвый эквивалент уменьшается на НДС в рублях, а не по курсу 90.01.
6. НДС 90.03 без пары в 90.01 -> WARNING + строки попадают в problem_data
   ConvergenceError вместе с итогом сверки.
7. Пустой 90.03 при нулевом обороте 90.03 в ОСВ -> сходимость без WARNING.
8. Сквозной синтетический прогон '_process_revenue_9001' + сверка с ОСВ.

Запуск: conda run -n fl_acc_card python -u _smoke_vat_from_9003.py
"""
import sys

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.errors import ConvergenceError
from pipeline.steps.step_14_build_opu_foundation import Step14BuildOpuFoundationStep

# Перехват сообщений -> сводка предупреждений прогона и INFO-сообщения шага
log_messages: list = []
info_messages: list = []
_sink_id = logger.add(log_messages.append, level="WARNING")
_sink_info_id = logger.add(
    lambda msg: info_messages.append(str(msg)), level="INFO", format="{message}"
)

failures: list = []
PASSED = 0

_STEP = Step14BuildOpuFoundationStep()
_TOL = 200.0


def check(condition: bool, message: str) -> None:
    """Проверка 'Ожидание Факт': успех увеличивает PASSED, провал — failures."""
    global PASSED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def warnings_text() -> str:
    return "\n".join(str(m) for m in log_messages)


def info_text() -> str:
    return "\n".join(info_messages)


def reset_warnings() -> None:
    log_messages.clear()
    info_messages.clear()


def row_9001(
    doc: str,
    amount: float,
    *,
    contract: str = "Покупатель 1",
    group: str = "Группа",
    rate: str = "22%",
    rub: float | None = None,
) -> dict:
    """Строка отчёта по проводкам 90.01 (после обработки процессором 1С)."""
    return {
        "Имя_файла": "smoke_отчпровод_90.01_test.txt",
        "Документ": doc,
        "Дт": "62.01",
        "Кт": "90.01",
        "Субконто Дт_1": contract,
        "Субконто Кт_1": group,
        "Субконто Кт_2": rate,
        "Сумма": amount,
        "Сумма_руб": amount if rub is None else rub,
    }


def row_9003(doc: str, amount: float, *, rub: float | None = None) -> dict:
    """Строка отчёта по проводкам 90.03 (Дт 90.03 Кт 68.02)."""
    return {
        "Имя_файла": "smoke_отчпровод_90.03_test.txt",
        "Документ": doc,
        "Дт": "90.03",
        "Кт": "68.02",
        "Субконто Дт_1": "Группа",
        "Сумма": amount,
        "Сумма_руб": amount if rub is None else rub,
    }


def osv(revenue_credit: float, vat_debit: float) -> pd.DataFrame:
    """Общая ОСВ: 90.01 (кредитовый оборот) и 90.03 (дебетовый оборот)."""
    return pd.DataFrame({
        "Счет": ["90.01", "90.03"],
        "Кредит_оборот": [revenue_credit, 0.0],
        "Дебет_оборот": [0.0, vat_debit],
    })


def context(**overrides) -> ProcessingContext:
    params = {"tolerance_reconciliation": _TOL}
    params.update(overrides)
    return ProcessingContext(
        company="smoke_vat", period="test", currency="RUB",
        tolerance_params=params,
    )


# ==========================================================================
# Тест 1: НДС вычитается из документа ровно один раз
# ==========================================================================
print("\n--- Тест 1: вычитание НДС документа ---")
trans = pd.DataFrame([
    row_9001("Продажа 1", 122_000.0),   # 100 000 + НДС 22 000
    row_9001("Продажа 2", 105_000.0),   # 100 000 + НДС 5 000
    row_9003("Продажа 1", 22_000.0),
    row_9003("Продажа 2", 5_000.0),
])
df9001 = _STEP._process_revenue_9001(
    trans,
    osv(227_000.0, 27_000.0),
    context(),
)
by_doc = df9001.set_index("Документ")["выручка_без_ндс_тыс_ед"].to_dict()
check(abs(by_doc["Продажа 1"] - 100.0) < 1e-9, "Продажа 1: 122 000 - 22 000 = 100 тыс.ед.")
check(abs(by_doc["Продажа 2"] - 100.0) < 1e-9, "Продажа 2: 105 000 - 5 000 = 100 тыс.ед.")
check(len(df9001) == 2, "обе строки выручки сохранены")

# ==========================================================================
# Тест 2: регресс RO — ставка в субконто не читается
# ==========================================================================
print("\n--- Тест 2: регресс RO (текстовая/пустая ставка) ---")
reset_warnings()
base_rows = [
    row_9001("Освобождено 1", 50_000.0, rate="Exempt from Tax"),
    row_9001("Облагается 1", 105_000.0, rate="5%"),
]
vat_rows = [row_9003("Облагается 1", 5_000.0)]
variants = {
    "текст Exempt/5%": base_rows,
    "пустые ставки": [
        dict(base_rows[0], **{"Субконто Кт_2": ""}),
        dict(base_rows[1], **{"Субконто Кт_2": "не_указано"}),
    ],
    "мусор в ставке": [
        dict(base_rows[0], **{"Субконто Кт_2": "0,15 (левой текст)"}),
        dict(base_rows[1], **{"Субконто Кт_2": "1/3"}),
    ],
    "колонки ставки нет": [
        {k: v for k, v in base_rows[0].items() if k != "Субконто Кт_2"},
        {k: v for k, v in base_rows[1].items() if k != "Субконто Кт_2"},
    ],
}
results = []
for name, rows in variants.items():
    frame = _STEP._process_revenue_9001(
        pd.DataFrame(rows + vat_rows),
        osv(155_000.0, 5_000.0),
        context(),
    )
    per_doc = frame.set_index("Документ")["выручка_без_ндс_тыс_ед"].to_dict()
    results.append(tuple(
        round(per_doc[doc], 6) for doc in ("Освобождено 1", "Облагается 1")
    ))
check(len(set(results)) == 1,
      f"выручка не зависит от значения ставки (вариантов: {len(set(results))})")
check(abs(results[0][0] - 50.0) < 1e-9, "освобождённый документ: 50 000 без вычета НДС")
check(abs(results[0][1] - 100.0) < 1e-9, "облагаемый документ: 105 000 - 5 000 = 100 тыс.ед.")
check(warnings_text().strip() == "",
      f"WARNING'ов нет (текст: {warnings_text()!r})")

# ==========================================================================
# Тест 3: документ без НДС в 90.03 не делится на (1+ставка)
# ==========================================================================
print("\n--- Тест 3: НДС нет в 90.03 -> выручка валовая ---")
df_no_vat = _STEP._process_revenue_9001(
    pd.DataFrame([
        row_9001("Без НДС", 100_000.0, rate="Без НДС"),
        row_9001("НДС есть", 105_000.0),
    ] + [row_9003("НДС есть", 5_000.0)]),
    osv(205_000.0, 5_000.0),
    context(),
)
novat = df_no_vat.set_index("Документ")["выручка_без_ндс_тыс_ед"].to_dict()
check(abs(novat["Без НДС"] - 100.0) < 1e-9, "«Без НДС»: 100 000 остаются валовыми")
check(abs(novat["НДС есть"] - 100.0) < 1e-9, "соседний документ вычтен верно")

# ==========================================================================
# Тест 4: документ с несколькими строками — НДС вычитается один раз
# ==========================================================================
print("\n--- Тест 4: несколько строк 90.01 на документ ---")
df_multi = _STEP._process_revenue_9001(
    pd.DataFrame([
        row_9001("Двухстрочный", 60_000.0, group="Группа"),
        row_9001("Двухстрочный", 40_000.0, group="Группа"),
    ] + [row_9003("Двухстрочный", 10_000.0)]),
    osv(100_000.0, 10_000.0),
    context(),
)
total = float(df_multi["выручка_без_ндс_тыс_ед"].sum())
check(abs(total - 90.0) < 1e-9, "100 000 - 10 000 = 90 тыс.ед. (НДС не задвоен)")
check(
    len(df_multi) == 1,
    f"строки документа схлопнулись в одну по номенклатурной группе ({len(df_multi)})",
)

# НДС документа делится пропорционально обороту строк
vat_df = _STEP._extract_vat_9003(pd.DataFrame([row_9003("Двухстрочный", 10_000.0)]))
rows_multi = pd.DataFrame([
    {"Документ": "Двухстрочный", "Сумма": 60_000.0, "Сумма_руб": 60_000.0},
    {"Документ": "Двухстрочный", "Сумма": 40_000.0, "Сумма_руб": 40_000.0},
])
per_row = _STEP._subtract_vat_from_revenue(rows_multi, vat_df)[0]["ндс_тыс_ед"].tolist()
check(abs(per_row[0] - 6.0) < 1e-9, "строка на 60 % оборота получила 60 % НДС (6,0)")
check(abs(per_row[1] - 4.0) < 1e-9, "строка на 40 % оборота получила 40 % НДС (4,0)")

# ==========================================================================
# Тест 5: рублёвый эквивалент уменьшается на НДС в рублях
# ==========================================================================
print("\n--- Тест 5: НДС в рублях (курс документа) ---")
df_rub = _STEP._process_revenue_9001(
    pd.DataFrame([
        row_9001("Валютная", 105_000.0, rub=1_260_000.0),
    ] + [row_9003("Валютная", 5_000.0, rub=60_000.0)]),
    osv(105_000.0, 5_000.0),
    context(),
)
rub = float(df_rub["выручка_без_ндс_тыс_руб"].iloc[0])
ed = float(df_rub["выручка_без_ндс_тыс_ед"].iloc[0])
check(abs(ed - 100.0) < 1e-9, "в ед.: 105 000 - 5 000 = 100 тыс.ед.")
check(abs(rub - 1_200.0) < 1e-9, "в руб.: 1 260 000 - 60 000 = 1 200 тыс.руб.")

# ==========================================================================
# Тест 6: НДС 90.03 без пары в 90.01 -> WARNING + problem_data
# ==========================================================================
print("\n--- Тест 6: несопоставленный НДС ---")
reset_warnings()
try:
    _STEP._process_revenue_9001(
        pd.DataFrame([
            row_9001("Продажа 1", 105_000.0),
        ] + [
            row_9003("Продажа 1", 5_000.0),
            row_9003("Продажа без выручки", 30_000.0),
        ]),
        # ОСВ: выручки 105 000, НДС 35 000 -> расхождение 30 000 (его и даёт
        # несопоставленный НДС, который вычесть некуда). Допуск tightened,
        # иначе 30 тыс.ед. прошли бы молча.
        osv(105_000.0, 35_000.0),
        context(tolerance_reconciliation=1.0),
    )
    check(False, "ConvergenceError не брошен")
except ConvergenceError as e:
    check(True, "ConvergenceError на расхождении с ОСВ")
    check(
        "не сопоставлено ни с одним документом выручки 90.01" in warnings_text(),
        "WARNING о несопоставленном НДС",
    )
    check(
        "Продажа без выручки" in str(e.problem_data.to_dict("records")),
        "несопоставленный документ попал в problem_data",
    )
    check(
        e.problem_data["проверка"].notna().any(),
        "в problem_data осталась строка итога сверки",
    )

# ==========================================================================
# Тест 7: пустой 90.03 при нулевом обороте в ОСВ -> сходимость без WARNING
# ==========================================================================
print("\n--- Тест 7: оборотов по 90.03 нет ---")
reset_warnings()
df_empty = _STEP._process_revenue_9001(
    pd.DataFrame([row_9001("Без налога", 100_000.0, rate="0%")]),
    osv(100_000.0, 0.0),
    context(),
)
check(
    abs(float(df_empty["выручка_без_ндс_тыс_ед"].iloc[0]) - 100.0) < 1e-9,
    "выручка = валовая, сверка с ОСВ прошла",
)
check(
    "НДС не начислялся" in info_text(),
    "INFO «НДС не начислялся» (WARNING не поднимается)",
)
check("НДС 90.03: 0" not in warnings_text() and "не вычтен" not in warnings_text(),
      "WARNING о несопоставленном НДС не появился")

# ==========================================================================
# Тест 8: расхождение без несопоставленного НДС — problem_data из одной строки
# ==========================================================================
print("\n--- Тест 8: расхождение не из-за НДС ---")
try:
    _STEP._process_revenue_9001(
        pd.DataFrame([
            row_9001("Продажа 1", 105_000.0),
        ] + [row_9003("Продажа 1", 5_000.0)]),
        osv(105_000.0, 60_000.0),  # НДС в ОСВ больше, чем в выгрузке
        context(tolerance_reconciliation=1.0),
    )
    check(False, "ConvergenceError не брошен")
except ConvergenceError as e:
    check(
        len(e.problem_data) == 1,
        f"problem_data — одна строка итога сверки ({len(e.problem_data)})",
    )

# ==========================================================================
# Тест 9: строки 90.02/91.01/99 не попадают в НДС
# ==========================================================================
print("\n--- Тест 9: фильтр по счёту и файлу ---")
reset_warnings()
other_rows = pd.DataFrame([
    row_9001("Продажа 1", 105_000.0),
    {
        "Имя_файла": "smoke_отчпровод_90.03_test.txt",
        "Документ": "Продажа 1",
        "Дт": "62.02",          # не 90.03 — строка не про НДС
        "Кт": "90.03",
        "Сумма": 99_000.0,
        "Сумма_руб": 99_000.0,
    },
    {
        "Имя_файла": "smoke_отчпровод_91.01_test.txt",
        "Документ": "Продажа 1",
        "Дт": "90.03",           # счёт верный, но файл другой
        "Кт": "68.02",
        "Сумма": 88_000.0,
        "Сумма_руб": 88_000.0,
    },
    row_9003("Продажа 1", 5_000.0),
])
df_filtered = _STEP._process_revenue_9001(
    other_rows, osv(105_000.0, 5_000.0), context()
)
check(
    abs(float(df_filtered["выручка_без_ндс_тыс_ед"].iloc[0]) - 100.0) < 1e-9,
    "в НДС попали только строки Дт 90.03 из файла 90.03 (вычтено 5 000)",
)

# ==========================================================================
print("\n=== ИТОГ ===")
total = PASSED + len(failures)
label = "выручка 90.01 минус НДС 90.03 (шаг 14)"
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{total}) — {label}")
    for f in failures:
        print(f"  [FAIL] {f}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)
