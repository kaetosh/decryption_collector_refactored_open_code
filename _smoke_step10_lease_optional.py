# -*- coding: utf-8 -*-
'''Смоук-тест: Шаг 10 — ведомость амортизации нужна только при наличии
арендованных ОС (pipeline/steps/step_10_classify_lease.py).

Проблема: файл читался ПЕРВЫМ, до вопроса «есть ли вообще аренда».
Ведомость амортизации выгружается из 1С только по арендованным объектам,
поэтому у компании без аренды её не существует — но требование на уровне
логики шага было безусловным, и обе стороны ломались:
  * ненужный БИТЫЙ файл (у компании без аренды) ронял конвейер — реальный
    прогон ResourceShanghaiImport 04.10.2026, IndexError на строке-шапке;
  * нужный ОТСУТСТВУЮЩИЙ файл терялся молча: шаг возвращал context, остатки
    по 01.03/02.03 оставались неразбитыми по группам ОС и видам связи,
    логировался только DEBUG, и на титульном листе отчёт выглядел зелёным.

Теперь решение принимает сама ОСВ (_detect_leased_os), а файл читается
только если остатки по счетам арендованных ОС есть.

Единицы: сальдо в сводной ОСВ — ТЫСЯЧИ (как в колонке «сальдо, тыс.ед.»),
а в ведомости амортизации — рубли (они делятся на 1000 в
_prepare_depreciation_data). Допуск tolerance_leased_os = 3000 тыс.ед.,
поэтому тестовые остатки взяты заведомо выше порога.

Сценарии:
 1. _detect_leased_os: остаток выше допуска -> has_lease=True.
 2. _detect_leased_os: остаток в пределах допуска (округление) -> False.
 3. _detect_leased_os: нулевой остаток при наличии строк -> False.
 4. _detect_leased_os: строк нет вовсе -> False (регресс ResourceShanghaiImport).
 5. _detect_leased_os: отрицательная сумма учитывается по модулю.
 6. _process: аренды нет, ведомость НЕ выгружена -> шаг отрабатывает, ОСВ цела.
 7. _process: аренды нет, ведомость выгружена БИТАЯ -> шаг не падает
    (регресс прогона 04.10.2026: файл не нужен, его форма не важна).
 8. _process: аренда есть, ведомость НЕ выгружена -> InputDataError с
    указанием счетов, числа строк, суммы и ожидаемого имени файла.
 9. _process: аренда есть, ведомость битая -> InputDataError с понятным
    текстом про двухстрочную шапку (а не IndexError).
10. _process: аренда есть, ведомость корректная -> штатный путь, вид_связи
    проставлен, сходимость сверена.
11. _process: аренда есть, ведомость есть, но расхождение больше допуска ->
    ConvergenceError (проверка сходимости не потеряна).

Запуск: python -u _smoke_step10_lease_optional.py
'''
import shutil
import tempfile
from pathlib import Path

import pandas as pd

import pipeline.steps.step_10_classify_lease as step10_module
from pipeline.errors import ConvergenceError, InputDataError
from pipeline.steps.step_10_classify_lease import Step10ClassifyLeaseSourceStep

failures: list = []
counters = {'passed': 0, 'total': 0}

TYPE_REGISTER = 'ведамор'
COMPANY = 'ТестКомпания'
PERIOD = '8мес2026'
TOLERANCE = 3000

# ОСВ (тыс.ед.) и ведомость (рубы) — согласованы между собой с точностью
# до допуска: сумма ОСВ 4000 тыс.ед. против 6000 тыс.ед. из ведомости.
OSV_COST = 5000.0        # тыс.ед. на 01.03
OSV_AMORT = -1000.0      # тыс.ед. на 02.03
STMT_COST = 5_000_000.0  # руб. — «Стоимость на конец периода»
STMT_AMORT = -1_000_000.0  # руб. — «Амортизация на конец периода»

# Файлы ведомости кладём во временную папку и подменяем SPECIAL_REPORTS_DIR —
# реальные _INPUT_DATA прогон не трогает. Очистка между сценариями обязательна:
# find_register_file возвращает ПЕРВЫЙ подходящий файл, и старый битый файл
# иначе утёк бы в сценарий «нужен корректный».
_smoke_tmp = Path(tempfile.mkdtemp(prefix='smoke_step10_'))
step10_module.SPECIAL_REPORTS_DIR = _smoke_tmp


class _NoopSaver:
    '''Заглушка DataSaver: шаг 10 сохраняет разобранную ведомость в
    _OUTPUT_DATA, а смоук не должен создавать папки запусков. Пишем в никуда,
    проверяя при этом только логику шага.'''

    @staticmethod
    def save_to_excel(*args, **kwargs):
        return None


step10_module.DataSaver = _NoopSaver


def reset_tmp():
    '''Чистит временную папку выгрузок перед очередным сценарием.'''
    for item in _smoke_tmp.glob('*.xlsx'):
        item.unlink()


