# -*- coding: utf-8 -*-
"""
Смоук: типы ошибок на границе загрузки входных данных (INC-5a, INC-5b, INC-6b).

Что фиксирует:
  Загрузчики (io_module/data_io.py), парсеры выгрузок 1С
  (data_processors/*) и проверки структуры данных в шагах
  (pipeline/steps/*) бросали builtin ValueError. Шаг оборачивал их в
  ProcessingStepError, и cli/main.py классифицировал
  первопричину «не PipelineError» как CRITICAL [!!] Неожиданная ошибка —
  хотя битый .xlsx, пустая выгрузка или сводная ОСВ без Level_-столбцов
  это штатная проблема данных.
  Теперь это InputDataError (подкласс PipelineError) с сохранённой
  первопричиной в __cause__, поэтому остановка помечается [STOP].

  Смоук проверяет контракт, а не тексты сообщений:
  - все ошибки загрузки — InputDataError, а не ValueError;
  - InputDataError остаётся PipelineError (иначе [STOP] не сработает);
  - __cause__ не теряется (первопричину видно в логе без --traceback);
  - required=False по-прежнему возвращает пустой DataFrame, а не падает;
  - FileNotFoundError для отсутствующего файла НЕ перехватывается
    (его отдельно и более понятно обрабатывает cli/main.py);
  - в парсерах не осталось builtin ValueError, а InputDataError внутри
    except всегда с `from e` (инвариант, а не разовый снимок);
  - в шаге конвейера ошибка структуры данных даёт ProcessingStepError
    с PipelineError-причиной — именно это отличает [STOP] от CRITICAL.

Запуск: python _smoke_input_data_errors.py
"""
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from io_module import data_io
from io_module.data_io import DataLoader
from pipeline.errors import InputDataError, PipelineError

TMP = Path(tempfile.mkdtemp(prefix='smoke_input_data_'))


def _expect_input_data_error(func, *args, **kwargs) -> InputDataError:
    """Вызывает func и требует InputDataError с сохранённой первопричиной."""
    try:
        func(*args, **kwargs)
    except InputDataError as exc:
        assert isinstance(exc, PipelineError), 'InputDataError должен быть PipelineError'
        assert not isinstance(exc, ValueError), (
            'InputDataError не должен быть ValueError: иначе старые '
            'обработчики except ValueError продолжат ловить его'
        )
        return exc
    raise AssertionError(f'{func} не выбросил InputDataError')


def test_wrong_extension() -> None:
    """Файл не .xlsx — входные данные, а не программная ошибка."""
    path = TMP / 'выгрузка.txt'
    path.write_text('не Excel', encoding='utf-8')
    exc = _expect_input_data_error(DataLoader._validate_file, path)
    assert 'выгрузка.txt' in str(exc), exc


def test_missing_file_stays_file_not_found() -> None:
    """Отсутствующий файл — FileNotFoundError, у него своя ветка в cli/main."""
    missing = TMP / 'нет_такого.xlsx'
    try:
        DataLoader._validate_file(missing)
    except FileNotFoundError:
        return
    except InputDataError as exc:
        raise AssertionError('отсутствие файла не должно давать InputDataError') from exc
    raise AssertionError('Ожидался FileNotFoundError')


def test_corrupt_xlsx_keeps_cause() -> None:
    """Битый .xlsx: первопричина pandas должна остаться в __cause__."""
    path = TMP / 'битый.xlsx'
    path.write_bytes(b'PK\x03\x04' + b'\x00' * 64)
    exc = _expect_input_data_error(DataLoader._load_raw_excel, path)
    assert exc.__cause__ is not None, 'первопричина потеряна — в логе будет только обёртка'
    assert 'битый.xlsx' in str(exc), exc


def test_reference_sheet_error_keeps_cause() -> None:
    """Ошибка чтения справочника: required=True -> stop с первопричиной."""
    ref = TMP / 'Справочники.xlsx'
    with pd.ExcelWriter(ref, engine='openpyxl') as writer:
        pd.DataFrame({'Код': ['1']}).to_excel(writer, sheet_name='Параметры', index=False)

    saved = data_io.REFERENCE_DATA_FILE
    data_io.REFERENCE_DATA_FILE = ref
    try:
        exc = _expect_input_data_error(DataLoader.load_reference_data, 'НетТакогоЛиста')
        assert exc.__cause__ is not None, 'первопричина pandas потеряна'
        assert 'НетТакогоЛиста' in str(exc), exc

        df = DataLoader.load_reference_data('НетТакогоЛиста', required=False)
        assert df.empty, 'required=False должен вернуть пустой DataFrame'
    finally:
        data_io.REFERENCE_DATA_FILE = saved


