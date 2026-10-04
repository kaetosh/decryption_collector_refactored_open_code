# -*- coding: utf-8 -*-
"""
Смоук-тест: нормализация журнала ОПУ в шаге 18 не зависит от наличия налога.

Регрессия 17.09.2026: нормализация журнала (вид_связи 'не_указано' → '3 лица'
и обрезка счёта до 5 символов) выполнялась внутри ветки `if new_rows_data:`,
т.е. только у компаний с ненулевыми оборотами по 99 счёту (налог на прибыль /
прочие движения). У компании без таких оборотов журнал оставался
ненормализованным, и шаг 19 падал с MissingMappingError:
ключ маппинга = (счет, вид_дохода_расхода, сегмент, вид_связи), а в справочнике
Меппинг_опу значения — '3 лица' и счета из 5 символов. Диагностический признак
в mismatches/: счёт '91.02.1' (не обрезан) и вид_связи='не_указано'.

Проверяет (данные синтетические, _INPUT_DATA не используется — поведение
воспроизводимо и одинаково на любой машине/версии pandas):

1. Нет 99-оборотов (регрессия): состав строк не меняется, но вид_связи
   'не_указано'/NA → '3 лица', счёт обрезан до 5 символов, object-колонок нет.
2. Есть налог (99↔68) и прочие движения: прежнее поведение сохранено — строки
   99.02/99.09 добавляются со своими счет_фо, нормализация применяется ко всему
   журналу.
3. Журнал с существующей колонкой 'счет_фо' (в т.ч. с NaN) не роняет шаг и не
   подменяет прежние значения.
4. Суммы оборотов сохраняются (в кейсе с налогом — плюс добавленные суммы).

Запуск: conda run -n fl_acc_card python -u _smoke_step18_normalization.py
"""
import sys

import pandas as pd
from loguru import logger

from pipeline.base import ProcessingContext
from pipeline.steps.step_18_add_task_and_other_movements import (
    Step18AddTaskAndOtherMovementsStep,
)

# При перенаправлении вывода в файл консоль Windows пишет в cp1251 —
# фиксируем UTF-8, чтобы кириллица и служебные символы не роняли print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_STEP = Step18AddTaskAndOtherMovementsStep()
failures: list = []
PASSED = 0

COMPANY = 'Тестовая Компания'
SEGMENT = 'Растениеводство'
REASSESSMENT_DR = 'Изменение в оценке'
REASSESSMENT_VIEW = 'Доходы (расходы) от чистой переоценки непроданного урожая СХ'
POSEVY_VIEW = 'Посевы изменение в оценке'


def check(condition: bool, message: str) -> None:
    """Мини-ассерт с накоплением результата."""
    global PASSED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def object_cols(df: pd.DataFrame) -> list:
    """Список столбцов с dtype 'object'."""
    return [col for col, dtype in df.dtypes.items() if dtype == object]


def make_companies_ref() -> pd.DataFrame:
    """Синтетический справочник КомпанииГруппы (нужен для сегмента)."""
    return pd.DataFrame({
        'сокращенное_наименование_компании': pd.Series([COMPANY], dtype='string'),
        'сегмент': pd.Series([SEGMENT], dtype='string'),
    })


