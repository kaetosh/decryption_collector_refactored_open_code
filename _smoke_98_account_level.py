# -*- coding: utf-8 -*-
"""
Смоук-тест: уровень счёта в иерархии ОСВ и поиск столбца со счетами.

Регресс 01.10.2026 (ББ, ОСВ 8мес2026). Шаг 1в упал с текстом «В сводной ОСВ по
счетам не найден столбец Level_, содержащий только бухгалтерские счета», и
текст уводил в сторону «проверьте формат выгрузки» — хотя формат был в порядке
у всех двадцати файлов. Виновным был один: `ББ_осв_98.01`.

Что в нём происходит. Это ОСВ по счёту с детализацией только по субконто, то
есть номер счёта есть лишь в заголовке отчёта, а строка счёта пришла с пустой
ячейкой «Субконто»:
  1. `_process_missing_values` ставит на верхний уровень заглушку 'не_указано';
  2. номер счёта из заголовка подставляется не в Level_0, а в Level_1
     (ищется «первый столбец без счетов»);
  3. `shiftable_level` дописывает в Level_1 контрагента.
В итоге Level_0 = ['не_указано', 'МИНИСТЕРСТВО...'], Level_1 = ['98.01',
'МИНИСТЕРСТВО...'] — оба «грязные». Сводная таблица склеивает 20 файлов, и
столбца со счетами не остаётся НИ ОДНОГО, поэтому останавливается весь шаг.

Проверяет (синтетические кадры — поведение воспроизводимо на любой машине):

  1. `_ensure_account_level` поднимает счёт в Level_0 на «битом» кадре 98.01.
  2. На корректном кадре (76: Level_0 = субсчета) ничего не меняется.
  3. Кадр с Level_0 = счёт из заголовка (случай 94, плоский отчёт) — no-op.
  4. Без счёта из заголовка (не-УПП) данные не подменяются.
  5. `resolve_account_level_column`: строгий путь берёт ПРАВЫЙ чистый столбец
     (паритет с find_target_column — от него зависит «счёт» в шаге 3).
  6. `resolve_account_level_column`: запасной путь на «почти чистой» сводной.
  7. `resolve_account_level_column` = None, когда столбцов со счетами нет.
  8. `describe_level_columns` называет виновника и примеры значений.
  9. Реальные файлы ОСВ (если есть): у каждого есть столбец со счетами.

Запуск: conda run -n fl_acc_card python -u _smoke_98_account_level.py
"""
import sys
from pathlib import Path

import pandas as pd

from data_processors.file_handler import FileHandler
from data_processors.file_processor import FileProcessor
from data_processors.osv_account import AccountOSV_UPPFileProcessor
from utils.column_utils import resolve_account_level_column, describe_level_columns

# При перенаправлении вывода в файл консоль Windows пишет в cp1251 —
# фиксируем UTF-8, чтобы кириллица не роняла print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FAILED = 0
PASSED = 0
_failed_messages: list[str] = []

CONTRACTOR = 'МИНИСТЕРСТВО СХ И ПРОДОВОЛЬСТВИЯ БЕЛГОРОДСКОЙ ОБЛАСТИ'


def check(condition: bool, message: str) -> None:
    """Мини-ассерт со счётчиками (смоук обязан падать при провале)."""
    global FAILED, PASSED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        FAILED += 1
        _failed_messages.append(message)
        print(f"[FAIL] {message}")


def skip(message: str) -> None:
    """Проверка неприменима (нет данных) — не в счётчик, но и не тишина."""
    print(f"[SKIP] {message}")


def make_frame(levels: dict, subconto=None) -> pd.DataFrame:
    """Кадр ОСВ с заданными Level_* и служебными колонками."""
    n = len(next(iter(levels.values())))
    data = {
        'Субконто': pd.Series(
            subconto if subconto is not None else ['не_указано', '3123019399'][:n],
            dtype='string',
        ),
        'Дебет_конец': pd.Series([0.0] * n, dtype='float'),
        'Кредит_конец': pd.Series([100.0] * n, dtype='float'),
        'Исх.файл': pd.Series(['ББ_осв_98.01_8мес2026_.xlsx'] * n, dtype='string'),
    }
    for name, values in levels.items():
        data[name] = pd.Series(values, dtype='string')
    return pd.DataFrame(data)


def has_account_column(df: pd.DataFrame) -> bool:
    """Есть ли столбец Level_*, целиком состоящий из бухгалтерских счетов."""
    return any(
        FileProcessor._is_accounting_code_vectorized(df[c]).all()
        for c in df.columns if str(c).startswith('Level_')
    )


proc = AccountOSV_UPPFileProcessor()


