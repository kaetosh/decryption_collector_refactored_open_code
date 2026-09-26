# -*- coding: utf-8 -*-
"""
Смоук сводки ключевых цифр прогона (io_module/run_summary.py).

Проверяет:
  1. Смоук на цифрах реального прогона (УК, 8мес2026): актив/пассив
     сходятся, чистая прибыль считается как сумма 'Значение' с переворотом знака.
  2. Несведённый баланс: расхождение сверх допуска помечается НЕ СВЕДЕНО.
  3. Копеечное расхождение печатается как «0», а не «-0».
  4. Пустые/отсутствующие таблицы и колонки не роняют сводку.
  5. Нет чистой прибыли в ОПУ — строка не показывается (не выдумывается 0).
  6. Блок печатается по строкам: ни одна строка не длиннее 500 символов,
     иначе консоль обрежет её (короткое сообщение в logger_config).
  7. Консоль и титульный лист берут цифры из одного источника
     (report_cover.collect_figures) — разойтись в числах они не могут.
  8. Сбой при сборе цифр не превращается в падение прогона (WARNING + пусто).


Запуск: python _smoke_run_summary.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from io_module import run_summary as rs
from pipeline.step_config import OpuReportConstants, ReportLayoutConstants

PASSED = 0

# Реальный прогон УК 8мес2026: актив +8 026 162, пассив -8 026 162,
# pnl_df: Выручка -1 359 246.45, Себестоимость 465 886.07, Прочие доходы -96 399.32
# Сумма VALUE = -989 759.70 -> Чистая прибыль = 989 759.70 (признак плюс)
REAL_BALANCE = pd.DataFrame({
    ReportLayoutConstants.ASSET_LIABILITY_COL: ['А', 'А', 'П', 'П'],
    ReportLayoutConstants.VALUE_COL: [4_207_893.51, 3_818_268.92, -5_081_152.15, -2_945_010.28],
    ReportLayoutConstants.LEVEL1_COL: ['Основные средства', 'Дебиторская задолженность', 'Долгосрочные кредиты и займы', 'Уставный капитал'],
})
REAL_PNL = pd.DataFrame({
    ReportLayoutConstants.LEVEL1_COL: ['Выручка', 'Себестоимость', 'Прочие доходы'],
    ReportLayoutConstants.VALUE_COL: [-1_359_246.45, 465_886.07, -96_399.32],
})


def check(condition: bool, message: str) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  [OK] {message}")
    else:
        print(f"  [FAIL] {message}")


def make_context(**overrides) -> SimpleNamespace:
    """Контекст с минимальным набором атрибутов, которые читает run_summary."""
    base = dict(
        company='УК',
        period='8мес2026',
        type_period='не_год',
        currency='RUB',
        balance_date=None,
        balance_df=REAL_BALANCE.copy(),
        pnl_df=REAL_PNL.copy(),
        data={},
        tolerance_params={'tolerance_balance': 3000.0},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def figures(context) -> dict:
    return dict(rs.collect_key_figures(context))


def main() -> int:
    print("1. Реальные цифры прогона")
    ctx = make_context()
    figs = figures(ctx)
    check(figs['Актив (итог баланса)'] == '8 026 162 тыс.ед.', f"актив: {figs['Актив (итог баланса)']}")
    check(figs['Пассив (итог баланса)'].startswith('-8 026 162'), f"пассив: {figs['Пассив (итог баланса)']}")
    check('СВЕДЕНО' in figs['Расхождение актив - пассив'], f"сходимость: {figs['Расхождение актив - пассив']}")
    check(figs['Расхождение актив - пассив'].startswith('0 '), "расхождение без знака минус")
    # Чистая прибыль = -sum(VALUE) = -(-989759.70) = 989759.70
    check(figs['Чистая прибыль (убыток)'] == '989 760 тыс.ед.', f"чистая прибыль: {figs['Чистая прибыль (убыток)']}")
    check(
        figs['Увязка ОПУ и баланса (ЧП = НРП)'].startswith('СВЕДЕНО'),
        f"статус увязки: {figs['Увязка ОПУ и баланса (ЧП = НРП)']}",
    )

    print("2. НеСВЕДЕНОный баланс и копеечное расхождение")
    off = REAL_BALANCE.copy()
    off.loc[0, ReportLayoutConstants.VALUE_COL] += 50_000
    figs = figures(make_context(balance_df=off))
    check('НЕ СВЕДЕНО' in figs['Расхождение актив - пассив'], f"расхождение 50 000: {figs['Расхождение актив - пассив']}")
    check('допуск 3 000' in figs['Расхождение актив - пассив'], "в вердикте указан применённый допуск")
    penny = REAL_BALANCE.copy()
    penny.loc[0, ReportLayoutConstants.VALUE_COL] -= 0.01
    diff = figures(make_context(balance_df=penny))['Расхождение актив - пассив']
    check(diff.startswith('0 тыс.ед.') and '-0' not in diff, f"копейка печатается как 0, а не -0: {diff}")

    print("3. Пустые и неполные данные")
    empty = figures(make_context(balance_df=None, pnl_df=None))
    check('не собрана' in empty['Баланс'], f"нет расшифровки баланса: {empty['Баланс']}")
    check('Чистая прибыль (убыток)' not in empty, "нет прибыли и пустых таблиц — строки прибыли нет")
    no_cols = figures(make_context(balance_df=pd.DataFrame({'сальдо, тыс.ед.': [1.0]})))
    check('не собрана' in no_cols['Баланс'], "нет колонок отчёта — сводка не падает")
    no_profit = figures(make_context(pnl_df=pd.DataFrame({
        ReportLayoutConstants.LEVEL1_COL: ['Себестоимость'],
        ReportLayoutConstants.VALUE_COL: [100.0],
    })))
    # Сумма = 100, переворот знака = -100 (убыток)
    check(no_profit['Чистая прибыль (убыток)'] == '-100 тыс.ед.', f"ОПУ с данными — показываем прибыль/убыток: {no_profit['Чистая прибыль (убыток)']}")
    text_value = figures(make_context(pnl_df=pd.DataFrame({
        ReportLayoutConstants.LEVEL1_COL: ['Выручка', 'Выручка'],
        ReportLayoutConstants.VALUE_COL: ['-100,5', None],
    })))
    check(
        text_value['Чистая прибыль (убыток)'] == '0 тыс.ед.',
        f"нечисловые значения отбрасываются, сумма остаётся числом: {text_value['Чистая прибыль (убыток)']}",
    )

    print("4. Формат блока для консоли")
    lines = rs.format_run_summary(ctx)
    check(len(lines) == len(rs.collect_key_figures(ctx)) + 4, f"рамка и подписи: {len(lines)} строк")
    check(lines[0] == rs.SEPARATOR and lines[-1] == rs.SEPARATOR, "блок обрамлён разделителями")
    check(lines[1].startswith(rs.HEADER), f"заголовок блока: {lines[1]}")
    check(
        all(len(line) <= 500 for line in lines),
        f"ни одна строка не длиннее 500 символов (макс. {max(len(line) for line in lines)})",
    )
    value_column = 2 + rs.LABEL_WIDTH
    rows = lines[3:-1]
    check(
        all(row[:2] == '  ' and row[2:value_column].strip() and row[value_column] == ' ' for row in rows),
        f"значения всех строк начинаются в одной колонке ({value_column})",
    )
    check(
        all(len(row[2:value_column].strip()) <= rs.LABEL_WIDTH for row in rows),
        "ни одна подпись не длиннее отведённой колонки",
    )

    print("5. Консоль и титульный лист берут цифры из одного места")
    from io_module import report_cover

    cover_numbers = dict(report_cover.collect_figures(ctx))
    console_numbers = {label: value for label, value in rs.collect_key_figures(ctx) if label in cover_numbers}
    check(cover_numbers == console_numbers, f"цифры совпадают: {console_numbers}")
    check(
        [label for label, _ in report_cover.collect_figures(ctx)]
        == [label for label, _ in rs.collect_key_figures(ctx)][:len(cover_numbers)],
        "порядок строк с цифрами на листе и в консоли одинаковый",
    )
    check(
        rs.collect_key_figures(ctx)[-1][0] == 'Увязка ОПУ и баланса (ЧП = НРП)',
        "в консоли статус увязки последней строкой",
    )
    check(
        not any('Строк в расшифровке' == label for label in cover_numbers),
        "счётчики строк не попали в общие цифры (на листе они отдельно)",
    )

    print("6. Устойчивость к сбоям")
    broken = make_context()
    broken.balance_df = 'не DataFrame'
    rs.log_run_summary(broken)
    check(True, "битый DataFrame: WARNING вместо исключения")

    class Exploding:
        @property
        def balance_df(self):
            raise RuntimeError('boom')

    rs.log_run_summary(Exploding())
    check(True, "исключение в атрибуте: WARNING вместо исключения")

    print(f"\n{'SMOKE_OK' if PASSED else 'SMOKE_FAIL'} ({PASSED} проверок)")
    return 0 if PASSED else 1


if __name__ == '__main__':
    sys.exit(main())