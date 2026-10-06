# -*- coding: utf-8 -*-
'''Смоук-тест: Шаг 6 — группы ОС аренды/лизинга
(pipeline/steps/step_06_add_os_group.py) без полного запуска пайплайна.

Проверяет защиту от служебной заглушки 'не_указано' из справочника ППА:
она не должна попадать в меппинги и не должна переносить группу ОС на
строки-заглушки (синтетические счета из общей ОСВ и строки с пустым
Субконто). Из-за неё шаг 13 (меппинг баланса) не находил комбинацию в
Меппинг_бб: реальный кейс — 17 строк с группой 'Здания' вместо 'не_указано'.

Сценарии:
1. _create_mapping исключает ключ-заглушку 'не_указано', пустые строки и NaN.
2. _create_mapping сохраняет реальные ключи (РБП и договоры аренды).
3. contract_by_rbp: строка-заглушка не получает чужой договор.
4. _map_os_groups_by_rbp: заглушка в 'допсубконто' -> группа не переносится.
5. Полная цепочка шага 6: синтетический счёт 05 и счёт 20 с пустым Субконто
   получают 'не_указано', а 76.07 и 97.21 — реальные группы.

Регресс 30.09.2026 (прогон «УБ 8мес2026»): заглушка 'не_указано' встречается
не только в ключе ППА, но и в ЗНАЧЕНИИ (у договоров № 141/24 и № 142/24
заполнены договор и РБП, а группа ОС — 'не_указано'). Такая строка мимо
проверок не проходила: ключ в словаре есть, значение — строка, а не NaN,
поэтому шаг 6 молчал, а шаг 13 «меппинг баланса» падал на немеппленных
строках 76.07.1 / 97.21.

6. _create_mapping исключает строку, где ЗНАЧЕНИЕ — заглушка/пусто.
7. Дубль ключа: заглушка первой, реальная группа второй -> в словаре реальная.
8. Строгий режим: строки аренды/лизинга с заглушкой -> MissingOSGroupError
   (76.07.2 — промежуточная сумма, не-арендные счета — не обязательные).
9. Мягкий режим: тот же кейс — WARNING без исключения, шаг продолжается.
# 10. Регресс 06.10.2026: в основной ОСВ нет 76.07/76.05.3 — _merge_with_76_lease_detail
#     раньше возвращал кадр БЕЗ колонки 'договор', и _process падал KeyError (этап 4).
#     Теперь колонка добавляется со значением не_указано, этап 4 — no-op.
# 11. Тот же кадр: ветка РБП (97.21, Аренда/Лизинг) продолжает работать — группа ОС
#     проставляется по РБП, _require_lease_os_group проходит.
# 12. Регресс: при наличии 76.07 в основной ОСВ merge работает как раньше —
#     колонка 'договор' заполняется реальными договорами из детализации.

Запуск: conda run -n fl_acc_card python -u _smoke_06_os_group_placeholder.py
'''
import pandas as pd

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
step = Step6AddOSGroupColumnStep()


# ======================================================================
# Справочник ППА: реальные договоры/РБП + строки-заглушки не_указано
# ======================================================================
ppa = pd.DataFrame({
    'наименование_компании': ['УК', 'УК', 'УК', 'УК'],
    'рбп': ['РБП Аренда 1', UNSPEC, 'РБП Лизинг 1', None],
    'ос_ппа': ['ОС 1', UNSPEC, 'ОС 2', 'ОС 3'],
    'договор_аренды': ['Договор А', 'Договор Б', 'Договор В', ''],
    'группа_ос': ['Здания', 'Здания', 'Транспортные средства', 'Здания'],
})

# 1-2. Меппинги из справочника ППА
os_group_by_rbp = step._create_mapping(ppa, 'рбп', 'группа_ос')
os_group_by_contract = step._create_mapping(ppa, 'договор_аренды', 'группа_ос')
contract_by_rbp = step._create_mapping(ppa, 'рбп', 'договор_аренды')

check(UNSPEC not in os_group_by_rbp, 'os_group_by_rbp: ключ-заглушка исключён')
check(os_group_by_rbp.get('РБП Аренда 1') == 'Здания', 'os_group_by_rbp: реальный РБП сохранён')
check(os_group_by_rbp.get('РБП Лизинг 1') == 'Транспортные средства', 'os_group_by_rbp: второй РБП сохранён')
check('' not in os_group_by_rbp, 'os_group_by_rbp: пустой ключ (None) исключён')
check(UNSPEC not in os_group_by_contract, 'os_group_by_contract: ключ-заглушка исключён')
check('' not in os_group_by_contract, 'os_group_by_contract: пустая строка исключена')
check(os_group_by_contract.get('Договор А') == 'Здания', 'os_group_by_contract: реальный договор сохранён')
check(UNSPEC not in contract_by_rbp, 'contract_by_rbp: ключ-заглушка исключён')


