# -*- coding: utf-8 -*-
'''Смоук-тест: исключения ложных срабатываний fuzzy-матчинга контрагентов
(шаг 11а, лист «ИсключенияПохожих»).

Зачем
-----
Шаг 11а сравнивает контрагентов-третьих лиц с названиями компаний группы по
НОРМАЛИЗОВАННОМУ имени: ОПФ, стоп-слова и цифры выжигаются, слова
сортируются. Поэтому «РЕСУРС ПО ООО» даёт 100 %-ное совпадение с «ООО
Ресурс» (оба нормализуются в «ресурс»), а «Ресурс Наша» и «Ресурс НеНаша» —
в одно и то же «ресурс наш». Такие контрагенты — настоящие третьи лица, они
встречаются во многих компаниях группы и постоянно дают ложную тревогу.

Нормализацию чинить нельзя (она делает «ООО Рыба Мясо» = «Рыба мясо ООО»),
поэтому ложные срабатывания гасит адресный справочник исключений.

Тестовые данные взяты из реального прогона РЗК 9мес2026: контрагент
«РЕСУРС ПО ООО» давал совпадения со всеми вариантами написания «Ресурс»
(«ООО Ресурс», «РЕСУРС ООО» — 100 %, «ООО Ресурс 1», «Ресурс-1 ООО» — 85,7 %).
Рядом оставлен второй контрагент-похожее имя, которого исключение не касается.

Число находок в тестах не захардкожено: базовый прогон без справочника задаёт
эталон, остальные сценарии сравниваются с ним — иначе тест ломался бы от
правки порога схожести или состава справочника.

Проверяется
-----------
 1. Листа исключений нет -> поведение ровно как раньше (WARNING, один лист).
 2. Лист есть, но пуст -> то же самое.
 3. Исключение по контрагенту гасит ВСЕ его пары и не трогает чужие.
 4. Пара «контрагент + похожая_компания» гасит ровно одну пару.
 5. Селектор «компания»: своё написание срабатывает, чужое — WARNING.
 6. Совпадение по нормализованному имени (перестановка слов, регистр, ОПФ).
 7. Правило, не нашедшее совпадений -> WARNING (иначе опечатка молчит).
 8. Строка без контрагента -> WARNING «пропущено», находки на месте.
 9. Когда всё исключено: WARNING не выдаётся, файл пишется двумя листами.
10. Сводка в context.data и строка титульного листа.
11. Шаг не меняет данные ОСВ (исключение убирает предупреждение, не цифры).

Запуск: python -u _smoke_contractor_similarity_exclusions.py
'''
import sys
import tempfile
from pathlib import Path

import pandas as pd
from loguru import logger

import pipeline.steps.step_11a_check_contractor_similarity as step11a_module
from pipeline.constants import ColumnNames
from pipeline.steps.step_11a_check_contractor_similarity import (
    Step11aCheckContractorSimilarityStep,
)
from pipeline.step_config import SimilarityExclusionConstants as SE
from io_module.report_cover import _similarity_exclusions_text

failures: list = []
counters = {'passed': 0, 'total': 0}

COMPANY = 'РЗК'
PERIOD = '9мес2026'
SALDO = -431.83
NOISY = 'РЕСУРС ПО ООО'   # контрагент с находками (реальный кейс РЗК)
OTHER = 'Ресурс-1 ООО'   # похожее имя, исключением НЕ задет


def check(condition: bool, description: str) -> None:
    counters['total'] += 1
    if condition:
        counters['passed'] += 1
        print(f'  OK   {description}')
    else:
        failures.append(description)
        print(f'  FAIL {description}')


# ─────────────────────────────────────────────────────────────────────────────
# Изоляция: вывод во временную папку, предупреждения — в список
# ─────────────────────────────────────────────────────────────────────────────
SMOKE_OUT = Path(tempfile.mkdtemp(prefix='smoke_similarity_'))
step11a_module.get_output_dir = lambda subfolder=None: (
    (SMOKE_OUT / subfolder) if subfolder else SMOKE_OUT
)
(SMOKE_OUT / 'warnings').mkdir(parents=True, exist_ok=True)

