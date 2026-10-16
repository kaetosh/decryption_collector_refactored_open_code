# -*- coding: utf-8 -*-
"""Смоук: непрочитанный отчёт по проводкам (остановка) и диагностика строк.

Что проверяем (регрессия РЗК от 16.10.2026, отчёт по проводкам по счёту 26):

1. `STRICT_TRANSACTION_REPORT_LOAD = True` — если файл `*_отчпровод_*.txt`
   существует, но не читается, прогон ОСТАНАВЛИВАЕТСЯ с InputDataError, даже
   когда остальные файлы загрузились успешно. Прежде конвейер молчал: 9 файлов
   из 10 давали непустой сводный кадр, десятый выпадал, и на выходе собирался
   внешне корректный отчёт без счёта 26 (расхождение увязки ЧП = НРП на
   290 957 тыс.ед.).
2. Тот же случай при флаге False — мягкий режим: WARNING, прогон продолжается
   (обратная совместимость).
3. Пустой отчёт по проводкам (нет оборотов по счёту) — НЕ ошибка ни при каком
   значении флага: это штатная ситуация, а не поломка выгрузки.
4. `_save_txt_problem_rows` пишет `mismatches/txt_problem_rows_<run_id>.xlsx`
   со строками, исключёнными при разборе, и НЕ пишет его, когда потерь нет.
5. Колонка «что_искать» — подстрока исходной строки файла: иначе Ctrl+F в
   блокноте её не найдёт, а номер строки в TXT искать нечем.
6. Трейсбек ошибки обработки файла не попадает в сводку предупреждений
   (уровень WARNING и выше) — на титульный лист ложился сырой стек Python.

Все проверки идут на ВРЕМЕННЫХ папках: реальные `_INPUT_DATA` и
`_OUTPUT_DATA` не трогаются. Запуск: python -u _smoke_txt_load_strict.py
"""
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent))

TAB = chr(9)
NL = chr(10)

PASSED = 0
FAILED = 0


def check(name, condition, detail=''):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f'[OK]   {name}')
    else:
        FAILED += 1
        print(f'[FAIL] {name} -> {detail}')


@contextmanager
def temp_run_dir():
    """Папка вывода прогона во временном каталоге."""
    with tempfile.TemporaryDirectory(prefix='smoke_txt_out_') as tmp:
        with patch('io_module.output_manager.OUTPUT_DATA_DIR', Path(tmp)):
            from io_module.output_manager import configure_run, get_run_dir
            configure_run()
            yield Path(get_run_dir())


@contextmanager
def temp_input_dir():
    """Папка входных файлов во временном каталоге."""
    with tempfile.TemporaryDirectory(prefix='smoke_txt_in_') as tmp:
        yield Path(tmp)


@contextmanager
def captured(level='WARNING'):
    """Sink того же вида, что и в logger_config (format='{message}')."""
    messages = []
    sid = logger.add(lambda m: messages.append(str(m)), level=level,
                     format='{message}')
    try:
        yield messages
    finally:
        logger.remove(sid)


# Шапка отчёта по проводкам: 12 полей, 11 табуляций.
HEADER = TAB.join(['', 'Дата', 'Документ', 'Содержание', 'Дт', '', 'Кт', '',
                   'Сумма', 'Субконто Дт', 'Субконто Кт', 'Номер журнала'])


def posting_row(ident, sub_konto='Здание склада'):
    return TAB.join(['', '31.01.2026 23:59:59', f'Регламентная запись {ident}',
                     'Оснащение', '26.01', '', '02.01', '', '1 000,00',
                     'Б-Д', sub_konto, 'Регистр'])


def write_posting(path, rows, ragged=None):
    lines = [HEADER] + rows
    if ragged is not None:
        lines.append(ragged)
    path.write_text(NL.join(lines) + NL, encoding='utf-8')


