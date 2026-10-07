# -*- coding: utf-8 -*-
'''Смоук-тест: Шаг 1б — ведомость амортизации условно обязательна
(pipeline/steps/step_01b_verify_files.py).

Проблема: задача «Ведомость амортизации: условная обязательность (шаг 10)»
сделала проверку условной только в шаге 10, а шаг 1б безусловно включал
ведамор в обязательный список и останавливал конвейер раньше, чем шаг 10
успевал спросить «есть ли вообще аренда». Прогон Branch 07.10.2026
упал на 1б: [STOP] «Отсутствует обязательные спецотчеты: 1 шт.» — при
отсутствии у компании арендованного имущества.

Теперь необходимость ведомости на шаге 1б решает общая ОСВ
(_is_lease_present): остатки по 01.03/02.03 выше tolerance_leased_os —
файл обязателен (fail-fast сохранён), нет — ведомор выпадает из списка
и шаг проходит. Авторитетная проверка по сводной ОСВ остаётся в шаге 10.

Единицы: в общей ОСВ Дебет_конец/Кредит_конец — РУБЛИ, допуск — ТЫС.ЕД.
(тестовые остатки в рублях: 5 000 000 руб = 5 000 тыс.ед. > 3000).

Сценарии:
 1. Аренды нет -> ведамор вне обязательного списка, шаг проходит (INFO).
 2. Аренда выше допуска, файла нет -> MissingFilesError (fail-fast).
 3. Остаток в пределах допуска (округление) -> ведомость не нужна.
 4. Отрицательный остаток учитывается по модулю.
 5. Детализация '01.03.1' в общей ОСВ -> аренда видна (префикс).
 6. Год: без аренды -> в списке только анализ_84 (ведамор отсутствует
    и в missing_files); с арендой -> оба файла.
 7. Колонок ОСВ нет -> консервативно требуем файл.
 8. Файл ведомости лежит в special_reports -> MissingFilesError нет.

Запуск: conda run -n fl_acc_card python -u _smoke_01b_lease_optional.py
'''
import shutil
import tempfile
from pathlib import Path

import pandas as pd
from loguru import logger

import pipeline.steps.step_01b_verify_files as step1b_module
from pipeline.errors import MissingFilesError
from pipeline.steps.step_01b_verify_files import Step1bVerifyFilesStep

failures: list = []
counters = {'passed': 0, 'total': 0}

COMPANY = 'Branch'
PERIOD = '8мес2026'
TOLERANCE = 3000  # тыс.ед. — дефолт tolerance_leased_os

# Папку спецотчётов подменяем на временную — реальные _INPUT_DATA не трогаем.
_smoke_tmp = Path(tempfile.mkdtemp(prefix='smoke_01b_'))
step1b_module.SPECIAL_REPORTS_DIR = _smoke_tmp

# Перехват INFO-логов: сценарий «остатков нет -> INFO и выход» проверяет
# не только поведение, но и сообщение (format='{message}' — без времени
# и уровня, иначе подстрока ищется в мусоре).
_log_records: list = []
_sink_id = logger.add(lambda m: _log_records.append(str(m)), level='INFO',
                      format='{message}')


def check(condition: bool, message: str) -> None:
    '''Мини-ассерт с накоплением результата.'''
    counters['total'] += 1
    if condition:
        counters['passed'] += 1
        print('[OK] ' + message)
    else:
        failures.append(message)
        print('[FAIL] ' + message)


def reset_tmp() -> None:
    '''Чистит временную папку спецотчётов и лог-перехват.'''
    for item in _smoke_tmp.glob('*'):
        if item.is_file():
            item.unlink()
    _log_records.clear()


def common_osv(rows) -> pd.DataFrame:
    '''Общая ОСВ из строк (счет, дебет_конец, кредит_конец) в рублях.'''
    return pd.DataFrame({
        'Счет': [r[0] for r in rows],
        'Дебет_конец': [float(r[1]) for r in rows],
        'Кредит_конец': [float(r[2]) for r in rows],
    })


def osv_row(account: str, balance_rub: float):
    '''Строка ОСВ с сальдо (плюс — дебет, минус — кредит).'''
    if balance_rub >= 0:
        return (account, balance_rub, 0.0)
    return (account, 0.0, -balance_rub)


def make_context(rows=(), type_period='месяц', osv_df=None):
    '''Контекст с минимальным набором для шага 1б.'''
    context = type('Ctx', (), {})()
    context.company = COMPANY
    context.period = PERIOD
    context.type_period = type_period
    context.data = {}
    if osv_df is not None:
        context.common_osv_df = osv_df
    else:
        context.common_osv_df = common_osv(rows)
    context.tolerance_params = {'tolerance_leased_os': TOLERANCE}
    return context


def run_process(**kwargs):
    '''Прогоняет _process; возвращает ('ok'|'err', exc, context).'''
    context = make_context(**kwargs)
    try:
        step._process(context)
        return 'ok', None, context
    except Exception as exc:  # noqa: BLE001 — смоук фиксирует любой исход
        return 'err', exc, context


def vedamor_name():
    return f'{COMPANY}_ведамор_0102_{PERIOD}_.xlsx'


def info_logged(text: str) -> bool:
    return any(text in line for line in _log_records)