_logs: list = []
_handler_id = logger.add(
    lambda message: _logs.append(str(message)),
    level='INFO',
    format='{level}|{message}',
)


def take_logs():
    """Забирает накопленные записи и разносит их по уровню.

    INFO ловится тоже: часть сценариев проверяет именно его — что при полном
    снятии находок шаг пишет INFO, а не WARNING. Собирать оба уровня одним
    sink'ом удобнее, чем двумя: список один, разделение — по префиксу
    уровня в формате.
    """
    warnings = [entry.split('|', 1)[1] for entry in _logs if entry.startswith('WARNING|')]
    infos = [entry.split('|', 1)[1] for entry in _logs if entry.startswith('INFO|')]
    _logs.clear()
    return warnings, infos


# ─────────────────────────────────────────────────────────────────────────────
# Данные прогона
# ─────────────────────────────────────────────────────────────────────────────
def osv_rows() -> pd.DataFrame:
    """Сводная ОСВ: контрагенты-третьи лица на счетах с контрагентами."""
    return pd.DataFrame({
        'счет': ['60.01', '60.01', '62.01'],
        'вид_связи': ['3 лица', '3 лица', '3 лица'],
        'допсубконто': [NOISY, NOISY, OTHER],
        'сальдо, тыс.ед.': [SALDO, SALDO, 120.5],
    })


GROUP_COMPANIES = pd.DataFrame({
    'СтандартноеНазвание': ['Ресурс', 'Ресурс 1', 'Ресурс 1'],
    'ВариантыНазвания': [
        'ООО Ресурс',
        'ООО Ресурс 1',
        'Ресурс-1 ООО',
    ],
    'ВидСвязиКА': ['Прочие ГАП', 'Прочие ГАП', 'Прочие ГАП'],
    'сегмент': ['Прочие', 'Прочие', 'Прочие'],
})

GROUP_LIST = pd.DataFrame({
    ColumnNames.SHORT_COMPANY_NAME: ['РЗК', 'ГСК'],
})


def exclusions_frame(rows: list) -> pd.DataFrame:
    """Лист «ИсключенияПохожих»: контрагент обязателен, остальное — селекторы."""
    return pd.DataFrame(
        rows,
        columns=[
            SE.COL_CONTRACTOR,
            SE.COL_LIKE_COMPANY,
            SE.COL_COMPANY,
            SE.COL_COMMENT,
        ],
        dtype='string',
    )


def make_context(exclusions, company: str = COMPANY):
    """Минимальный контекст: шагу нужны только эти атрибуты."""

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.company = company
    ctx.period = PERIOD
    ctx.summary_osv_df = osv_rows()
    ctx.data = {}
    ctx.references = {
        'вид_связи_ка': GROUP_COMPANIES.copy(),
        'компании_группы': GROUP_LIST.copy(),
    }
    if exclusions is not None:
        ctx.references['исключения_похожих'] = exclusions
    return ctx


def report_path() -> Path:
    return SMOKE_OUT / 'warnings' / f'WARNING_similar_contractors_{COMPANY}_{PERIOD}.xlsx'


def read_sheet(name: str) -> pd.DataFrame:
    return pd.read_excel(report_path(), sheet_name=name)


def run(ctx):
    """Прогоняет шаг и возвращает (context, {имя листа: кадр})."""
    if report_path().exists():
        report_path().unlink()
    take_logs()
    Step11aCheckContractorSimilarityStep()._process(ctx)
    sheets = {}
    if report_path().exists():
        book = pd.ExcelFile(report_path())
        sheets = {name: pd.read_excel(book, sheet_name=name) for name in book.sheet_names}
        book.close()
    return ctx, sheets


def pairs_of(frame: pd.DataFrame) -> set:
    """Множество пар «контрагент — своя компания» из отчёта."""
    return set(zip(frame['контрагент_в_осв'], frame['похожая_своя_компания']))


