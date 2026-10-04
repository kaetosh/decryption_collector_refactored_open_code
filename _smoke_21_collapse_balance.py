# -*- coding: utf-8 -*-
"""
Смоук-тест: Шаг 21 — сворачивание статей расшифровки баланса
(pipeline/steps/step_21_collapse_balance.py) по справочнику
«СтатьиБаланс_свернуто» без полного запуска пайплайна.

Сценарии:
1. Неттинг в актив:  ОНА 1000 + ОНО -300  -> одна строка ОНА 700.
2. Неттинг в пассив: ОНА 300 + ОНО -1000  -> одна строка ОНО -700.
3. Итог ≈ 0:         ОНА 500 + ОНО -500   -> обе строки удалены.
4. Только одна сторона в балансе (ОНО нулевое) -> строки не изменены.
5. ОНА «Отклонения в учете» (другой 2-й уровень) не затронуты.
6. Валютная компания: параллельная свертка «Значение_руб».
7. Пустой/отсутствующий справочник -> no-op.
8. Сумма «Значение» баланса не меняется; нет object-колонок; индекс чистый.

Запуск: conda run -n fl_acc_card python -u _smoke_21_collapse_balance.py
"""
import sys
from pathlib import Path

import pandas as pd
from loguru import logger

from io_module.output_manager import configure_run
from pipeline.base import ProcessingContext
from pipeline.steps.step_21_collapse_balance import Step21CollapseBalanceArticlesStep

# Инициализация папки вывода (нужна для mismatches/ отчёта шага)
configure_run()

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


# ==========================================================================
# Хелперы: сборка синтетического balance_df (структура как на выходе шага 13)
# ==========================================================================
L1, L2 = "1 уровень", "2 уровень (точка плана)"
L3, L4 = "3 уровень", "4 уровень: СВЯЗАННОСТЬ"
A_P, VALUE, RUB = "Актив/Пассив", "Значение", "Значение_руб"
RSBU, ACCOUNT, REPORT, ARTICLE = (
    "РСБУ Код отчетности", "Итоговый номер счета", "Отчетность", "Статья отчетности",
)

ONA_1, ONA_2 = "Отложенные налоговые активы", "Отложенные налоговые активы"
ONO_1, ONO_2 = "Отложенные налоговые обязательства", "Отложенные налоговые обязательства"
DEV_2 = "Отклонения в учете"
OS_1 = "Основные средства"


def make_balance(ona: float, ono: float, with_rub: bool = False) -> pd.DataFrame:
    """
    Синтетический баланс: ОНА (60010100), ОНО (270010103),
    ОНА «Отклонения в учете» (60020100) и несвязанная строка ОС (10010100).
    """
    rows = [
        (1150, 10010100, OS_1, OS_1, OS_1, "Собственность", "А",
         5000.0, "Баланс", "Основные средства"),
        (1180, 60010100, ONA_1, ONA_2, ONA_2, "Собственность", "А",
         ona, "Баланс", "Отложенные налоговые активы"),
        (1180, 60020100, ONA_1, DEV_2, "Высокий риск", "Собственность", "А",
         250.0, "Баланс", "Отложенные налоговые активы"),
        (1420, 270010103, ONO_1, ONO_2, ONO_2, "3 лица", "П",
         ono, "Баланс", "Отложенные налоговые обязательства"),
    ]
    df = pd.DataFrame(
        rows,
        columns=[RSBU, ACCOUNT, L1, L2, L3, L4, A_P, VALUE, REPORT, ARTICLE],
    ).set_index(ACCOUNT)
    if with_rub:
        # Валютная компания: рублевый эквивалент (курс ~80, для простоты *80)
        df[RUB] = (df[VALUE] * 80).round(2)
    return df


def make_reference() -> pd.DataFrame:
    """Содержимое листа «СтатьиБаланс_свернуто» Справочники.xlsx."""
    return pd.DataFrame({
        "номер_группы_сворачивания": [1, 1],
        L1: [ONA_1, ONO_1],
        L2: [ONA_2, ONO_2],
    })


def run_step(balance_df: pd.DataFrame, reference, currency: str = "RUB"):
    """Прогоняет шаг 21 через Step.execute() (с поствалидацией)."""
    context = ProcessingContext(
        company="СМПК", period="6мес2026", currency=currency,
    )
    context.balance_df = balance_df
    if reference is not None:
        context.references["статьи_баланс_свернуто"] = reference
    step = Step21CollapseBalanceArticlesStep()
    context = step.execute(context)
    return context.balance_df


def df_shape_report(df: pd.DataFrame, label: str) -> None:
    print(f"    {label}: строк={len(df)}, индекс={list(df.index)}")


