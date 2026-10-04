# -*- coding: utf-8 -*-
"""
Смоук: не-УПП отчёт по проводкам (TXT) — секции «Кол.»/«Вал.» извлекаются.

Регрессия: _extract_quantity_currency_sections вызывался ДО
_rename_columns_after_pokaz, поэтому колонок 'Дебет'/'Кредит' ещё не
существовало, get_loc падал, а 4× `except (KeyError, IndexError): pass`
молча гасили ошибку — 6 колонок количества/валюты терялись.

Смоук обязан падать при провале: check() увеличивает счётчик FAILED, финальная
строка — SMOKE_OK (PASSED/total) либо SMOKE_FAIL со списком провалов и код
возврата 1.

Запуск: python -u _smoke_nonupp_posting.py
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


PASSED = 0
FAILED = 0
_failed_messages: list[str] = []


def check(condition: bool, message: str) -> None:
    """Одна проверка с прогрессом (зелёная — счётчик, красная — счётчик)."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"[OK] {message}")
    else:
        FAILED += 1
        _failed_messages.append(message)
        print(f"[FAIL] {message}")


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
    check(
        not missing,
        f"секции «Кол.»/«Вал.» не потеряны: потеряны {missing or 'ни одной'}",
    )

    check(
        len(result) == 2,
        f"в отчёте 2 строки операций (факт: {len(result)})",
    )
    check(
        list(result['Дебет_количество']) == [5.0, 7.0],
        f"Дебет_количество = [5.0, 7.0] (факт: {list(result['Дебет_количество'])})",
    )
    check(
        list(result['Кредит_количество']) == [6.0, 8.0],
        f"Кредит_количество = [6.0, 8.0] (факт: {list(result['Кредит_количество'])})",
    )
    check(
        list(result['Дебет_валюта']) == ['USD', 'EUR'],
        f"Дебет_валюта = ['USD', 'EUR'] (факт: {list(result['Дебет_валюта'])})",
    )
    check(
        list(result['Дебет_валютное_количество']) == [3.0, 9.0],
        f"Дебет_валютное_количество = [3.0, 9.0] "
        f"(факт: {list(result['Дебет_валютное_количество'])})",
    )
    check(
        list(result['Кредит_валюта']) == ['USD', 'EUR'],
        f"Кредит_валюта = ['USD', 'EUR'] (факт: {list(result['Кредит_валюта'])})",
    )
    check(
        list(result['Кредит_валютное_количество']) == [500.0, 700.0],
        f"Кредит_валютное_количество = [500.0, 700.0] "
        f"(факт: {list(result['Кредит_валютное_количество'])})",
    )
    check(
        list(result['Сумма']) == [1000.50, 2000.00],
        f"Сумма не пострадала = [1000.5, 2000.0] (факт: {list(result['Сумма'])})",
    )

    total = PASSED + FAILED
    label = 'отчёт по проводкам не-УПП (секции Кол./Вал.)'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
