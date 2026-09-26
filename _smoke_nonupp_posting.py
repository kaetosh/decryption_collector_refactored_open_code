# -*- coding: utf-8 -*-
"""
Смоук: не-УПП отчёт по проводкам (TXT) — секции «Кол.»/«Вал.» извлекаются.

Регрессия: _extract_quantity_currency_sections вызывался ДО
_rename_columns_after_pokaz, поэтому колонок 'Дебет'/'Кредит' ещё не
существовало, get_loc падал, а 4× `except (KeyError, IndexError): pass`
молча гасили ошибку — 6 колонок количества/валюты терялись.

Запуск: python _smoke_nonupp_posting.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data_processors.transaction_report import Posting_NonUPPFileProcessor

HEADER = ['Период', 'Показ', 'Unnamed: 2', 'Unnamed: 3', 'Unnamed: 4',
          'Unnamed: 5', 'Сумма']

# Раскладка реальной выгрузки: колонки «Дебет/Дебет_значение/Кредит/
# Кредит_значение/Сумма» в строке операции несут счёт и расшифровку сторон,
# а в строках-продолжениях «Кол.»/«Вал.» те же колонки переиспользуются под
# количество и валюту. Период в продолжениях пуст — они отсекаются фильтром
# по дате, а их значения переносятся в колонки секций. Строка «Итоги»
# продолжает и по Кол./Вал., поэтому секции длиннее отчёта на 1.
ROWS = [
    ['01.03.2026', 'Дт', '', 'Поставщик', '60', 'Поставщик', '1000,50'],
    ['', 'Кол.', '', '5', '', '6', ''],
    ['', 'Вал.', '', 'USD', '3', 'USD', '500'],
    ['02.03.2026', 'Дт', '', 'Поставщик', '60', 'Поставщик', '2000,00'],
    ['', 'Кол.', '', '7', '', '8', ''],
    ['', 'Вал.', '', 'EUR', '9', 'EUR', '700'],
    ['Итоги', 'Дт', '', 'Поставщик', '60', 'Поставщик', '3000,50'],
    ['', 'Кол.', '', '12', '', '12', ''],
    ['', 'Вал.', '', 'USD', '12', 'USD', '1200'],
]

EXPECTED_COLUMNS = [
    'Дебет_количество', 'Кредит_количество',
    'Дебет_валюта', 'Дебет_валютное_количество',
    'Кредит_валюта', 'Кредит_валютное_количество',
]


def build_report(tmp_dir: Path) -> Path:
    path = tmp_dir / 'posting.txt'
    lines = ['Отчёт по проводкам', '']
    lines.append('\t'.join(HEADER))
    lines.extend('\t'.join(str(cell) for cell in row) for row in ROWS)
    path.write_text('\n'.join(lines) + '\n', encoding='cp1251')
    return path


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = build_report(Path(tmp))
        result, _ = Posting_NonUPPFileProcessor().process_file(path, path.name)

    missing = [col for col in EXPECTED_COLUMNS if col not in result.columns]
    assert not missing, f'ПОТЕРЯНЫ КОЛОНКИ: {missing}'

    assert len(result) == 2, f'Ожидалось 2 строки, получено {len(result)}'
    assert list(result['Дебет_количество']) == [5.0, 7.0], result['Дебет_количество']
    assert list(result['Кредит_количество']) == [6.0, 8.0], result['Кредит_количество']
    assert list(result['Дебет_валюта']) == ['USD', 'EUR'], result['Дебет_валюта']
    assert list(result['Дебет_валютное_количество']) == [3.0, 9.0]
    assert list(result['Кредит_валюта']) == ['USD', 'EUR'], result['Кредит_валюта']
    assert list(result['Кредит_валютное_количество']) == [500.0, 700.0]
    assert list(result['Сумма']) == [1000.50, 2000.00], result['Сумма']

    print('SMOKE_OK', list(result.columns))


if __name__ == '__main__':
    main()