# 3. Строка-заглушка не получает чужой договор
osv_stub = pd.DataFrame({
    'счет': ['97.21', '97.21'],
    'допсубконто': [UNSPEC, 'РБП Аренда 1'],
    'договор': [UNSPEC, UNSPEC],
})
contracts_97 = osv_stub['допсубконто'].map(contract_by_rbp)
check(pd.isna(contracts_97.iloc[0]), 'contract_by_rbp: строка-заглушка без договора')
check(contracts_97.iloc[1] == 'Договор А', 'contract_by_rbp: реальный РБП -> Договор А')

# 4. Группа по РБП: заглушка не переносится
groups_97 = step._map_os_groups_by_rbp(osv_stub, os_group_by_rbp)
check(pd.isna(groups_97.iloc[0]), '_map_os_groups_by_rbp: заглушка -> группа не переносится')
check(groups_97.iloc[1] == 'Здания', '_map_os_groups_by_rbp: реальный РБП -> Здания')


# ======================================================================
# 5. Полная цепочка заполнения (как в _process шага 6):
#    map по договору -> fillna(map по РБП) -> fillna(не_указано)
# ======================================================================
osv_all = pd.DataFrame({
    'счет': ['05', '20', '76.07.1', '97.21', '60.01'],
    'субконто': [UNSPEC, UNSPEC, 'Договор А', UNSPEC, 'КА'],
    'допсубконто': [UNSPEC, UNSPEC, 'КА', 'РБП Аренда 1', 'КА'],
    'договор': [UNSPEC, UNSPEC, 'Договор А', UNSPEC, UNSPEC],
})
osv_all['группа_ос_аренды_лизинга'] = osv_all['договор'].map(os_group_by_contract)
osv_all['группа_ос_аренды_лизинга'] = osv_all['группа_ос_аренды_лизинга'].fillna(
    step._map_os_groups_by_rbp(osv_all, os_group_by_rbp)
)
osv_all['группа_ос_аренды_лизинга'] = osv_all['группа_ос_аренды_лизинга'].fillna(UNSPEC)

got = osv_all.set_index('счет')['группа_ос_аренды_лизинга'].to_dict()
check(got['05'] == UNSPEC, 'синтетический счёт 05: не_указано (регресс: было Здания)')
check(got['20'] == UNSPEC, 'счёт 20 с пустым Субконто: не_указано (было Здания)')
check(got['76.07.1'] == 'Здания', '76.07: группа по договору сохранена')
check(got['97.21'] == 'Здания', '97.21: группа по РБП сохранена')
check(got['60.01'] == UNSPEC, 'прочие счета: штатная заглушка не_указано')

# ======================================================================
# 6-7. Заглушка в ЗНАЧЕНИИ справочника ППА (регресс 30.09.2026, «УБ 8мес2026»)
# ======================================================================
ppa_stub_group = pd.DataFrame({
    'наименование_компании': ['УБ', 'УБ', 'УБ'],
    'договор_аренды': ['Договор 141/24', 'Договор 142/24', 'Договор В'],
    'рбп': ['Проценты ППА 141', 'Проценты ППА 142', 'РБП Лизинг 1'],
    'группа_ос': [UNSPEC, '', 'Здания'],
})

by_contract = step._create_mapping(ppa_stub_group, 'договор_аренды', 'группа_ос')
by_rbp = step._create_mapping(ppa_stub_group, 'рбп', 'группа_ос')
check(
    'Договор 141/24' not in by_contract,
    '_create_mapping: договор с группой не_указано исключён (регресс УБ)',
)
check(
    'Договор 142/24' not in by_contract,
    '_create_mapping: договор с пустой группой исключён',
)
check(
    by_contract.get('Договор В') == 'Здания',
    '_create_mapping: строка с реальной группой сохранена',
)
check(
    'Проценты ППА 141' not in by_rbp,
    '_create_mapping: РБП с группой не_указано исключён',
)

