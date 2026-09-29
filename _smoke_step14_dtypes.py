# -*- coding: utf-8 -*-
"""
Смоук-тест: dtype-гигиена шага 14 (защита от object-столбцов в journal_df).

Регрессия 14.09.2026: при сиротах «3 лица» (кост группы без выручки) и
отсутствии переоценки (Дт 90.02 Кт 16) столбец 'контрагент' в journal_df
получал dtype 'object':
  1) pd.DataFrame({'контрагент': ['3 лица'] * n}) создавал object-столбец,
     и pd.concat(string, object) в pandas 2.x понижал итог до object;
  2) финальный каст text_cols в _merge_with_reassessment выполнялся только
     в ветке «переоценка есть» — ранний return её пропускал.

Проверяет (данные синтетические, _INPUT_DATA не используется — поведение
воспроизводимо и одинаково на любой машине/версии pandas):

1. _distribute_cost_to_buyers: 'контрагент'/'ном_группа' — 'string'; сирота
   не сгорает (строка '3 лица' с костом), сумма себестоимости сохраняется.
2. Полный конвейер шага 14 (reshape -> enrich -> merge) при ПУСТОЙ переоценке:
   в итоговой таблице нет object-столбцов (главный регрессионный чек).
3. То же с НЕпустой переоценкой: нет object-столбцов.
4. Sanity: счета 90.01/90.02, выручка инвертирована, себестоимость сходится.

Запуск: conda run -n fl_acc_card python -u _smoke_step14_dtypes.py
"""
import sys

import pandas as pd
from loguru import logger

from pipeline.steps.step_14_build_opu_foundation import Step14BuildOpuFoundationStep

_STEP = Step14BuildOpuFoundationStep()
failures: list = []


def check(condition: bool, message: str) -> None:
    """Мини-ассерт с накоплением результата."""
    if condition:
        print(f"[OK] {message}")
    else:
        failures.append(message)
        print(f"[FAIL] {message}")


def object_cols(df: pd.DataFrame) -> list:
    """Список столбцов с dtype 'object'."""
    return [col for col, dtype in df.dtypes.items() if dtype == object]


def make_inputs():
    """Синтетические df9001/df9002 (пост-группировочная форма шага 14).

    'Выбраковка' — сирота: есть себестоимость, нет ни одной строки выручки
    (реальный кейс «Куры-несушки_Выбраковка_Тур 002_2024» из TASKS.md).
    """
    df9001 = pd.DataFrame({
        'Документ': pd.Series(['Док 1', 'Док 1', 'Док 2'], dtype='string'),
        'контрагент': pd.Series(['КА 1', 'КА 2', 'КА 1'], dtype='string'),
        'ном_группа': pd.Series(['Яйцо', 'Яйцо', 'Помет'], dtype='string'),
        'выручка_без_ндс_тыс_ед': [100.0, 300.0, 50.0],
        'выручка_без_ндс_тыс_руб': [100.0, 300.0, 50.0],
    })
    df9002 = pd.DataFrame({
        'Документ': pd.Series(['Док 1', 'Док 2', 'Док С'], dtype='string'),
        'ном_группа': pd.Series(['Яйцо', 'Помет', 'Выбраковка'], dtype='string'),
        'себестоимость_тыс_ед': [80.0, 20.0, 15.0],
        'себестоимость_тыс_руб': [80.0, 20.0, 15.0],
    })
    return df9001, df9002


def make_references():
    """Синтетические справочники для _enrich_with_mappings (полное покрытие)."""
    mapping_opu_df = pd.DataFrame({
        'счет': ['90.01', '90.02'],
        'доход_расход': ['Выручка', 'Себестоимость'],
    })
    directory_ufr_df = pd.DataFrame({
        'счет': ['90.01', '90.01', '90.01', '90.02', '90.02', '90.02'],
        'ном_группа_1с': ['Яйцо', 'Помет', 'Выбраковка',
                          'Яйцо', 'Помет', 'Выбраковка'],
        'строка_уфр': ['стр. Яйцо', 'стр. Помет', 'стр. Выбраковка',
                       'стр. Яйцо', 'стр. Помет', 'стр. Выбраковка'],
        'сегмент': ['Птицеводство'] * 6,
    })
    group_companies_df = pd.DataFrame({
        'ВариантыНазвания': ['КА 1', 'КА 2'],
        'ВидСвязиКА': ['ГСК', 'ГСК'],
        'сегмент': ['Птицеводство', 'Птицеводство'],
    })
    return mapping_opu_df, directory_ufr_df, group_companies_df


