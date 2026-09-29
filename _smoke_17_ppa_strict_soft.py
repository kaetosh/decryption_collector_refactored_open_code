# -*- coding: utf-8 -*-
"""
Смоук-тест для STRICT_PPA_MAPPING_CHECK (шаг 17, меппинг ППА).

Сценарии:
1. Строгий режим (по умолчанию) — неполный меппинг → MissingMappingError
2. Мягкий режим — неполный меппинг → WARNING + замена на '3 лица'
3. Мягкий режим, колонка отсутствует → WARNING + замена на '3 лица'
4. Happy path — оба режима, ошибки нет

Запуск: python -u _smoke_17_ppa_strict_soft.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

# Добавляем корень проекта в sys.path
sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import Step
from pipeline.errors import MissingMappingError
from pipeline.step_config import StepConstants
from pipeline.steps._step17_processing import Step17ProcessingMixin


class MockStep(Step17ProcessingMixin):
    """Мок-класс для тестирования миксина без полной инициализации Step."""

    PPA_ACCOUNTS = ('01.09', '02.01', '01.01')
    NDS_ACCOUNTS = ('68.02',)

    def __init__(self):
        # Не вызываем super().__init__() — нам нужен только миксин
        self.last_error = None
        pass

    def _save_reference_mismatch_report(self, error):
        """Мок: не сохраняем файл, только логируем и запоминаем ошибку."""
        self.last_error = error
        print(f"  [mock] Отчёт сохранён: {error.reference_name}")

    def hint_companies_in_reference(self, reference_df, company_column):
        """Мок: возвращаем пустую подсказку."""
        return ""

    def make_missing_values_problem_data(self, missing_by_type, company, **kwargs):
        """Считаем реальный формат отчёта (без сохранения файла)."""
        return Step.make_missing_values_problem_data(missing_by_type, company, **kwargs)


def make_reference_ppa(include_os_ppa=True, include_os_transfer=True, fill_ppa=True, fill_transfer=True):
    """Создаёт тестовый справочник ППА."""
    data = {
        'наименование_компании': ['Компания_А'] * 4,
        'ос_ппа': ['Объект_1', 'Объект_2', None, None] if include_os_ppa else None,
        'ос_после_перехода_в_собственность': ['Объект_3', None, 'Объект_4', None] if include_os_transfer else None,
        'контрагент': ['Контрагент_1', 'Контрагент_2', 'Контрагент_3', 'Контрагент_4'],
    }
    if not include_os_ppa:
        del data['ос_ппа']
    if not include_os_transfer:
        del data['ос_после_перехода_в_собственность']

    df = pd.DataFrame(data)

    if not fill_ppa and include_os_ppa:
        df['ос_ппа'] = None
    if not fill_transfer and include_os_transfer:
        df['ос_после_перехода_в_собственность'] = None

    return df


def make_9101_with_ppa():
    """Создаёт тестовый df_9101 с ППА-проводками."""
    return pd.DataFrame({
        'вид_дохода_расхода': [
            'Доходы от выбытия прав пользования активами, изменения условий договоров аренды',
            'Доходы от выбытия прав пользования активами, изменения условий договоров аренды',
            'Доходы от выбытия прав пользования активами, изменения условий договоров аренды',
            'Прочие доходы',
        ],
        'Корр.счет': ['01.09', '01.09', '02.01', '01.09'],
        'Субконто Дт_1': ['Объект_1', 'Объект_99', 'Объект_3', 'Объект_1'],
        'контрагент': [None, None, None, None],
        'сегмент': ['Сегмент_А'] * 4,
        'оборот, тыс.ед.': [100.0, 200.0, 300.0, 400.0],
        'оборот, тыс.руб.': [100.0, 200.0, 300.0, 400.0],
    }).astype({'вид_дохода_расхода': 'string', 'Корр.счет': 'string',
                'Субконто Дт_1': 'string', 'контрагент': 'string',
                'сегмент': 'string'})


def make_9102_with_ppa():
    """Создаёт тестовый df_9102 с ППА-проводками."""
    return pd.DataFrame({
        'вид_дохода_расхода': [
            'Расходы от выбытия прав пользования активами, изменений условий договоров аренды',
            'Расходы от выбытия прав пользования активами, изменений условий договоров аренды',
            'Прочие расходы',
        ],
        'Корр.счет': ['01.09', '01.01', '01.09'],
        'Субконто Кт_1': ['Объект_2', 'Объект_4', 'Объект_99'],
        'контрагент': [None, None, None],
        'сегмент': ['Сегмент_А'] * 3,
        'оборот, тыс.ед.': [150.0, 250.0, 350.0],
        'оборот, тыс.руб.': [150.0, 250.0, 350.0],
    }).astype({'вид_дохода_расхода': 'string', 'Корр.счет': 'string',
                'Субконто Кт_1': 'string', 'контрагент': 'string',
                'сегмент': 'string'})


def _check_report_has_sums(problem_data):
    """Проверяет, что отчёт содержит агрегат сумм по недостающим объектам ОС."""
    if problem_data is None or problem_data.empty:
        print("  FAIL: problem_data пуст")
        return False
    cols = problem_data.columns.tolist()
    for required_col in ('отсутствующее_значение', 'тип', 'компания', 'количество_строк', 'оборот, тыс.ед.'):
        if required_col not in cols:
            print(f"  FAIL: в problem_data нет колонки '{required_col}' ({cols})")
            return False
    obj_row = problem_data[problem_data['отсутствующее_значение'] == 'Объект_99']
    if obj_row.empty:
        print("  FAIL: нет строки 'Объект_99' в problem_data")
        return False
    expected = 200.0  # одна строка-источник: 01.09/Объект_99 в df_9101, оборот 200.0
    actual = obj_row.iloc[0]['оборот, тыс.ед.']
    if abs(actual - expected) > 1e-9:
        print(f"  FAIL: оборот Объект_99 ожидался {expected}, получено {actual}")
        return False
    return True


def test_strict_mode_raises():
    """Сценарий 1: строгий режим — MissingMappingError."""
    print("\\n=== Сценарий 1: Строгий режим (STRICT_PPA_MAPPING_CHECK=True) ===")
    step = MockStep()
    ref_ppa = make_reference_ppa()
    df_9101 = make_9101_with_ppa()
    df_9102 = make_9102_with_ppa()

    with patch('pipeline.steps._step17_processing.STRICT_PPA_MAPPING_CHECK', True):
        try:
            step._process_ppa(df_9101, df_9102, ref_ppa, 'Компания_А')
            print("  FAIL: Ожидалась MissingMappingError, но ошибки не было")
            return False
        except MissingMappingError as e:
            print(f"  OK: MissingMappingError — {str(e)[:80]}...")
            if not _check_report_has_sums(e.problem_data):
                return False
            print("  OK: problem_data содержит суммы оборотов по недостающим объектам ОС")
            return True


def test_soft_mode_replaces():
    """Сценарий 2: мягкий режим — замена на '3 лица'."""
    print("\\n=== Сценарий 2: Мягкий режим (STRICT_PPA_MAPPING_CHECK=False) ===")
    step = MockStep()
    ref_ppa = make_reference_ppa()
    df_9101 = make_9101_with_ppa()
    df_9102 = make_9102_with_ppa()

    with patch('pipeline.steps._step17_processing.STRICT_PPA_MAPPING_CHECK', False):
        df_9101_result, df_9102_result = step._process_ppa(df_9101, df_9102, ref_ppa, 'Компания_А')

    # Проверяем, что несмапленные объекты получили '3 лица'
    obj_99_rows = df_9101_result[df_9101_result['Субконто Дт_1'] == 'Объект_99']
    if obj_99_rows.empty:
        print("  FAIL: Не найдена строка с Объект_99")
        return False

    contractor = obj_99_rows.iloc[0]['контрагент']
    if contractor != '3 лица':
        print(f"  FAIL: Ожидалось '3 лица', получено '{contractor}'")
        return False

    # Проверяем, что отчёт в мягком режиме тоже содержит суммы оборотов
    if step.last_error is None:
        print("  FAIL: отчёт не сохранён в мягком режиме (last_error is None)")
        return False
    if not _check_report_has_sums(step.last_error.problem_data):
        return False

    print(f"  OK: Объект_99 заменён на '{contractor}'")

    # Проверяем, что смапленные объекты получили правильных контрагентов
    obj_1_rows = df_9101_result[df_9101_result['Субконто Дт_1'] == 'Объект_1']
    contractor_1 = obj_1_rows.iloc[0]['контрагент']
    if contractor_1 != 'Контрагент_1':
        print(f"  FAIL: Объект_1 ожидался 'Контрагент_1', получено '{contractor_1}'")
        return False

    print(f"  OK: Объект_1 смаплен на '{contractor_1}'")
    return True


def test_soft_mode_missing_column():
    """Сценарий 3: мягкий режим, колонка ос_ппа отсутствует."""
    print("\\n=== Сценарий 3: Мягкий режим, колонка ос_ппа отсутствует ===")
    step = MockStep()
    ref_ppa = make_reference_ppa(include_os_ppa=False)
    df_9101 = make_9101_with_ppa()
    df_9102 = make_9102_with_ppa()

    with patch('pipeline.steps._step17_processing.STRICT_PPA_MAPPING_CHECK', False):
        df_9101_result, df_9102_result = step._process_ppa(df_9101, df_9102, ref_ppa, 'Компания_А')

    # Все объекты по маске 01.09 должны быть заменены на '3 лица'
    mask_01_09 = df_9101_result['Корр.счет'] == '01.09'
    ppa_income_mask = df_9101_result['вид_дохода_расхода'].str.contains('выбытия прав пользования', na=False)
    ppa_rows = df_9101_result[mask_01_09 & ppa_income_mask]

    if ppa_rows.empty:
        print("  FAIL: Не найдены строки ППА")
        return False

    all_third_party = (ppa_rows['контрагент'] == '3 лица').all()
    if not all_third_party:
        print(f"  FAIL: Не все строки получили '3 лица': {ppa_rows['контрагент'].tolist()}")
        return False

    print(f"  OK: Все {len(ppa_rows)} строк ППА заменены на '3 лица'")
    return True


def test_happy_path():
    """Сценарий 4: happy path — все объекты смаплены."""
    print("\\n=== Сценарий 4: Happy path (все объекты в ППА) ===")
    step = MockStep()

    # Создаём справочник со всеми объектами
    # Важно: каждый объект ОС должен быть в своей строке с правильным контрагентом
    ref_ppa = pd.DataFrame({
        'наименование_компании': ['Компания_А'] * 4,
        'ос_ппа': ['Объект_1', 'Объект_2', None, None],
        'ос_после_перехода_в_собственность': [None, None, 'Объект_3', 'Объект_4'],
        'контрагент': ['Контрагент_1', 'Контрагент_2', 'Контрагент_3', 'Контрагент_4'],
    }).astype({'наименование_компании': 'string', 'ос_ппа': 'string',
                'ос_после_перехода_в_собственность': 'string', 'контрагент': 'string'})

    df_9101 = pd.DataFrame({
        'вид_дохода_расхода': [
            'Доходы от выбытия прав пользования активами, изменения условий договоров аренды',
            'Доходы от выбытия прав пользования активами, изменения условий договоров аренды',
        ],
        'Корр.счет': ['01.09', '02.01'],
        'Субконто Дт_1': ['Объект_1', 'Объект_3'],
        'контрагент': [None, None],
        'сегмент': ['Сегмент_А'] * 2,
        'оборот, тыс.ед.': [100.0, 300.0],
        'оборот, тыс.руб.': [100.0, 300.0],
    }).astype({'вид_дохода_расхода': 'string', 'Корр.счет': 'string',
                'Субконто Дт_1': 'string', 'контрагент': 'string',
                'сегмент': 'string'})

    df_9102 = pd.DataFrame({
        'вид_дохода_расхода': [
            'Расходы от выбытия прав пользования активами, изменений условий договоров аренды',
        ],
        'Корр.счет': ['01.09'],
        'Субконто Кт_1': ['Объект_2'],
        'контрагент': [None],
        'сегмент': ['Сегмент_А'],
        'оборот, тыс.ед.': [150.0],
        'оборот, тыс.руб.': [150.0],
    }).astype({'вид_дохода_расхода': 'string', 'Корр.счет': 'string',
                'Субконто Кт_1': 'string', 'контрагент': 'string',
                'сегмент': 'string'})

    # Строгий режим
    with patch('pipeline.steps._step17_processing.STRICT_PPA_MAPPING_CHECK', True):
        df_9101_result, df_9102_result = step._process_ppa(df_9101.copy(), df_9102.copy(), ref_ppa, 'Компания_А')

    # Все контрагенты должны быть подтянуты
    expected_9101 = ['Контрагент_1', 'Контрагент_3']
    actual_9101 = df_9101_result['контрагент'].tolist()
    if actual_9101 != expected_9101:
        print(f"  FAIL: df_9101 ожидалось {expected_9101}, получено {actual_9101}")
        return False

    expected_9102 = ['Контрагент_2']
    actual_9102 = df_9102_result['контрагент'].tolist()
    if actual_9102 != expected_9102:
        print(f"  FAIL: df_9102 ожидалось {expected_9102}, получено {actual_9102}")
        return False

    print(f"  OK: Все контрагенты подтянуты корректно")
    return True


if __name__ == '__main__':
    print("=" * 60)
    print("Смоук-тест: STRICT_PPA_MAPPING_CHECK (шаг 17, ППА)")
    print("=" * 60)

    results = []
    results.append(("Строгий режим", test_strict_mode_raises()))
    results.append(("Мягкий режим", test_soft_mode_replaces()))
    results.append(("Мягкий режим (нет колонки)", test_soft_mode_missing_column()))
    results.append(("Happy path", test_happy_path()))

    print("\\n" + "=" * 60)
    print("ИТОГО:")
    all_passed = True
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}: {name}")
        if not passed:
            all_passed = False

    if all_passed:
        print("\\nSMOKE_OK")
    else:
        print("\\nSMOKE_FAIL")
        sys.exit(1)