def write_damaged(path):
    """Файл есть, но читается как отчёт по проводкам не может.

    Реальный сценарий «файл не обработан»: выгрузка пришла не та, шапки с
    колонкой «Дата» в ней нет, и `_find_header_row` обрывается. Заметьте —
    строка с незакрытой кавычкой (исходная причина регрессии РЗК) сюда НЕ
    годится: после `quoting=QUOTE_NONE` такой файл читается, и он не может
    служить примером непрочитанного.
    """
    path.write_text(
        NL.join(['Список документов за январь 2026',
                 'Всего документов: 0',
                 NL + NL]) ,
        encoding='utf-8',
    )


def call_load(folder, strict):
    """Прогоняет загрузку отчётов по проводкам с заданным значением флага."""
    import io_module.data_io as data_io
    from pipeline.errors import InputDataError
    with patch.object(data_io, 'ACCOUNT_CARDS_DIR', folder), \
            patch.object(data_io, 'STRICT_TRANSACTION_REPORT_LOAD', strict):
        try:
            return 'ok', data_io.DataLoader.load_transaction_report()
        except InputDataError as err:
            return 'stop', str(err)


def main():
    # --- 1. Непрочитанный файл среди хороших: строгий режим останавливает ---
    with temp_input_dir() as src, temp_run_dir() as out:
        write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                      [posting_row(f'44-{i}') for i in range(10)])
        write_damaged(src / 'ТЕСТ_отчпровод_26_9мес2026_.txt')

        state, payload = call_load(src, strict=True)
        check('Строгий режим: непрочитанный файл ОСТАНАВЛИВАЕТ прогон',
              state == 'stop', f'состояние={state}')
        check('Строгий режим: в сообщении назван проблемный файл',
              '26_9мес2026' in payload, f'{payload[:200]}')
        check('Строгий режим: сказано, что перевыгрузить',
              'еревыгруз' in payload, f'{payload[:200]}')
        check('Строгий режим: в списке ровно один проблемный файл',
              len([ln for ln in payload.splitlines() if ln.strip().startswith('- ')]) == 1,
              f'{payload[:200]}')
        check('Строгий режим: причина называет шапку, а не «что-то с pandas»',
              'апк' in payload.lower() or 'аголов' in payload.lower(),
              f'{payload[:200]}')

    # --- 2. Мягкий режим: прогон продолжается, файл просто выпал ---
    with temp_input_dir() as src, temp_run_dir() as out:
        write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                      [posting_row(f'44-{i}') for i in range(10)])
        write_damaged(src / 'ТЕСТ_отчпровод_26_9мес2026_.txt')
        with captured() as messages:
            state, payload = call_load(src, strict=False)
        check('Мягкий режим: прогон продолжается', state == 'ok', f'{state}')
        check('Мягкий режим: данные хороших файлов загружены',
              len(payload) > 0, f'строк={len(payload) if state == "ok" else 0}')
        check('Мягкий режим: выпавший файл назван в предупреждении',
              any('26_9мес2026' in m for m in messages),
              f'{messages[:2]}')

    # --- 3. Пустой отчёт — не ошибка ни при каком флаге ---
    for strict in (True, False):
        with temp_input_dir() as src, temp_run_dir() as out:
            write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                          [posting_row(f'44-{i}') for i in range(5)])
            write_posting(src / 'ТЕСТ_отчпровод_90.07_9мес2026_.txt', [])
            state, payload = call_load(src, strict=strict)
            check(f'Пустой отчёт 90.07 не считается поломкой (строгий={strict})',
                  state == 'ok', f'состояние={state}: {str(payload)[:120]}')

    # --- 4. Диагностика исключённых строк: файл создаётся и содержателен ---
    with temp_input_dir() as src, temp_run_dir() as out:
        write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                      [posting_row(f'44-{i}') for i in range(5)])
        # Строка с лишней табуляцией, которую не удастся привести к структуре.
        bad = TAB.join(['', 'НЕ ДАТА', 'текст', 'ещё', 'НЕ СЧЕТ', '', 'НЕ СЧЕТ',
                        '', 'НЕ СУММА', 'Наименование объекта ОС инв 12345',
                        'a', 'b', 'c', 'd'])
        write_posting(src / 'ТЕСТ_отчпровод_26_9мес2026_.txt',
                      [posting_row('26-0'), posting_row('26-1')], ragged=bad)
        with captured() as messages:
            state, payload = call_load(src, strict=False)

        files = sorted((out / 'mismatches').glob('txt_problem_rows_*.xlsx'))
        check('Диагностика: создан ровно один файл txt_problem_rows_*.xlsx',
              len(files) == 1, f'{[f.name for f in files]}')
        if files:
            df = pd.read_excel(files[0])
            check('Диагностика: есть колонка «что_искать»',
                  'что_искать' in df.columns, f'{list(df.columns)}')
            check('Диагностика: указано имя исходного файла',
                  df['файл'].iloc[0].endswith('26_9мес2026_.txt'),
                  f'{df["файл"].iloc[0]!r}')
            hint = str(df['что_искать'].iloc[0])
            check('Диагностика: «что_искать» — подстрока исходной строки',
                  hint in bad, f'{hint!r}')
            check('Диагностика: подсказка взята из наименования, а не из счёта',
                  hint == 'Наименование объекта ОС инв 12345', f'{hint!r}')
            check('Диагностика: в подсказке нет табов (вставка в «Найти»)',
                  TAB not in hint, f'{hint!r}')
            check('Диагностика: табы исходной строки показаны стрелкой',
                  '→' in str(df['строка_файла'].iloc[0]),
                  f'{df["строка_файла"].iloc[0]!r}')
            check('Диагностика: причина объяснена',
                  'табуляция' in str(df['причина'].iloc[0]),
                  f'{df["причина"].iloc[0]!r}')
            check('Диагностика: путь к файлу в логе относительный',
                  any('_OUTPUT_DATA' not in m and 'txt_problem_rows' in m
                      for m in messages),
                  f'{[m for m in messages if "txt_problem_rows" in m][:1]}')

    # --- 5. Чистый файл: диагностика НЕ создаётся ---
    with temp_input_dir() as src, temp_run_dir() as out:
        write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                      [posting_row(f'44-{i}') for i in range(5)])
        write_posting(src / 'ТЕСТ_отчпровод_26_9мес2026_.txt',
                      [posting_row('26-0'), posting_row('26-1')])
        call_load(src, strict=True)
        files = sorted((out / 'mismatches').glob('txt_problem_rows_*.xlsx'))
        check('Чистые файлы: диагностика не создаётся (нет лишних файлов)',
              not files, f'{[f.name for f in files]}')

# --- 6. Сводка предупреждений не содержит трейсбека ---
    with temp_input_dir() as src, temp_run_dir() as out:
        write_posting(src / 'ТЕСТ_отчпровод_44_9мес2026_.txt',
                      [posting_row(f'44-{i}') for i in range(5)])
        write_damaged(src / 'ТЕСТ_отчпровод_26_9мес2026_.txt')
        with captured() as messages:
            call_load(src, strict=False)
        joined = NL.join(messages)
        check('Сводка предупреждений: нет строки «Traceback (most recent call last)',
              'Traceback (most recent call last)' not in joined,
              f'{[m for m in messages if "Traceback" in m][:1]}')
        check('Сводка предупреждений: нет строк вида «File "...", line N»',
              not any(m.strip().startswith('File "') for m in messages),
              f'{[m for m in messages if m.strip().startswith("File ")]}')

    print()
    total = PASSED + FAILED
    if FAILED:
        print(f'SMOKE_FAIL ({total - FAILED}/{total})')
        return 1
    print(f'SMOKE_OK ({PASSED}/{total})')
    return 0


if __name__ == '__main__':
    sys.exit(main())