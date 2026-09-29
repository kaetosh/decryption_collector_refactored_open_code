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

Запуск: conda run -n fl_acc_card python -u _smoke_06_os_group_placeholder.py
'''
import pandas as pd

from pipeline.steps.step_06_add_os_group import Step6AddOSGroupColumnStep

failures: list = []


def check(condition: bool, message: str) -> None:
    '''Мини-ассерт с накоплением результата.'''
    if condition:
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
print('-' * 60)
if failures:
    print('[FAIL] SMOKE_FAILED: ' + str(len(failures)))
    for item in failures:
        print('   - ' + item)
    raise SystemExit(1)
print('[OK] SMOKE_OK')