def check(condition: bool, message: str) -> None:
    '''Мини-ассерт с накоплением результата.'''
    counters['total'] += 1
    if condition:
        counters['passed'] += 1
        print('[OK] ' + message)
    else:
        failures.append(message)
        print('[FAIL] ' + message)


def make_context():
    '''Контекст с минимальным набором: сводная ОСВ, компания, период, допуски.'''
    context = type('Ctx', (), {})()
    context.company = COMPANY
    context.period = PERIOD
    context.summary_osv_df = None
    context.data = {}
    # Пустой, но корректно оформленный справочник: маппинг получается пустым,
    # поэтому любой контрагент из ведомости трактуется как «3 лица».
    context.references = {
        'вид_связи_ка': pd.DataFrame({
            'ВариантыНазвания': pd.Series(dtype='object'),
            'ВидСвязиКА': pd.Series(dtype='object'),
        })
    }
    context.tolerance_params = {'tolerance_leased_os': TOLERANCE}
    return context


def osv(rows):
    '''Сводная ОСВ из (счет, сальдо_тыс_ед) — с нужными для шага колонками.'''
    return pd.DataFrame({
        'счет': [r[0] for r in rows],
        'сальдо, тыс.ед.': [float(r[1]) for r in rows],
    })


def write_statement(path, cost=STMT_COST, amort=STMT_AMORT,
                    contractor='ООО Арендодатель', os_group='Здания'):
    '''Валидная двухстрочная шапка + строка данных + итоговая строка.

    Верхняя строка задаёт иерархию денежных блоков, нижняя — подзаголовки;
    одиночные реквизиты (группа, договор, контрагент, вид взаиморасчётов)
    стоят только в верхней, иначе комбинатор склеил бы их с заголовком
    предыдущей колонки.
    '''
    top = ['Основное средство', 'Группа учета ОС', 'Договор аренды',
           'Контрагент', 'Вид взаиморасчетов', 'Стоимость', None, None,
           None, None, 'Амортизация', None, None]
    bottom = [None, None, None, None, None, 'первоначальная',
              'на начало периода', 'изменение', 'на конец периода',
              'остаточная', 'на начало периода', 'за период',
              'на конец периода']
    # Данные выровнены по 13 столбцам в том же порядке, что и шапка:
    # реквизиты (объект, группа, договор, контрагент, вид взаиморасчётов),
    # затем 5 колонок блока «Стоимость» и 3 колонки блока «Амортизация».
    data = ['Оборудование 1', os_group, 'Договор №1/26', contractor, 'Аренда',
            1_000_000.0, 1_000_000.0, 0.0, cost, 900_000.0,
            100_000.0, 100_000.0, amort]
    total = ['Итог', os_group, '', '', '', 1_000_000.0, 1_000_000.0, 0.0,
             cost, 900_000.0, 100_000.0, 100_000.0, amort]

    frame = pd.DataFrame([
        ['Ведомость амортизации ОС'] + [None] * 12,
        [None] * 13,
        top,
        bottom,
        data,
        total,
    ])
    frame.to_excel(path, sheet_name='Ведомость', header=False, index=False)
    return path


def make_broken_statement(path):
    '''Файл, где 'Основное средство' — последняя строка (регресс 04.10.2026).'''
    frame = pd.DataFrame([
        ['Ведомость амортизации ОС', None, None],
        ['Период: 8мес2026', None, None],
        [None, None, None],
        [None, None, None],
        [None, None, None],
        [None, None, None],
        ['Основное средство', 'Стоимость', 'Амортизация'],
    ])
    frame.to_excel(path, sheet_name='Ведомость', header=False, index=False)
    return path


def run_process(rows):
    '''Прогоняет _process; возвращает ('ok'|'err', exc, context).'''
    context = make_context()
    context.summary_osv_df = osv(rows)
    try:
        step._process(context)
        return 'ok', None, context
    except Exception as exc:  # noqa: BLE001 — смоук фиксирует любой исход
        return 'err', exc, context


step = Step10ClassifyLeaseSourceStep()
stmt_path = _smoke_tmp / f'{COMPANY}_{TYPE_REGISTER}_0102_{PERIOD}_.xlsx'


# ======================================================================
# 1-5. _detect_leased_os: решение принимает остаток в ОСВ
# ======================================================================
info = step._detect_leased_os(osv([('01.03', 5000.0)]), make_context())
check(info['has_lease'] is True and info['rows'] == 1 and info['sum'] == 5000.0,
      'Остаток 01.03 выше допуска -> ведомость нужна (has_lease=True)')

info = step._detect_leased_os(osv([('01.03', 1500.0)]), make_context())
check(info['has_lease'] is False,
      'Остаток в пределах допуска (округление) -> ведомость не нужна')

info = step._detect_leased_os(osv([('02.03', 0.0)]), make_context())
check(info['has_lease'] is False,
      'Нулевой остаток при наличии строк -> ведомость не нужна')