def run(df: pd.DataFrame, account, file_name='ББ_осв_98.01_8мес2026_.xlsx'):
    proc.file = file_name
    return proc._ensure_account_level(df, account)


# ==========================================================================
# Тест 1: битый кадр 98.01 — счёт поднимается в Level_0
# ==========================================================================
print("\n--- Тест 1: кадр 98.01 (счёт уехал в Level_1, в Level_0 аналитика) ---")
broken = make_frame({
    'Level_0': ['не_указано', CONTRACTOR],
    'Level_1': ['98.01', CONTRACTOR],
})
check(not has_account_column(broken),
      "исходный кадр действительно без столбца со счетами (это и ломало шаг 1в)")

fixed = run(broken, '98.01')
check(fixed['Level_0'].tolist() == ['98.01', '98.01'],
      f"счёт поднят в Level_0 (факт: {fixed['Level_0'].tolist()})")
check(has_account_column(fixed), "после починки столбец со счетами есть")
check(fixed['Субконто'].tolist() == ['не_указано', '3123019399'],
      "служебные колонки не тронуты")
check(fixed['Дебет_конец'].tolist() == [0.0, 0.0], "суммы не тронуты")
check(fixed['Level_1'].tolist() == ['не_указано', CONTRACTOR],
      f"аналитика сохранена и сдвинута в Level_1 (факт: {fixed['Level_1'].tolist()})")
lvl2_values = [str(v) for v in fixed['Level_2'].tolist()]
check(not any('98.01' in v for v in lvl2_values),
      f"дубль счёта убран из уровней аналитики (факт: {lvl2_values})")
check(str(fixed['Level_0'].dtype) == 'string',
      f"dtype Level_0 = 'string' (факт: {fixed['Level_0'].dtype})")


# ==========================================================================
# Тест 2: корректный кадр (76) — ничего не меняется
# ==========================================================================
print("\n--- Тест 2: корректный кадр (счёт на своём уровне) — no-op ---")
good = make_frame({
    'Level_0': ['76.01', '76.02'],
    'Level_1': ['ООО Ромашка', 'ООО Ромашка'],
    'Level_2': ['Договор 1', 'Договор 2'],
})
untouched = run(good, '76')
check(untouched is good, "корректный кадр возвращён как есть (объект не пересоздан)")
check(untouched['Level_0'].tolist() == ['76.01', '76.02'], "Level_0 не изменён")
check(untouched['Level_1'].tolist() == ['ООО Ромашка', 'ООО Ромашка'],
      "Level_1 не изменён")


# ==========================================================================
# Тест 3: случай 94 — счёт в Level_0, строки отчёта в Level_1 (_validate_empty_osv)
# ==========================================================================
print("\n--- Тест 3: случай 94 (Level_0 = счёт из заголовка, Level_1 = строки) ---")
# _validate_empty_osv поднимает все строки на Уровень=1 (отчёт плоский), из-за чего
# Level_0 пустеет и заполняется счётом из заголовка. Этот случай закрыт ДО
# _ensure_account_level, поэтому здесь важно лишь не сломать его повторно.
case94 = make_frame({'Level_0': ['94.Н', '94.Н'], 'Level_1': ['94', '94.01']})
res94 = run(case94, '94.Н', 'ББ_осв_94_8мес2026_.xlsx')
check(res94 is case94, "случай 94 — no-op: кадр уже корректен, заново не пересобирается")
check(res94['Level_0'].tolist() == ['94.Н', '94.Н'],
      f"счёт из заголовка остался в Level_0 (факт: {res94['Level_0'].tolist()})")
check(res94['Level_1'].tolist() == ['94', '94.01'],
      f"данные уровня счёта не тронуты (факт: {res94['Level_1'].tolist()})")
check(has_account_column(res94), "случай 94 не ломает контракт")


# ==========================================================================
# Тест 4: счёт из заголовка недоступен (не-УПП) — данные не подменяются
# ==========================================================================
print("\n--- Тест 4: без счёта из заголовка — только предупреждение ---")
no_account = make_frame({
    'Level_0': ['не_указано', CONTRACTOR],
    'Level_1': ['98.01', CONTRACTOR],
})
res_noacc = run(no_account, None, 'осв_не_упп.xlsx')
check(res_noacc['Level_0'].tolist() == ['не_указано', CONTRACTOR],
      "без счёта из заголовка Level_0 не переписывается")
check(res_noacc['Level_1'].tolist() == ['98.01', CONTRACTOR],
      "без счёта из заголовка Level_1 не переписывается")