def make_journal(with_schet_fo: bool = False) -> pd.DataFrame:
    """Журнал ОПУ в форме после шага 17.

    Строки 1-3 — переоценка (90.02 Дт/Кт 16 и 91.01/91.02 «Посевы...»),
    как в реальном отчёте: контрагента нет → вид_связи='не_указано'.
    Строка 4 — субсчёт 90.07.1 (проверка обрезки счёта до 5 символов).
    """
    df = pd.DataFrame({
        'контрагент': pd.Series(['не_указано', 'не_указано', 'КА 1', None], dtype='string'),
        'ном_группа': pd.Series(
            ['Пшеница озимая Урожай 2025', 'не_указано', 'Ном 1', 'Ном 2'],
            dtype='string',
        ),
        'счет': pd.Series(['90.02', '91.01', '91.02.1', '90.07.1'], dtype='string'),
        'доход_расход': pd.Series(
            [REASSESSMENT_DR, REASSESSMENT_VIEW, REASSESSMENT_VIEW, 'Управленческие расходы'],
            dtype='string',
        ),
        'вид_дохода_расхода': pd.Series(
            [POSEVY_VIEW, POSEVY_VIEW, POSEVY_VIEW, 'Управленческие расходы'],
            dtype='string',
        ),
        'сегмент': pd.Series([SEGMENT] * 4, dtype='string'),
        'группа_ка': pd.Series(['не_указано', 'не_указано', '3 лица', 'не_указано'], dtype='string'),
        'сегмент_ка': pd.Series(['не_указано', 'не_указано', SEGMENT, 'не_указано'], dtype='string'),
        'вид_связи': pd.Series(['не_указано', 'не_указано', '3 лица', None], dtype='string'),
        'объект для изм ппа': pd.Series(['не_указано'] * 4, dtype='string'),
        'рбп_кредитные_линии': pd.Series(['не_указано'] * 4, dtype='string'),
        'оборот, тыс.ед.': [100.0, -50.0, 30.0, 20.0],
        'оборот, тыс.руб.': [100.0, -50.0, 30.0, 20.0],
    })
    if with_schet_fo:
        df['счет_фо'] = pd.Series(['800000000', None, '800000001', '800000002'], dtype='string')
    return df


def make_transactions(with_tax: bool) -> pd.DataFrame:
    """Проводки: минимально необходимые шагу 18 колонки.

    Без налога — только 9099 (реформация/закрытие периода: из 99-файла
    ничего не остаётся). С налогом — добавляются 99.02↔68.04 (налог) и
    99.09↔76.05 (прочие движения).
    """
    rows = [{
        'Имя_файла': 'Компания_отчпровод_90.01_период_.txt',
        'Дт': '90.01.1',
        'Кт': '99.01.1',
        'Сумма': 1000.0,
        'Сумма_руб': 1000.0,
    }]
    if with_tax:
        rows.append({
            'Имя_файла': 'Компания_отчпровод_99_период_.txt',
            'Дт': '99.02',
            'Кт': '68.04',
            'Сумма': 700.0,
            'Сумма_руб': 700.0,
        })
        rows.append({
            'Имя_файла': 'Компания_отчпровод_99_период_.txt',
            'Дт': '99.09',
            'Кт': '76.05',
            'Сумма': 200.0,
            'Сумма_руб': 200.0,
        })
    return pd.DataFrame(rows)


def make_context(journal_df: pd.DataFrame, transactions_df: pd.DataFrame) -> ProcessingContext:
    """Контекст с минимальным набором данных для _process шага 18."""
    context = ProcessingContext(
        company=COMPANY,
        segment=SEGMENT,
        period='2025',
        type_period='год',
    )
    context.references['компании_группы'] = make_companies_ref()
    context.journal_df = journal_df
    context.data['transactions_all_df'] = transactions_df
    return context


logger.remove()

# ==========================================================================
# Тест 1: нет 99-оборотов — регрессия (нормализация обязана выполниться)
# ==========================================================================
print("\n--- Тест 1: нет 99-оборотов (регрессия: «не_указано» вместо «3 лица») ---")
ctx_no_tax = make_context(make_journal(with_schet_fo=False), make_transactions(with_tax=False))
_STEP._process(ctx_no_tax)
df_no_tax = ctx_no_tax.journal_df

check(len(df_no_tax) == 4,
      f"состав строк не изменился: 4 (факт: {len(df_no_tax)})")
check(set(df_no_tax['счет'].unique()) == {'90.02', '91.01', '91.02', '90.07'},
      f"счета обрезаны до 5 символов (факт: {sorted(df_no_tax['счет'].unique())})")
check((df_no_tax['вид_связи'] == '3 лица').all(),
      f"вид_связи у всех строк '3 лица' (факт: {sorted(df_no_tax['вид_связи'].unique())})")
check(not object_cols(df_no_tax),
      f"нет object-столбцов (факт: {object_cols(df_no_tax)})")
check(str(df_no_tax['вид_связи'].dtype) == 'string',
      f"dtype 'вид_связи' = 'string' (факт: {df_no_tax['вид_связи'].dtype})")
check(abs(df_no_tax['оборот, тыс.ед.'].sum() - 100.0) < 1e-9,
      f"сумма оборотов сохранена: 100.0 (факт: {df_no_tax['оборот, тыс.ед.'].sum():.2f})")