step = Step1bVerifyFilesStep()


# ======================================================================
# 1. Аренды нет: ведамор вне списка, шаг проходит с INFO
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('60', 1_000_000.0),
                                         osv_row('76.07', -500_000.0)])
check(status == 'ok',
      'Аренды нет -> шаг 1б проходит без ошибки (регресс Branch 07.10.2026)')
obligatory = context.data.get('special_reports_obligatory_list', [])
check(vedamor_name() not in obligatory,
      'Ведомость амортизации НЕ в обязательном списке')
check(info_logged('ведомость амортизации не обязательна'),
      'INFO: «ведомость амортизации не обязательна»')

# ======================================================================
# 2. Аренда выше допуска, файла нет: fail-fast сохранён
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('01.03', 5_000_000.0),
                                         osv_row('60', 1_000_000.0)])
obligatory = context.data.get('special_reports_obligatory_list', [])
check(vedamor_name() in obligatory,
      'Остаток 01.03 (5000 тыс.ед. > 3000) -> ведомость в обязательном списке')
check(status == 'err' and isinstance(exc, MissingFilesError),
      'Аренда есть + файл НЕ выгружен -> MissingFilesError (fail-fast 1б)')
missing = getattr(exc, 'missing_files', None) if exc is not None else None
check(missing is not None and vedamor_name() in missing,
      'Отсутствующий файл ведомости перечислен в missing_files')

# ======================================================================
# 3. Остаток в пределах допуска (копеечное округление) -> не требуем
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('01.03', 2_000_000.0)])
obligatory = context.data.get('special_reports_obligatory_list', [])
check(status == 'ok' and vedamor_name() not in obligatory,
      'Остаток 2000 тыс.ед. в пределах допуска 3000 -> ведомость не нужна')

# ======================================================================
# 4. Отрицательный остаток учитывается по модулю
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('02.03', -6_000_000.0)])
obligatory = context.data.get('special_reports_obligatory_list', [])
check(vedamor_name() in obligatory,
      'Отрицательный остаток (-6000 тыс.ед.) -> |сумма| > допуска, ведомость нужна')

# ======================================================================
# 5. Детализация ниже синтетического уровня: '01.03.1' видна по префиксу
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('01.03.1', 5_000_000.0)])
obligatory = context.data.get('special_reports_obligatory_list', [])
check(vedamor_name() in obligatory,
      "Счет '01.03.1' (детализация) -> аренда распознана по префиксу")

# ======================================================================
# 6. Годовой период: анализ_84 всегда, ведамор — по условию
# ======================================================================
reset_tmp()
status, exc, context = run_process(rows=[osv_row('60', 1_000_000.0)],
                                   type_period='год')
obligatory = context.data.get('special_reports_obligatory_list', [])
check(f'{COMPANY}_анализ_84_{PERIOD}_.xlsx' in obligatory,
      'Год: анализ_84 всегда в обязательном списке')
check(vedamor_name() not in obligatory,
      'Год без аренды: ведомости в списке нет')
check(status == 'err' and isinstance(exc, MissingFilesError)
      and vedamor_name() not in (exc.missing_files or []),
      'Год без аренды: в missing_files только анализ_84, не ведомость')

reset_tmp()
status, exc, context = run_process(rows=[osv_row('01.03', 5_000_000.0)],
                                   type_period='год')
obligatory = context.data.get('special_reports_obligatory_list', [])
check(f'{COMPANY}_анализ_84_{PERIOD}_.xlsx' in obligatory
      and vedamor_name() in obligatory,
      'Год с арендой: в списке и анализ_84, и ведомость')

# ======================================================================
# 7. Колонок ОСВ нет: консервативно требуем файл (True)
# ======================================================================
reset_tmp()
broken_df = pd.DataFrame({'ДругаяКолонка': [1]})
status, exc, context = run_process(osv_df=broken_df)
obligatory = context.data.get('special_reports_obligatory_list', [])
check(vedamor_name() in obligatory,
      'Нет нужных колонок ОСВ -> консервативно требуем ведомость')
check(status == 'err' and isinstance(exc, MissingFilesError),
      '... и MissingFilesError срабатывает (файл не выгружен)')

# ======================================================================
# 8. Файл в special_reports лежит: MissingFilesError нет
# ======================================================================
reset_tmp()
(_smoke_tmp / vedamor_name()).write_bytes(b'placeholder')
status, exc, context = run_process(rows=[osv_row('01.03', 5_000_000.0)])
check(status == 'ok',
      'Аренда есть + файл выгружен -> шаг проходит без ошибки'
      + ('' if exc is None else f' (упало: {exc!r})'))


# ======================================================================
print('-' * 60)
logger.remove(_sink_id)
shutil.rmtree(_smoke_tmp, ignore_errors=True)
label = ('Шаг 1б: ведомость амортизации условно обязательна '
         '(п. «Обязательность ведомости амортизации»)')
if failures:
    print(f'SMOKE_FAIL ({len(failures)}/{counters["total"]}) — {label}')
    for item in failures:
        print(f'  [FAIL] {item}')
    raise SystemExit(1)
print(f'SMOKE_OK ({counters["passed"]}/{counters["total"]}) — {label}')
raise SystemExit(0)