# ==========================================================================
# Тест 1: неттинг в актив
# ==========================================================================
print("\n--- Тест 1: ОНА 1000 + ОНО -300 -> одна строка ОНА 700 ---")
df = run_step(make_balance(ona=1000.0, ono=-300.0), make_reference())
df_shape_report(df, "итог")
check(60010100 in df.index and 270010103 not in df.index,
      "Строка ОНА осталась, строка ОНО удалена")
check(abs(df.loc[60010100, VALUE] - 700.0) < 1e-6,
      "Значение ОНА = 700 (сальдированный итог)")
check(df.loc[60010100, A_P] == "А", "Направление результата: А")
check(df.loc[60010100, RSBU] == 1180, "РСБУ код результата = 1180")
check(60020100 in df.index and abs(df.loc[60020100, VALUE] - 250.0) < 1e-6,
      "ОНА «Отклонения в учете» не затронуты")

# ==========================================================================
# Тест 2: неттинг в пассив
# ==========================================================================
print("\n--- Тест 2: ОНА 300 + ОНО -1000 -> одна строка ОНО -700 ---")
df = run_step(make_balance(ona=300.0, ono=-1000.0), make_reference())
df_shape_report(df, "итог")
check(270010103 in df.index and 60010100 not in df.index,
      "Строка ОНО осталась, строка ОНА удалена")
check(abs(df.loc[270010103, VALUE] + 700.0) < 1e-6,
      "Значение ОНО = -700 (сальдированный итог)")
check(df.loc[270010103, A_P] == "П", "Направление результата: П")
check(df.loc[270010103, RSBU] == 1420, "РСБУ код результата = 1420")

# ==========================================================================
# Тест 3: итог ≈ 0 — обе строки удалены
# ==========================================================================
print("\n--- Тест 3: ОНА 500 + ОНО -500 -> обе строки удалены ---")
df = run_step(make_balance(ona=500.0, ono=-500.0), make_reference())
df_shape_report(df, "итог")
check(60010100 not in df.index and 270010103 not in df.index,
      "Обе строки ОНА/ОНО удалены")
check(60020100 in df.index and 10010100 in df.index,
      "Незатронутые строки остались")

# ==========================================================================
# Тест 4: только одна сторона в балансе — строки не изменены
# ==========================================================================
print("\n--- Тест 4: только ОНА 800 (ОНО нулевое) -> без изменений ---")
df = run_step(make_balance(ona=800.0, ono=0.0), make_reference())
df_shape_report(df, "итог")
check(60010100 in df.index and abs(df.loc[60010100, VALUE] - 800.0) < 1e-6,
      "ОНА осталась без изменений (сворачивать нечего)")

# ==========================================================================
# Тест 5: пустой справочник / отсутствие справочника -> no-op
# ==========================================================================
print("\n--- Тест 5: пустой справочник -> no-op ---")
df_before = make_balance(ona=1000.0, ono=-300.0)
df = run_step(df_before.copy(), make_reference().iloc[0:0])
check(df.equals(df_before), "Пустой справочник: баланс не изменён")
df = run_step(df_before.copy(), None)
check(df.equals(df_before), "Отсутствующий справочник: баланс не изменён")

# ==========================================================================
# Тест 6: валютная компания — параллельная свертка «Значение_руб»
# ==========================================================================
print("\n--- Тест 6: валютная компания (Значение_руб) ---")
df = run_step(
    make_balance(ona=1000.0, ono=-300.0, with_rub=True),
    make_reference(),
    currency="USD",
)
df_shape_report(df, "итог")
check(abs(df.loc[60010100, VALUE] - 700.0) < 1e-6, "Значение = 700")
check(abs(df.loc[60010100, RUB] - 700.0 * 80) < 1e-6,
      "Значение_руб = 56000 (параллельная свертка)")

# ==========================================================================
# Тест 7: сходимость суммы и чистота типов
# ==========================================================================
print("\n--- Тест 7: инварианты (сумма, dtypes, индекс) ---")
df_before = make_balance(ona=1000.0, ono=-300.0)
df = run_step(df_before.copy(), make_reference())
check(abs(float(df[VALUE].sum()) - float(df_before[VALUE].sum())) < 1e-6,
      "Сумма «Значение» баланса не изменилась")
check(not any(dtype == object for dtype in df.dtypes),
      "Нет object-колонок")
check(str(df.index.dtype) in ("int64", "Int64"),
      f"Индекс «{ACCOUNT}» числовой ({df.index.dtype})")
check(not df.index.duplicated().any(), "Индекс без дубликатов")
check(str(df[RSBU].dtype) in ("int64", "Int64", "Float64"),
      f"РСБУ код числовой ({df[RSBU].dtype})")

# ==========================================================================
# Итог
# ==========================================================================
print("\n" + "=" * 70)
total = PASSED + len(failures)
label = "свёртывание статей баланса (шаг 21)"
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{total}) — {label}")
    for f in failures:
        print(f"  [FAIL] {f}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)

