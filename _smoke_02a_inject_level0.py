# -*- coding: utf-8 -*-
"""
Смоук-тест: синтетические счета шага 2а не разбавляют столбец счетов.

Регресс 06.10.2026 (ResourceShanghaiImport, ОСВ 8мес2026). В сводной ОСВ был
один файл с одной строкой (ОСВ по 76: level_0 = ['76.25'] — чистый столбец
счетов). Шаг 2а добавил в сводную строки синтетических счетов 84 и 99 из общей
ОСВ, но номер счёта записал в level_1..level_6, а level_0 оставил заглушке
'не_указано' (ветка `elif col not in new_row`). Уровень счетов разбавился:
level_0 = ['76.25', 'не_указано', 'не_указано'] — доля счетов 33% < порога 95%,
шаг 3 упал: «не найден столбец level_ с номерами бухгалтерских счетов».

Проверяет на синтетических кадрах (поведение воспроизводимо на любой машине):

  1. Счёт синтетического счёта попадает в level_0 (а не 'не_указано').
  2. После шага 2а в level_0 ВСЕ значения — счета, resolve_account_level_column
     находит 'level_0' (контракт шагов 1в/3).
  3. Прежнее поведение аналитики не сломано: в level_1/level_2 счёт дублируется,
     допсубконто = 'не_указано'.
  4. Сальдо синтетических строк считается из общей ОСВ без потерь.
  5. Пустой список accounts_from_general_osv — шаг проходит без изменений.

Запуск: conda run -n fl_acc_card python -u _smoke_02a_inject_level0.py
"""
import sys

import pandas as pd

from pipeline.base import ProcessingContext
from pipeline.steps.step_02a_inject_from_general_osv import (
    Step2aInjectFromGeneralOSVStep,
)
from utils.column_utils import is_accounting_code, resolve_account_level_column


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILED = 0
PASSED = 0
_failed_messages: list[str] = []


def check(condition: bool, message: str) -> None:
    """Мини-ассерт со счётчиками (смоук обязан падать при провале)."""
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print("[OK] " + message)
    else:
        FAILED += 1
        _failed_messages.append(message)
        print("[FAIL] " + message)


def make_summary() -> pd.DataFrame:
    """Сводная ОСВ «после шага 2» — одна строка 76.25 и два уровня аналитики."""
    return pd.DataFrame({
        'субконто': ['76.25'],
        'level_0': ['76.25'],
        'level_1': ['Resource Overseas General Trading L.L.C.'],
        'level_2': ['<Объект не найден> (158:80f10050569f4d4f11e65d662f84a9be)'],
        'допсубконто': ['Resource Overseas General Trading L.L.C.'],
        'сальдо, тыс.ед.': [123.45],
    }).astype('string')


def make_common_osv() -> pd.DataFrame:
    """Общая ОСВ с двумя синтетическими счетами (ненулевое сальдо)."""
    return pd.DataFrame({
        'Счет': ['84', '99'],
        'Дебет_оборот': [0.0, 0.0],
        'Кредит_оборот': [0.0, 0.0],
        'Дебет_конец': [1_000.0, 2_000.0],
        'Кредит_конец': [0.0, 0.0],
    })


def build_context(summary: pd.DataFrame, common_osv: pd.DataFrame,
                  accounts: list) -> ProcessingContext:
    return ProcessingContext(
        company='ResourceShanghaiImport',
        segment='Другое',
        period='8мес2026',
        currency='RUB',
        summary_osv_df=summary,
        common_osv_df=common_osv,
        data={'accounts_from_general_osv': accounts},
    )


# ============================================================================
# Тест 1: счёт синтетики в level_0, столбец счетов остаётся чистым
# ============================================================================
print("--- Тест 1: счёт синтетики попадает в level_0, столбец счетов цел ---")
ctx = build_context(make_summary(), make_common_osv(), ['84', '99'])
Step2aInjectFromGeneralOSVStep()._process(ctx)
res = ctx.summary_osv_df

check(res is not None and len(res) == 3,
      f"добавлены 2 строки синтетических счетов (факт: {len(res)})")
check(res['level_0'].tolist() == ['76.25', '84', '99'],
      f"level_0 = ['76.25', '84', '99'] — счёт синтетики на уровне счетов "
      f"(факт: {res['level_0'].tolist()})")
check(is_accounting_code(res['level_0']).all(),
      "level_0 целиком состоит из счетов (инвариант шагов 1в/3)")
chosen = resolve_account_level_column(res, column_prefix='level_')
check(chosen == 'level_0',
      f"resolve_account_level_column находит 'level_0' (факт: {chosen})")

# ============================================================================
# Тест 2: прежнее поведение аналитики не сломалось
# ============================================================================
print("\n--- Тест 2: аналитика синтетической строки (прежнее поведение) ---")
check(res['level_1'].tolist() ==
      ['Resource Overseas General Trading L.L.C.', '84', '99'],
      f"level_1: исходный контрагент + дубль счёта синтетики "
      f"(факт: {res['level_1'].tolist()})")
check(res['допсубконто'].tolist() ==
      ['Resource Overseas General Trading L.L.C.', 'не_указано', 'не_указано'],
      f"допсубконто синтетики = 'не_указано' "
      f"(факт: {res['допсубконто'].tolist()})")

# ============================================================================
# Тест 3: сальдо синтетики из общей ОСВ
# ============================================================================
print("\n--- Тест 3: сальдо синтетических счетов из общей ОСВ ---")
check(res['сальдо, тыс.ед.'].iloc[1:].tolist() == [1.0, 2.0],
      f"сальдо = [1.0, 2.0] "
      f"(факт: {res['сальдо, тыс.ед.'].iloc[1:].tolist()})")

# ============================================================================
# Тест 4: пустой список синтетических счетов — шаг без изменений
# ============================================================================
print("\n--- Тест 4: пустой accounts_from_general_osv — ранний выход ---")
ctx2 = build_context(make_summary(), make_common_osv(), [])
Step2aInjectFromGeneralOSVStep()._process(ctx2)
check(ctx2.summary_osv_df is not None and len(ctx2.summary_osv_df) == 1,
      f"сводная не меняется (факт: {len(ctx2.summary_osv_df)})")
check(ctx2.summary_osv_df['level_0'].tolist() == ['76.25'],
      "level_0 без изменений")

# ============================================================================
# Тест 5: контракт шага 3 — 'счет' из уровня счетов строится корректно
# ============================================================================
print("\n--- Тест 5: шаг 3 берёт 'счет' из level_0 ---")
res3 = res.copy()
res3['счет'] = res3['level_0']
check(res3['счет'].tolist() == ['76.25', '84', '99'],
      f"счет = ['76.25', '84', '99'] (факт: {res3['счет'].tolist()})")


# ============================================================================
print("\n=== ИТОГ ===")
total = PASSED + FAILED
label = "синтетические счета шага 2а не разбавляют столбец счетов (level_0)"
if FAILED:
    print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
    for line in _failed_messages:
        print(f"  [FAIL] {line}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)