info = step._detect_leased_os(osv([('60', 1000.0), ('76.07', -500.0)]),
                              make_context())
check(info['has_lease'] is False and info['rows'] == 0,
      'Строк 01.03/02.03 нет вовсе -> ведомость не нужна (регресс '
      'ResourceShanghaiImport)')

info = step._detect_leased_os(osv([('01.03', -5000.0), ('02.03', 1000.0)]),
                              make_context())
check(info['has_lease'] is True and info['sum'] == -4000.0,
      'Отрицательная сумма учитывается по модулю (-4000 > допуска)')


# ======================================================================
# 6-11. _process: поведение по наличию/формату ведомости
# ======================================================================
# --- 6. Аренды нет, файл не выгружен: штатный пропуск ------------------
reset_tmp()
status, exc, context = run_process([('60', 1000.0), ('76.07', -500.0)])
check(status == 'ok' and exc is None,
      'Аренды нет + ведомость не выгружена -> шаг отрабатывает без ошибки')
check(len(context.summary_osv_df) == 2,
      'ОСВ осталась неизменной (2 строки) — шаг не тронул данные')

# --- 7. Аренды нет, файл битый: не падать (регресс 04.10.2026) --------
reset_tmp()
make_broken_statement(stmt_path)
status, exc, context = run_process([('60', 1000.0), ('76.07', -500.0)])
check(status == 'ok',
      'Аренды нет + ведомость БИТАЯ -> шаг не падает (регресс прогона '
      '04.10.2026): файл не нужен, его форма не важна')

# --- 8. Аренда есть, файл не выгружен: стоп с диагностикой ------------
reset_tmp()
status, exc, context = run_process([('01.03', OSV_COST), ('02.03', OSV_AMORT)])
check(status == 'err' and isinstance(exc, InputDataError),
      'Аренда есть + ведомость НЕ выгружена -> InputDataError ([STOP])')
message = str(exc) if exc is not None else ''
check('01.03' in message and '02.03' in message,
      'Текст ошибки перечисляет счета арендованных ОС')
check('4000.00' in message,
      'Текст ошибки содержит сумму остатков (4000.00 тыс.ед.)')
check(f'{COMPANY}_{TYPE_REGISTER}_0102_{PERIOD}_.xlsx' in message,
      'Текст ошибки содержит ожидаемое имя файла')

# --- 9. Аренда есть, файл битый: стоп с понятным текстом --------------
reset_tmp()
make_broken_statement(stmt_path)
status, exc, context = run_process([('01.03', OSV_COST)])
check(status == 'err' and isinstance(exc, InputDataError),
      'Аренда есть + ведомость БИТАЯ -> InputDataError, а не IndexError')
check('шапк' in str(exc).lower(),
      'Текст ошибки объясняет проблему с шапкой файла')

# --- 10. Аренда есть, ведомость корректная: штатный путь --------------
reset_tmp()
write_statement(stmt_path)
status, exc, context = run_process([('01.03', OSV_COST), ('02.03', OSV_AMORT)])
check(status == 'ok',
      f'Аренда есть + ведомость корректная -> шаг отрабатывает штатно'
      + ('' if exc is None else f' (но упало: {exc!r})'))
if status == 'ok':
    result = context.summary_osv_df
    lease_rows = result[result['счет'].isin(step.ACCOUNTS_01_03)]
    check(len(lease_rows) == 2,
          'Строки 01.03/02.03 пересобраны из ведомости (2 строки)')
    check(set(lease_rows['вид_связи'].unique()) == {'3 лица'},
          "вид_связи проставлен ('3 лица' — контрагент не в справочнике)")
    check(abs(lease_rows['сальдо, тыс.ед.'].sum() - 6000.0) < 0.001,
          'Сумма после замены = 6000 тыс.ед. (данные ведомости /1000)')
else:
    check(False, 'Каскад: строки 01.03/02.03 должны быть пересобраны')
    check(False, 'Каскад: вид_связи должен быть проставлен')
    check(False, 'Каскад: сумма после замены должна совпасть')

# --- 11. Регресс: сверка сходимости не потеряна ------------------------
reset_tmp()
write_statement(stmt_path)
status, exc, context = run_process([('01.03', 90000.0), ('02.03', OSV_AMORT)])
check(status == 'err' and isinstance(exc, ConvergenceError),
      'Расхождение ОСВ и ведомости больше допуска -> ConvergenceError '
      '(входная сверка сохранена)')


# ======================================================================
print('-' * 60)
shutil.rmtree(_smoke_tmp, ignore_errors=True)
label = ("Ведомость амортизации условно обязательна (п. «Обязательность "
         "ведомости амортизации», шаг 10)")
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{counters['total']}) — {label}")
    for item in failures:
        print(f"  [FAIL] {item}")
    raise SystemExit(1)
print(f"SMOKE_OK ({counters['passed']}/{counters['total']}) — {label}")
raise SystemExit(0)