# ─────────────────────────────────────────────────────────────────────────────
# Сценарии
# ─────────────────────────────────────────────────────────────────────────────
print('1. Листа исключений нет — поведение как раньше (задаёт эталон)')
base_ctx, base_sheets = run(make_context(None))
base_pairs = pairs_of(read_sheet(SE.SHEET_REPORT))
noisy_pairs = {pair for pair in base_pairs if pair[0] == NOISY}
other_pairs = base_pairs - noisy_pairs

check(len(noisy_pairs) >= 3, f'у «{NOISY}» есть находки ({len(noisy_pairs)} пар)')
check(len(other_pairs) >= 1, f'у второго контрагента находки не пересекаются')
check(SE.SHEET_EXCLUDED not in base_sheets, 'лист «Исключено» не создаётся')
check(
    any('потенциальных совпадений контрагентов' in w for w in take_logs()[0]),
    'WARNING о потенциальных совпадениях выдан — поведение прежнее',
)
check(SE.SUMMARY_KEY not in base_ctx.data, 'сводки исключений нет')

print('2. Лист исключений есть, но пуст — то же самое')
_, sheets = run(make_context(exclusions_frame([])))
check(
    pairs_of(read_sheet(SE.SHEET_REPORT)) == base_pairs,
    'пустой лист исключений не меняет отчёт',
)
check(SE.SHEET_EXCLUDED not in sheets, 'лист «Исключено» не создан')

print('3. Исключение по контрагенту гасит все его пары и не трогает чужие')
_, sheets = run(make_context(exclusions_frame([
    (NOISY, None, None, 'третье лицо, «ПО» — не ОПФ'),
])))
report_pairs = pairs_of(read_sheet(SE.SHEET_REPORT))
check(
    not (report_pairs & noisy_pairs),
    'пары исключённого контрагента убраны из отчёта',
)
check(report_pairs == other_pairs, 'находки второго контрагента остались целыми')
check(SE.SHEET_EXCLUDED in sheets, 'добавлен лист «Исключено по справочнику»')

excluded = read_sheet(SE.SHEET_EXCLUDED)
check(len(excluded) == len(noisy_pairs), f'исключены все пары ({len(excluded)})')
check(
    set(excluded['контрагент_в_осв']) == {NOISY},
    'в листе «Исключено» только пары исключённого контрагента',
)
check(
    'основание' in excluded.columns and NOISY in str(excluded['основание'].iloc[0]),
    'видно, по какому правилу убрана пара',
)
check(
    any('потенциальных совпадений контрагентов' in w for w in take_logs()[0]),
    'WARNING оставшихся находок выдан (второй контрагент не исключён)',
)

print('4. Пара «контрагент + похожая_компания» гасит ровно одну пару')
target = sorted(noisy_pairs)[0]
_, sheets = run(make_context(exclusions_frame([
    (NOISY, target[1], None, 'совпадение ложное'),
])))
report_pairs = pairs_of(read_sheet(SE.SHEET_REPORT))
check(len(read_sheet(SE.SHEET_EXCLUDED)) == 1, 'исключена ровно одна пара')
check(target not in report_pairs, f'исключена именно пара с «{target[1]}»')
check(
    (report_pairs | {target}) == base_pairs,
    'остальные пары остались в отчёте',
)

print('5. Селектор «компания»')
_, sheets = run(make_context(exclusions_frame([
    (NOISY, None, 'ДругаяКомпания', 'опечатка'),
])))
check(
    pairs_of(read_sheet(SE.SHEET_REPORT)) == base_pairs,
    'чужое написание компании не гасит находки',
)
check(
    any('неизвестная компания' in w for w in take_logs()[0]),
    'WARNING об опечатке в названии компании выдан',
)
_, sheets = run(make_context(exclusions_frame([
    (NOISY, None, COMPANY, 'для этой компании'),
])))
check(
    not (pairs_of(read_sheet(SE.SHEET_REPORT)) & noisy_pairs),
    'своя компания — исключение сработало',
)