dup = pd.DataFrame({
    'договор_аренды': ['Договор Д', 'Договор Д'],
    'группа_ос': [UNSPEC, 'Здания'],
})
check(
    step._create_mapping(dup, 'договор_аренды', 'группа_ос').get('Договор Д') == 'Здания',
    'дубль ключа: реальная группа выигрывает у строки-заглушки',
)

ppa_stub_contract = pd.DataFrame({
    'рбп': ['РБП 1', 'РБП 2'],
    'договор_аренды': ['Договор А', UNSPEC],
})
check(
    'РБП 2' not in step._create_mapping(ppa_stub_contract, 'рбп', 'договор_аренды'),
    'contract_by_rbp: РБП с договором-заглушкой исключён',
)

# ======================================================================
# 8-9. Обязательный признак у строк аренды/лизинга: строгий/мягкий режим
# ======================================================================
osv_lease = pd.DataFrame({
    'счет': pd.Series(['76.07.1', '97.21', '76.07.2', '60.01'], dtype='string'),
    'субконто': pd.Series(['Аренда', 'Арендные платежи', UNSPEC, 'Контрагент'], dtype='string'),
    'допсубконто': pd.Series(
        ['ООО Арендодатель', 'Проценты ППА 141', UNSPEC, 'ООО КА'], dtype='string',
    ),
    'подвид_задолженности': pd.Series(
        ['Аренда', 'Аренда', UNSPEC, UNSPEC], dtype='string',
    ),
    'группа_ос_аренды_лизинга': pd.Series([UNSPEC, None, UNSPEC, UNSPEC], dtype='string'),
})
mask_97_lease = (
    osv_lease['подвид_задолженности'].isin(['Аренда', 'Лизинг'])
    & (osv_lease['счет'] == '97.21')
)

check(
    step._is_service_value(
        pd.Series([UNSPEC, ' Здания ', None, ''], dtype='string')
    ).tolist() == [True, False, True, True],
    '_is_service_value: заглушка/пусто/NaN — служебные, пробелы обрезаются',
)

original_strict = step6_module.STRICT_OS_GROUP_CHECK
strict_error = None
step6_module.STRICT_OS_GROUP_CHECK = True
try:
    step._require_lease_os_group(osv_lease, mask_97_lease)
except MissingOSGroupError as exc:
    strict_error = exc
finally:
    step6_module.STRICT_OS_GROUP_CHECK = original_strict

check(
    strict_error is not None,
    'строгий режим: заглушка у 76.07.1/97.21 -> MissingOSGroupError',
)
if strict_error is not None:
    check(
        list(strict_error.problem_data['счет']) == ['76.07.1', '97.21'],
        'строгий режим: в problem_data только обязательные строки (76.07.2 вне аренды)',
    )
    check(
        'листе ППА' in str(strict_error),
        'строгий режим: подсказка про заполнение группы ОС в листе ППА',
    )

osv_ok = osv_lease.copy()
osv_ok['группа_ос_аренды_лизинга'] = pd.Series(
    ['Здания', 'Здания', UNSPEC, UNSPEC], dtype='string',
)
passed_ok = True
step6_module.STRICT_OS_GROUP_CHECK = True
try:
    step._require_lease_os_group(osv_ok, mask_97_lease)
except MissingOSGroupError:
    passed_ok = False
finally:
    step6_module.STRICT_OS_GROUP_CHECK = original_strict
check(passed_ok, 'строгий режим: заполненные группы проходят проверку без ошибки')

soft_ok = True
step6_module.STRICT_OS_GROUP_CHECK = False
try:
    step._require_lease_os_group(osv_lease, mask_97_lease)
except MissingOSGroupError:
    soft_ok = False
finally:
    step6_module.STRICT_OS_GROUP_CHECK = original_strict
check(soft_ok, 'мягкий режим: тот же кейс — WARNING без исключения, шаг продолжается')

# ======================================================================
# 10-12. Регресс 06.10.2026: в основной ОСВ нет 76.07/76.05.3
# ======================================================================
# Детализация 76 (из файла) есть, но в сводной ОСВ остатков 76.07 нет.
# Раньше _merge_with_76_lease_detail молча отдавал кадр БЕЗ колонки 'договор',
# и _process падал KeyError на этапе 4 (классификация по договорам).
osv_no_76 = pd.DataFrame({
    'счет': pd.Series(['97.21', '60.01'], dtype='string'),
    'субконто': pd.Series(['Арендные платежи', 'Контрагент'], dtype='string'),
    'допсубконто': pd.Series(['РБП Аренда 1', 'ООО КА'], dtype='string'),
    'сальдо, тыс.ед.': [12.5, -3.2],
})
detail_76_stub = pd.DataFrame({
    'счет': pd.Series(['76.07.1'], dtype='string'),
    'вид взаиморасчетов': pd.Series(['Аренда'], dtype='string'),
    'контрагент': pd.Series(['ООО Арендодатель'], dtype='string'),
    'сальдо, тыс.ед.': [10.0],
    'договор': pd.Series(['Договор А'], dtype='string'),
})

