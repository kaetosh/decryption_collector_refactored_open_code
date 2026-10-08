# -*- coding: utf-8 -*-
'''Смоук-тест: Шаг 6 — бизнес-правило «нет ППА, если аренды на балансе нет»
(pipeline/steps/step_06_add_os_group.py).

Кейс PrimeGoods (08.10.2026): компания арендует имущество, на 97.21 есть
виды РБП «Арендные платежи», но аренда операционная (краткосрочная) — объекты
не приняты на баланс (нет 76.07/76.05.3 и 01.03/02.03), поэтому справочник
ППА по компании не заполнен. Раньше шаг 6 всегда стопал здесь через
_raise_ppa_company_missing («нет записей по компании + есть РБП»), хотя
арендованного имущества на балансе нет и маппинг ППА не нужен.

Правило: если договоров 76.07/76.05.3 нет И остатков по счетам аренды
(76.07/76.05.3/01.03/02.03) в сводной ОСВ нет (порог tolerance_leased_os),
требование ППА для РБП 97.21 снимается целиком — WARNING вместо стопа,
строки получают группу 'не_указано' (маппятся по отдельной строке
Меппинг_бб). Если аренда на балансе ЕСТЬ — стоп остаётся (есть объекты,
но нет записей справочника → актуализация).

Сценарии:
 1-4. _has_lease_on_balance: порог, префикс, консервативная ветка.
 5. _raise_ppa_company_missing: только РБП, аренды нет -> WARNING.
 6. Есть остаток 01.03 -> MissingOSGroupError.
 7. Есть договор 76.07 -> MissingOSGroupError.
 8. Нет ни РБП, ни договоров -> WARNING.
 9. _require_lease_os_group(include_97=False): 97.21 проходит, 76.07 стопает.
10. _require_lease_os_group(include_97=True): прежнее поведение (регресс).

Запуск: conda run -n fl_acc_card python -u _smoke_06_no_lease_no_ppa.py
'''
import pandas as pd
from loguru import logger

import pipeline.steps.step_06_add_os_group as step6_module
from pipeline.errors import MissingOSGroupError
from pipeline.steps.step_06_add_os_group import Step6AddOSGroupColumnStep

failures: list = []
counters = {'passed': 0, 'total': 0}


def check(condition: bool, message: str) -> None:
    '''Мини-ассерт с накоплением результата.'''
    counters['total'] += 1
    if condition:
        counters['passed'] += 1
        print('[OK] ' + message)
    else:
        failures.append(message)
        print('[FAIL] ' + message)


UNSPEC = 'не_указано'
TOLERANCE = 3000  # тыс.ед. — дефолт tolerance_leased_os
step = Step6AddOSGroupColumnStep()

# Перехват WARNING: сценарии проверяют не только поведение, но и сообщение.
_log_records: list = []
_sink_id = logger.add(
    lambda m: _log_records.append(str(m)), level='WARNING', format='{message}'
)


def warnings_contain(text: str) -> bool:
    return any(text in line for line in _log_records)


def make_context():
    '''Минимальный контекст для _has_lease_on_balance / _raise_ppa...'''
    context = type('Ctx', (), {})()
    context.tolerance_params = {'tolerance_leased_os': TOLERANCE}
    return context


def osv(rows):
    '''Сводная ОСВ из строк (счет, сальдо_тыс_ед).'''
    return pd.DataFrame({
        'счет': pd.Series([r[0] for r in rows], dtype='string'),
        'сальдо, тыс.ед.': [float(r[1]) for r in rows],
    })


def osv_rbp_only():
    '''Кадр как у PrimeGoods: только РБП 97.21, счетов аренды нет.'''
    return pd.DataFrame({
        'счет': pd.Series(['97.21', '60.01'], dtype='string'),
        'субконто': pd.Series(['Арендные платежи', 'Кредиторки'], dtype='string'),
        'допсубконто': pd.Series(['Договор аренды 1', 'Поставщик'], dtype='string'),
        'подвид_задолженности': pd.Series(['Аренда', UNSPEC], dtype='string'),
        'договор': pd.Series([UNSPEC, UNSPEC], dtype='string'),
        'сальдо, тыс.ед.': [100.0, -3.2],
    })


# ======================================================================
# 1-4. _has_lease_on_balance
# ======================================================================
print('1-4. _has_lease_on_balance: решает по счетам аренды в сводной ОСВ')
check(
    step._has_lease_on_balance(osv([('97.21', 100.0), ('60.01', -3.2)]), make_context())
    is False,
    'аренды на балансе нет (только 97.21/60) -> False (правило применимо)',
)
check(
    step._has_lease_on_balance(osv([('01.03', 5000.0)]), make_context()) is True,
    'остаток 01.03 выше допуска -> True (аренда на балансе есть)',
)
check(
    step._has_lease_on_balance(osv([('76.07', 1500.0)]), make_context()) is False,
    'остаток 76.07 в пределах допуска -> False (порог tolerance_leased_os)',
)
check(
    step._has_lease_on_balance(pd.DataFrame({'другое': [1]}), make_context()) is True,
    "нет колонки 'счет' -> True (консервативно: точнее решить не можем)",
)
check(
    step._has_lease_on_balance(osv([('01.03.1', 9000.0)]), make_context()) is True,
    'детализация 01.03.1 -> True (префиксное сравнение)',
)

# ======================================================================
# 5. _raise_ppa_company_missing: операционная аренда -> WARNING, не стоп
# ======================================================================
print('5. _raise_ppa_company_missing: операционная аренда -> WARNING')
_log_records.clear()
_df = osv_rbp_only()
raised = None
try:
    step._raise_ppa_company_missing('PrimeGoods', pd.DataFrame(), _df, make_context())