check('счет_фо' not in df_no_tax.columns,
      "налоговые строки не добавлены (колонка 'счет_фо' не появилась)")

# ==========================================================================
# Тест 2: есть налог и прочие движения — прежнее поведение сохранено
# ==========================================================================
print("\n--- Тест 2: есть налог (99↔68) и прочие движения — строки 99 добавляются ---")
ctx_tax = make_context(make_journal(with_schet_fo=False), make_transactions(with_tax=True))
_STEP._process(ctx_tax)
df_tax = ctx_tax.journal_df

check(len(df_tax) == 6,
      f"добавлены 2 строки налога/прочего: 6 (факт: {len(df_tax)})")
check(set(df_tax['счет'].unique()) == {'90.02', '91.01', '91.02', '90.07', '99.02', '99.09'},
      f"счета: 90.02/91.01/91.02/90.07/99.02/99.09 (факт: {sorted(df_tax['счет'].unique())})")
check(df_tax.loc[df_tax['счет'] == '99.02', 'счет_фо'].iloc[0] == '1300000000',
      "налог: счет_фо = 1300000000")
check(df_tax.loc[df_tax['счет'] == '99.09', 'счет_фо'].iloc[0] == '1300000100',
      "прочее: счет_фо = 1300000100")
check(abs(df_tax.loc[df_tax['счет'] == '99.02', 'оборот, тыс.ед.'].iloc[0] - 0.7) < 1e-9,
      "налог 700 руб. -> 0.7 тыс.ед.")
check(abs(df_tax.loc[df_tax['счет'] == '99.09', 'оборот, тыс.ед.'].iloc[0] - 0.2) < 1e-9,
      "прочее 200 руб. -> 0.2 тыс.ед.")
check((df_tax['вид_связи'] == '3 лица').all(),
      "вид_связи у всех строк (включая новые) '3 лица' "
      f"(факт: {sorted(df_tax['вид_связи'].unique())})")
check(not object_cols(df_tax),
      f"нет object-столбцов (факт: {object_cols(df_tax)})")
check(abs(df_tax['оборот, тыс.ед.'].sum() - 100.9) < 1e-9,
      f"сумма = журнал 100.0 + налог 0.7 + прочее 0.2 (факт: {df_tax['оборот, тыс.ед.'].sum():.2f})")

# ==========================================================================
# Тест 3: журнал с существующей колонкой 'счет_фо' (в т.ч. с NaN) — без падений
# ==========================================================================
print("\n--- Тест 3: журнал с колонкой 'счет_фо' (есть NaN) без 99-оборотов ---")
ctx_fo = make_context(make_journal(with_schet_fo=True), make_transactions(with_tax=False))
_STEP._process(ctx_fo)
df_fo = ctx_fo.journal_df

check(list(df_fo['счет_фо'].isna()) == [False, True, False, False],
      f"прежние значения счет_фо не подменяются (факт: {list(df_fo['счет_фо'].isna())})")
check(str(df_fo['счет_фо'].dtype) == 'string',
      f"dtype 'счет_фо' = 'string' (факт: {df_fo['счет_фо'].dtype})")
check((df_fo['вид_связи'] == '3 лица').all(),
      "вид_связи нормализована и в этом кейсе")

# ==========================================================================
# Тест 4: входной журнал в контексте не мутируется задним числом
# ==========================================================================
print("\n--- Тест 4: входной журнал не изменяется задним числом ---")
journal_src = make_journal(with_schet_fo=False)
ctx_src = make_context(journal_src, make_transactions(with_tax=False))
_STEP._process(ctx_src)

check(set(journal_src['счет'].unique()) == {'90.02', '91.01', '91.02.1', '90.07.1'},
      "исходный DataFrame сохранил счета с субсчетами "
      f"(факт: {sorted(journal_src['счет'].unique())})")
check(ctx_src.journal_df is not journal_src,
      "журнал в контексте — новый объект (нормализация не мутирует вход)")

# ==========================================================================
print("\n=== ИТОГ ===")
total = PASSED + len(failures)
label = "безусловная нормализация журнала ОПУ (шаг 18)"
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{total}) — {label}")
    for f in failures:
        print(f"  [FAIL] {f}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)