no_levels = make_frame({'Level_0': [None], 'Level_1': [None]})
res_nolevel = run(no_levels, '60')
check(res_nolevel['Level_0'].tolist() == ['60'],
      f"кадр без значений Level_* получает Level_0 из заголовка "
      f"(факт: {res_nolevel['Level_0'].tolist()})")


# ==========================================================================
# Тест 5: строгий путь — правый чистый столбец (паритет с find_target_column)
# ==========================================================================
print("\n--- Тест 5: resolve_account_level_column — строгий путь ---")
clean = make_frame({
    'Level_0': ['60.01', '60.02'],
    'Level_1': ['60.01.1', '60.02'],
    'Level_2': ['ООО Ромашка', 'ООО Ромашка'],
})
check(resolve_account_level_column(clean) == 'Level_1',
      f"берётся самый правый чистый столбец "
      f"(факт: {resolve_account_level_column(clean)})")

check(resolve_account_level_column(pd.DataFrame({'счет': ['60']})) is None,
      "нет столбцов Level_* -> None")


# ==========================================================================
# Тест 6: запасной путь на «почти чистой» сводной (регресс 98.01 в сводной)
# ==========================================================================
print("\n--- Тест 6: запасной путь — 2 «не-счёта» из 3750 строк ---")
rows = 3750
dirty = pd.DataFrame({
    'Level_0': pd.Series(['60.01'] * rows, dtype='string'),
    'Level_1': pd.Series(['ООО Ромашка'] * rows, dtype='string'),
    'Исх.файл': pd.Series(['ББ_осв_60_8мес2026_.xlsx'] * rows, dtype='string'),
})
dirty.loc[dirty.index[:2], 'Level_0'] = pd.Series(
    ['не_указано', CONTRACTOR], dtype='string')
dirty.loc[dirty.index[:2], 'Исх.файл'] = pd.Series(
    ['ББ_осв_98.01_8мес2026_.xlsx'] * 2, dtype='string')
check(resolve_account_level_column(dirty) == 'Level_0',
      f"запасной путь выбирает Level_0 "
      f"(факт: {resolve_account_level_column(dirty)})")

hopeless = pd.DataFrame({
    'Level_0': pd.Series(['ООО Ромашка'] * 100, dtype='string'),
    'Level_1': pd.Series(['ООО Ромашка'] * 100, dtype='string'),
})
check(resolve_account_level_column(hopeless) is None,
      "нет столбцов с долей счетов >= 95% -> None")


# ==========================================================================
# Тест 7: диагностика называет виновника
# ==========================================================================
print("\n--- Тест 7: describe_level_columns ---")
diag = describe_level_columns(broken)
check('Level_0' in diag and 'Level_1' in diag,
      f"перечислены все уровни (факт: {diag[:70]})")
check('не_указано' in diag,
      "диагностика показывает пример «не-счёта» — видно, что именно попало в столбец")
check('отсутствуют' in describe_level_columns(pd.DataFrame({'счет': ['60']})),
      "при отсутствии Level_* диагностика не молчит")

low = clean.rename(columns={'Level_0': 'level_0',
                            'Level_1': 'level_1',
                            'Level_2': 'level_2'})
check(resolve_account_level_column(low, column_prefix='level_') == 'level_1',
      "поиск работает и для префикса в нижнем регистре (шаг 3)")


# ==========================================================================
# Тест 8: реальные выгрузки ОСВ (если они есть в _INPUT_DATA)
# ==========================================================================
print("\n--- Тест 8: реальные файлы ОСВ ---")
osv_dir = Path('_INPUT_DATA/accounts_osv')
osv_files = sorted(osv_dir.glob('*_осв_*.xlsx')) if osv_dir.is_dir() else []
if not osv_files:
    skip(f"нет выгрузок в {osv_dir.as_posix()} — пункт не проверялся")
else:
    handler = FileHandler()
    bad_files = []
    for path in osv_files:
        try:
            frame = handler.handle_input(path, 'accountosv')['accountosv'][0]
        except Exception as exc:  # обработчик упал — это тоже провал контракта
            bad_files.append(f"{path.name}: обработчик упал ({exc})")
            continue
        if not has_account_column(frame):
            bad_files.append(f"{path.name}: нет столбца Level_* со счетами")
    check(not bad_files,
          f"у всех {len(osv_files)} реальных файлов ОСВ есть столбец со счетами"
          + (f" — проблемы: {bad_files}" if bad_files else ""))


# ==========================================================================
print("\n=== ИТОГ ===")
total = PASSED + FAILED
label = "уровень счёта в Level_* для ОСВ по счёту (шаги 1в/2/3)"
if FAILED:
    print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
    for line in _failed_messages:
        print(f"  [FAIL] {line}")
    sys.exit(1)
print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
sys.exit(0)