def build_enriched():
    """Полная цепочка шага 14 на свежих копиях данных до merge с переоценкой."""
    df9001, df9002 = make_inputs()
    df_dist = _STEP._distribute_cost_to_buyers(df9001, df9002)
    df_long = _STEP._reshape_to_long_format(df_dist)
    mapping_opu_df, directory_ufr_df, group_companies_df = make_references()
    return _STEP._enrich_with_mappings(
        df_long, mapping_opu_df, directory_ufr_df, group_companies_df
    )


# ==========================================================================
# Тест 1: распределение себестоимости с сиротой — dtype и суммы
# ==========================================================================
print("\n--- Тест 1: _distribute_cost_to_buyers с сиротой «3 лица» ---")
df9001, df9002 = make_inputs()
df_dist = _STEP._distribute_cost_to_buyers(df9001, df9002)
check(str(df_dist['контрагент'].dtype) == 'string',
      f"dtype 'контрагент' после распределения = 'string' "
      f"(факт: {df_dist['контрагент'].dtype})")
check(str(df_dist['ном_группа'].dtype) == 'string',
      f"dtype 'ном_группа' после распределения = 'string' "
      f"(факт: {df_dist['ном_группа'].dtype})")
check(abs(df_dist['себестоимость_тыс_ед'].sum() - 115.0) < 1e-9,
      "себестоимость не сгорает: сумма = 115.0 (80 + 20 + 15 сироты)")
orphan_rows = df_dist[df_dist['контрагент'] == '3 лица']
check(len(orphan_rows) == 1
      and orphan_rows['ном_группа'].iloc[0] == 'Выбраковка'
      and abs(orphan_rows['себестоимость_тыс_ед'].iloc[0] - 15.0) < 1e-9,
      "сирота «Выбраковка» в отчёте с контрагентом '3 лица' и костом 15.0")

# ==========================================================================
# Тест 2: полный конвейер, переоценка ОТСУТСТВУЕТ (регрессионная ветка)
# ==========================================================================
print("\n--- Тест 2: merge с пустой переоценкой (ранний return) — нет object ---")
df_enriched_b = build_enriched()
df_final_b = _STEP._merge_with_reassessment(df_enriched_b, pd.DataFrame())
check(not object_cols(df_final_b),
      f"нет object-столбцов в journal_df (факт: {object_cols(df_final_b)})")
check(str(df_final_b['контрагент'].dtype) == 'string',
      f"dtype 'контрагент' = 'string' (факт: {df_final_b['контрагент'].dtype})")
check((df_final_b['контрагент'] == '3 лица').any(),
      "строка '3 лица' доезжает до финальной таблицы")

# ==========================================================================
# Тест 3: полный конвейер, переоценка ПРИСУТСТВУЕТ
# ==========================================================================
print("\n--- Тест 3: merge с непустой переоценкой — нет object ---")
df_enriched_c = build_enriched()
df9002_16 = pd.DataFrame({
    'ном_группа': pd.Series(['Переоценка'], dtype='string'),
    'оборот, тыс.ед.': [-30.0],
    'оборот, тыс.руб.': [-30.0],
    'счет': pd.Series(['90.02'], dtype='string'),
    'доход_расход': pd.Series(['Изменение в оценке'], dtype='string'),
    'сегмент': pd.Series(['Птицеводство'], dtype='string'),
    'вид_связи': pd.Series(['не_указано'], dtype='string'),
    'вид_дохода_расхода': pd.Series(['переоценка продукции'], dtype='string'),
})
df_final_c = _STEP._merge_with_reassessment(df_enriched_c, df9002_16)
check(not object_cols(df_final_c),
      f"нет object-столбцов в journal_df (факт: {object_cols(df_final_c)})")
check((df_final_c['вид_дохода_расхода'] == 'переоценка продукции').any(),
      "строка переоценки добавлена")

# ==========================================================================
# Тест 4: sanity чисел и типов
# ==========================================================================
print("\n--- Тест 4: sanity значений ---")
check(set(df_final_b['счет'].unique()) == {'90.01', '90.02'},
      "в расшифровке только счета 90.01/90.02")
revenue_9001 = df_final_b.loc[df_final_b['счет'] == '90.01',
                              'оборот, тыс.ед.'].sum()
cost_9002 = df_final_b.loc[df_final_b['счет'] == '90.02',
                           'оборот, тыс.ед.'].sum()
check(abs(revenue_9001 + 450.0) < 1e-9,
      f"выручка инвертирована: {revenue_9001:.2f} (ожидается -450.00)")
check(abs(cost_9002 - 115.0) < 1e-9,
      f"себестоимость в финале = {cost_9002:.2f} (ожидается 115.00)")

# ==========================================================================
logger.remove()
print("\n=== ИТОГ ===")
if failures:
    print(f"SMOKE_FAIL ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("SMOKE_OK")