def test_empty_frame_branches_are_input_data_errors() -> None:
    """Пустая выгрузка: файл валиден, но данных нет -> InputDataError."""
    path = TMP / 'пустая_выгрузка.xlsx'
    pd.DataFrame().to_excel(path, index=False)
    exc = _expect_input_data_error(DataLoader.process_depreciation_statement_decoding, path)
    assert 'пустая_выгрузка.xlsx' in str(exc), exc
    assert not isinstance(exc, FileNotFoundError), exc


def test_parser_layer_raises_input_data_error() -> None:
    """Парсеры выгрузок 1С (data_processors) — тоже входные данные."""
    from data_processors.transaction_report import Posting_UPPFileProcessor

    path = TMP / 'проводки_без_шапки.txt'
    path.write_bytes('колонки\tсумма\n1\t2\n'.encode('cp1251'))
    exc = _expect_input_data_error(Posting_UPPFileProcessor._find_header_row, path, 'период')
    assert 'период' in str(exc), exc


def test_parsers_keep_no_stray_valueerror() -> None:
    """Инвариант INC-5b: в парсерах нет builtin ValueError, `from e` не забыт."""
    import ast

    from data_processors import (
        analisys_account,
        file_handler,
        file_processor,
        osv_account,
        osv_general,
        transaction_report,
    )

    modules = (
        analisys_account,
        file_handler,
        file_processor,
        osv_account,
        osv_general,
        transaction_report,
    )
    for module in modules:
        source = Path(module.__file__).read_bytes().decode('utf-8-sig')
        tree = ast.parse(source)
        in_except = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                for stmt in node.body:
                    in_except.update(n for n in ast.walk(stmt) if isinstance(n, ast.Raise))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            called = getattr(node.exc, 'func', None)
            name = getattr(called, 'id', None) or getattr(called, 'attr', None)
            assert name != 'ValueError', f'{module.__name__}:{node.lineno} — остался builtin ValueError'
            if name == 'InputDataError' and node in in_except:
                assert node.cause is not None, (
                    f'{module.__name__}:{node.lineno} — InputDataError внутри except '
                    'без `from e`: в логе пропадёт первопричина'
                )


def test_step_boundary_is_input_data_error() -> None:
    """Ошибка структуры данных в шаге конвейера — тоже [STOP], а не CRITICAL."""
    from pipeline.base import ProcessingContext, Step
    from pipeline.errors import ProcessingStepError
    from pipeline.steps.step_03_add_account import Step3AddAccountColumnStep

    context = ProcessingContext(
        company='Тест', segment='Собственность', period='202608', type_period='8 мес'
    )
    context.summary_osv_df = pd.DataFrame({'Счет': ['60.01'], 'Сальдо': [1.0]})

    try:
        Step3AddAccountColumnStep().execute(context)
    except ProcessingStepError as exc:
        assert isinstance(exc.__cause__, InputDataError), exc.__cause__
        assert isinstance(exc.__cause__, PipelineError), (
            'причина должна быть PipelineError — иначе cli/main.py напечатает '
            'CRITICAL [!!] Неожиданная ошибка'
        )
    else:
        raise AssertionError('Ожидался ProcessingStepError')


def main() -> None:
    test_wrong_extension()
    test_missing_file_stays_file_not_found()
    test_corrupt_xlsx_keeps_cause()
    test_reference_sheet_error_keeps_cause()
    test_empty_frame_branches_are_input_data_errors()
    test_parser_layer_raises_input_data_error()
    test_parsers_keep_no_stray_valueerror()
    test_step_boundary_is_input_data_error()
    print('SMOKE_OK 8 scenarios: InputDataError на границе загрузки (INC-5a/5b/6b)')


if __name__ == '__main__':
    main()