# 10. Merge без 76.07: колонка 'договор' обязана появиться со значением не_указано
merged_no_76 = step._merge_with_76_lease_detail(osv_no_76.copy(), detail_76_stub)
check('договор' in merged_no_76.columns, 'нет 76.07: колонка договор добавлена в кадр')
check(
    merged_no_76['договор'].tolist() == [UNSPEC, UNSPEC],
    'нет 76.07: все значения договора — не_указано',
)
check(
    merged_no_76['договор'].dtype != object,
    'нет 76.07: колонка договор не object (валидация выхода проходит)',
)

# Этап 4 на таком кадре — безопасный no-op: договоры не собираются, ошибок нет
contracts_no_76 = merged_no_76.loc[
    merged_no_76['договор'] != UNSPEC, 'договор'
].unique()
step._validate_mapping(
    values=pd.Series(contracts_no_76),
    mapping=os_group_by_contract,
    value_type='договоры',
    mapping_name='справочнике ППА',
    df=merged_no_76,
    column_name='договор',
)
check(len(contracts_no_76) == 0, 'нет 76.07: этап 4 — no-op, договоры не собираются')

# 11. Ветка РБП (97.21) продолжает работать: группа по РБП + _require_lease_os_group
osv_rbp_only = merged_no_76.copy()
osv_rbp_only['подвид_задолженности'] = pd.Series(['Аренда', UNSPEC], dtype='string')
osv_rbp_only['группа_ос_аренды_лизинга'] = osv_rbp_only['договор'].map(os_group_by_contract)
mask_97_rbp = (
    osv_rbp_only['подвид_задолженности'].isin(['Аренда', 'Лизинг'])
    & (osv_rbp_only['счет'] == '97.21')
)
os_groups_97_rbp = step._map_os_groups_by_rbp(osv_rbp_only, os_group_by_rbp)
osv_rbp_only['группа_ос_аренды_лизинга'] = (
    osv_rbp_only['группа_ос_аренды_лизинга'].fillna(os_groups_97_rbp)
)
check(
    osv_rbp_only.loc[osv_rbp_only['счет'] == '97.21', 'группа_ос_аренды_лизинга'].iloc[0]
    == 'Здания',
    'нет 76.07: группа ОС для 97.21 проставлена по РБП (Аренда)',
)
rbp_require_ok = True
step6_module.STRICT_OS_GROUP_CHECK = True
try:
    step._require_lease_os_group(osv_rbp_only, mask_97_rbp)
except MissingOSGroupError:
    rbp_require_ok = False
finally:
    step6_module.STRICT_OS_GROUP_CHECK = original_strict
check(rbp_require_ok, 'нет 76.07: _require_lease_os_group проходит (группы заполнены)')

# 12. Регресс: 76.07 в основной ОСВ есть — merge работает как раньше
osv_with_76 = pd.DataFrame({
    'счет': pd.Series(['76.07.1', '60.01'], dtype='string'),
    'субконто': pd.Series(['Аренда', 'Контрагент'], dtype='string'),
    'допсубконто': pd.Series(['ООО Арендодатель', 'ООО КА'], dtype='string'),
    'сальдо, тыс.ед.': [10.0, -3.2],
})
merged_with_76 = step._merge_with_76_lease_detail(osv_with_76.copy(), detail_76_stub)
check(
    merged_with_76.loc[merged_with_76['счет'] == '76.07.1', 'договор'].iloc[0]
    == 'Договор А',
    'есть 76.07: договор из детализации попадает в сводную ОСВ (штатный путь не сломан)',
)

# ======================================================================
# ======================================================================
print('-' * 60)
label = "заглушка не_указано в ППА — не ключ и не значение (шаг 6)"
if failures:
    print(f"SMOKE_FAIL ({len(failures)}/{counters['total']}) — {label}")
    for item in failures:
        print(f"  [FAIL] {item}")
    raise SystemExit(1)
print(f"SMOKE_OK ({counters['passed']}/{counters['total']}) — {label}")
raise SystemExit(0)