print('6. Совпадение по нормализованному имени')
_, sheets = run(make_context(exclusions_frame([
    ('ООО Ресурс ПО', None, None, 'написание отличается порядком слов'),
])))
check(
    not (pairs_of(read_sheet(SE.SHEET_REPORT)) & noisy_pairs),
    'перестановка слов и регистр не мешают исключению',
)
_, sheets = run(make_context(exclusions_frame([
    ('Ресурс ПУ ООО', None, None, 'похоже, но другое лицо'),
])))
check(
    pairs_of(read_sheet(SE.SHEET_REPORT)) == base_pairs,
    'похожее, но не то же имя исключением не считается',
)

print('7. Правило, не нашедшее совпадений')
ctx, _ = run(make_context(exclusions_frame([
    (NOISY, None, None, 'применится'),
    ('ООО Нет Такого Контрагента', None, None, 'опечатка'),
])))
check(
    any('Исключение не сработало' in w for w in take_logs()[0]),
    'WARNING о несработавшем исключении выдан (иначе опечатка молчала бы)',
)
check(
    ctx.data[SE.SUMMARY_KEY][SE.SUMMARY_RULES_UNMATCHED] == 1,
    'в сводке видно одно несработавшее правило',
)
check(
    ctx.data[SE.SUMMARY_KEY][SE.SUMMARY_RULES_APPLIED] == 1,
    'в сводке видно одно сработавшее правило',
)

print('8. Строка без контрагента')
ctx, _ = run(make_context(exclusions_frame([
    (None, 'ООО Ресурс', None, 'забыли указать контрагента'),
])))
check(
    any('не заполнена колонка «контрагент»' in w for w in take_logs()[0]),
    'WARNING о строке без контрагента выдан',
)
check(
    pairs_of(read_sheet(SE.SHEET_REPORT)) == base_pairs,
    'находки остались на месте',
)

print('9. Все находки исключены — WARNING нет, но след виден')
ctx, sheets = run(make_context(exclusions_frame([
    (NOISY, None, None, 'третье лицо'),
    (OTHER, None, None, 'тоже третье лицо'),
])))
check(
    len(read_sheet(SE.SHEET_REPORT)) == 0,
    'в основном отчёте не осталось ни одной строки',
)
# Один захват на оба уровня: take_logs() очищает список, второй вызов
# вернул бы уже пустое.
warnings, infos = take_logs()
check(
    not any('потенциальных совпадений контрагентов' in w for w in warnings),
    'WARNING о потенциальных совпадениях не выдан',
)
check(
    any('исключены справочником' in message for message in infos),
    'INFO о применённых исключениях выдан',
)
check(SE.SHEET_EXCLUDED in sheets, 'файл содержит лист «Исключено»')

print('10. Сводка и титульный лист')
summary = ctx.data[SE.SUMMARY_KEY]
check(
    summary[SE.SUMMARY_PAIRS_EXCLUDED] == len(base_pairs),
    'сводка: исключены все найденные пары',
)
check(
    summary[SE.SUMMARY_CONTRACTORS_EXCLUDED] == 2,
    'сводка: исключено два контрагента',
)
text = _similarity_exclusions_text(ctx)
check('применено — 2 правил' in text, f'титул показывает применённые правила: {text}')
check(
    f'исключено {len(base_pairs)} совпадений' in text,
    'титул показывает число исключений',
)
check(
    'не применялись' in _similarity_exclusions_text(make_context(None)),
    'без справочника титул честно пишет «не применялись»',
)

print('11. Данные не изменяются')
ctx, _ = run(make_context(exclusions_frame([
    (NOISY, None, None, 'третье лицо'),
    (OTHER, None, None, 'тоже третье лицо'),
])))
check(
    list(ctx.summary_osv_df['допсубконто']) == [NOISY, NOISY, OTHER],
    'ОСВ не тронута: исключение убирает предупреждение, а не сальдо',
)

logger.remove(_handler_id)

print()
if failures:
    print(f'SMOKE_FAIL ({counters["passed"]}/{counters["total"]})')
    for item in failures:
        print(f'  - {item}')
    sys.exit(1)

print(f'SMOKE_OK ({counters["passed"]}/{counters["total"]})')