except MissingOSGroupError as exc:
    raised = exc
check(raised is None, 'аренды на балансе нет -> MissingOSGroupError НЕ брошен')
check(
    warnings_contain('операционная аренда'),
    'WARNING про операционную аренду записан в лог',
)
check(
    warnings_contain('пропущено'),
    'WARNING сообщает, что требование ППА для РБП пропущено',
)

# ======================================================================
# 6. Есть остаток 01.03 (ППА на балансе) -> стоп остаётся
# ======================================================================
print('6. _raise_ppa_company_missing: есть 01.03 -> MissingOSGroupError')
_log_records.clear()
_df_0103 = pd.concat([osv_rbp_only(), osv([('01.03', 5000.0)])], ignore_index=True)
raised = None
try:
    step._raise_ppa_company_missing('PrimeGoods', pd.DataFrame(), _df_0103, make_context())
except MissingOSGroupError as exc:
    raised = exc
check(raised is not None, 'остаток 01.03 выше допуска -> MissingOSGroupError')
check(
    raised is not None and '97.21' in str(raised),
    'в тексте ошибки перечислены РБП 97.21',
)

# ======================================================================
# 7. Есть договор 76.07 -> правило не действует (задолженность по аренде)
# ======================================================================
print('7. _raise_ppa_company_missing: есть договор 76.07 -> стоп')
_df_76 = osv_rbp_only().copy()
_df_76.loc[0, 'счет'] = '76.07.1'          # договор аренды в ОСВ
_df_76.loc[0, 'договор'] = 'Договор аренды 1'
raised = None
try:
    step._raise_ppa_company_missing('PrimeGoods', pd.DataFrame(), _df_76, make_context())
except MissingOSGroupError as exc:
    raised = exc
check(raised is not None, 'задолженность по аренде (76.07) есть -> MissingOSGroupError')

# ======================================================================
# 8. Нет ни РБП, ни договоров -> WARNING (штатный путь без изменений)
# ======================================================================
print('8. _raise_ppa_company_missing: нет позиций -> WARNING')
_log_records.clear()
_df_none = pd.DataFrame({
    'счет': pd.Series(['60.01'], dtype='string'),
    'субконто': pd.Series(['Кредиторки'], dtype='string'),
    'допсубконто': pd.Series(['Поставщик'], dtype='string'),
    'подвид_задолженности': pd.Series([UNSPEC], dtype='string'),
    'договор': pd.Series([UNSPEC], dtype='string'),
    'сальдо, тыс.ед.': [-3.2],
})
raised = None
try:
    step._raise_ppa_company_missing('PrimeGoods', pd.DataFrame(), _df_none, make_context())
except MissingOSGroupError as exc:
    raised = exc
check(raised is None, 'позиций для меппинга нет -> MissingOSGroupError НЕ брошен')
check(
    warnings_contain('нет строк аренды/лизинга'),
    'WARNING про отсутствие строк аренды/лизинга записан',
)

# ======================================================================
# 9-10. _require_lease_os_group: include_97
# ======================================================================
print('9-10. _require_lease_os_group: include_97 управляет веткой 97.21')
_df_97_no_group = osv_rbp_only().copy()
_df_97_no_group['группа_ос_аренды_лизинга'] = pd.Series([UNSPEC, UNSPEC], dtype='string')
mask_97 = (
    _df_97_no_group['подвид_задолженности'].isin(['Аренда', 'Лизинг'])
    & (_df_97_no_group['счет'] == '97.21')
)

# 9а. include_97=False: 97.21 без группы проходит даже в строгом режиме
step6_module.STRICT_OS_GROUP_CHECK = True
include_97_ok = True
try:
    step._require_lease_os_group(_df_97_no_group, mask_97, include_97=False)
except MissingOSGroupError:
    include_97_ok = False
check(
    include_97_ok,
    'include_97=False: 97.21 без группы проходит (операционная аренда)',
)

# 9б. 76.07 без группы при include_97=False всё ещё стопает
_df_76_no_group = _df_97_no_group.copy()
_df_76_no_group.loc[0, 'счет'] = '76.07.1'
_df_76_no_group.loc[0, 'подвид_задолженности'] = 'Аренда'
mask_97_76 = (
    _df_76_no_group['подвид_задолженности'].isin(['Аренда', 'Лизинг'])
    & (_df_76_no_group['счет'] == '97.21')
)
stuck_76 = False
try:
    step._require_lease_os_group(_df_76_no_group, mask_97_76, include_97=False)
except MissingOSGroupError:
    stuck_76 = True
check(
    stuck_76,
    'include_97=False: 76.07 без группы всё ещё стопает (задолженность)',
)

# 10. include_97=True (по умолчанию): прежнее поведение — стоп на 97.21
default_97_stuck = False
try:
    step._require_lease_os_group(_df_97_no_group, mask_97)
except MissingOSGroupError:
    default_97_stuck = True
check(
    default_97_stuck,
    'include_97=True (регресс): 97.21 без группы стопает в строгом режиме',
)
step6_module.STRICT_OS_GROUP_CHECK = True  # восстанавливаем дефолт

# ======================================================================
logger.remove(_sink_id)
print('-' * 60)
label = 'операционная аренда: правило B (нет ППА при пустом балансе аренды)'
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{counters['total']}) — {label}")
    for item in failures:
        print(f"  [FAIL] {item}")
    raise SystemExit(1)
print(f"SMOKE_OK ({counters['passed']}/{counters['total']}) — {label}")
raise SystemExit(0)

