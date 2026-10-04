# -*- coding: utf-8 -*-
"""
Смоук: ошибки остановки конвейера в шагах — ReferenceMismatchError и
ConvergenceError вместо ValueError (INC-6a, INC-6c); ProcessingStepError
в иерархии PipelineError (INC-8).

Что фиксирует:
  Проверки справочников в шагах 1c, 5, 13 и 19 бросали builtin
  ValueError. Причина не-PipelineError, поэтому cli/main.py печатал
  CRITICAL [!!] Неожиданная ошибка — хотя виноват справочник, который
  пользователю нужно актуализировать. Теперь это
  ReferenceMismatchError: штатная остановка [STOP] и problem_data в
  mismatches/ (для тех мест, где есть что показать).

  Аналогично три проверки сходимости выручки/себестоимости/расходов с
  общей ОСВ (шаги 14-16) бросали ValueError. Теперь это
  ConvergenceError со своей веткой в handle_pipeline_errors: в логе
  «расхождение при сверке», а не «несоответствие данных справочникам».

  Проверяется контракт, а не тексты сообщений:
  - «Справочник Меппинг без обязательных столбцов» -> ReferenceMismatchError
    с problem_data (список реально имеющихся столбцов) и reference_name;
  - «нет справочника в context.references» -> ReferenceMismatchError с
    сохранённой первопричиной KeyError;
  - ReferenceMismatchError доходит через handle_pipeline_errors до
    ProcessingStepError, у которого __cause__ — PipelineError. Именно
    это отличает [STOP] от CRITICAL [!!] в cli/main.py, поэтому тест
    проверяет цепочку целиком, а не только факт raise.
  - ConvergenceError идёт по своей ветке декоратора («расхождение при
    сверке»), а не по ветке «несоответствие данных справочникам», и
    проблемные данные доходят до места сохранения в mismatches/.
  - ProcessingStepError сам наследует PipelineError (INC-8): обёртка
    ловится единым `except PipelineError`, цепочка __cause__ при этом
    сохраняется — по ней cli/main.py и различает [STOP] и CRITICAL.

Запуск: python -u _smoke_reference_error_types.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import ProcessingContext, Step
from pipeline.errors import (
    ConvergenceError,
    PipelineError,
    ProcessingStepError,
    ReferenceMismatchError,
)
from pipeline.steps._step19_base import Step19BaseMixin
from pipeline.steps.step_13_build_balance import Step13BuildBalanceBreakdownStep

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


def test_mapping_columns_missing_gives_reference_error() -> None:
    """В Меппинг нет ключевых столбцов — справочник, а не программная ошибка."""
    step = Step13BuildBalanceBreakdownStep()
    step.name = 'step_13'
    broken = pd.DataFrame({'счет': ['60.01'], 'совпадение': ['да']})

    try:
        step._build_mapping_dict(broken)
    except ReferenceMismatchError as exc:
        check(
            isinstance(exc, PipelineError),
            'ReferenceMismatchError — PipelineError, иначе cli/main.py покажет CRITICAL [!!]',
        )
        check(
            exc.reference_name == 'Меппинг_бб',
            f"справочник назван в ошибке (факт: {exc.reference_name})",
        )
        check(
            exc.problem_data is not None,
            'problem_data заполнен, иначе в mismatches/ ничего не попадёт',
        )
        if exc.problem_data is not None:
            check(
                'счет' in set(exc.problem_data['столбец_в_справочнике']),
                f"в problem_data перечислены реальные столбцы: {exc.problem_data}",
            )
    else:
        raise AssertionError('Ожидался ReferenceMismatchError')


def test_missing_reference_keeps_cause() -> None:
    """Отсутствующий справочник: первопричина KeyError должна остаться в __cause__."""
    context = ProcessingContext(company='Тест', segment='Собственность', period='202608',
                                type_period='8 мес')
    context.references = {'план_счетов_фо': pd.DataFrame()}

    try:
        Step19BaseMixin._get_reference(None, context, 'меппинг_опу')
    except ReferenceMismatchError as exc:
        check(
            exc.reference_name == 'меппинг_опу',
            f"справочник назван в ошибке (факт: {exc.reference_name})",
        )
        check(
            isinstance(exc.__cause__, KeyError),
            f"первопричина KeyError сохранена (факт: {exc.__cause__!r})",
        )
    else:
        raise AssertionError('Ожидался ReferenceMismatchError')


def test_reference_error_stops_pipeline_as_stop_not_critical() -> None:
    """Цепочка целиком: ReferenceMismatchError -> ProcessingStepError(PipelineError)."""
    class _FailingStep(Step):
        def __init__(self):
            super().__init__('step_test')

        def _process(self, context):
            raise ReferenceMismatchError('Справочник неактуален', reference_name='Меппинг_бб')

    context = ProcessingContext(company='Тест', segment='Собственность', period='202608',
                                type_period='8 мес')

    try:
        _FailingStep().execute(context)
    except ProcessingStepError as exc:
        cause = exc.__cause__
        check(isinstance(cause, ReferenceMismatchError), f"причина — ReferenceMismatchError (факт: {cause!r})")
        check(
            isinstance(cause, PipelineError),
            'первопричина — PipelineError, иначе cli/main.py напечатал бы '
            'CRITICAL [!!] Неожиданная ошибка вместо [STOP]',
        )
        check(
            context.step_metrics[-1]['status'] == 'error',
            f"метрики шага записали статус error (факт: {context.step_metrics})",
        )
    else:
        raise AssertionError('Ожидался ProcessingStepError')


def test_convergence_error_has_own_stop_message() -> None:
    """Сверка с ОСВ сверх допуска: свой тег в логе, дамп диагностики, жёсткий стоп."""
    from loguru import logger

    class _DivergingStep(Step):
        def __init__(self):
            super().__init__('step_test_conv')
            self.saved_error = None

        def _save_reference_mismatch_report(self, error):
            # подмена записи файла: в смоук не пишем в _OUTPUT_DATA
            self.saved_error = error

        def _process(self, context):
            raise ConvergenceError(
                'Выручка из отчёта по проводкам отличается от общей ОСВ',
                problem_data=pd.DataFrame([{'проверка': 'выручка_против_ОСВ'}]),
                reference_name='выручка_против_ОСВ',
            )

    context = ProcessingContext(company='Тест', segment='Собственность', period='202608',
                                type_period='8 мес')
    captured: list[str] = []
    handler = logger.add(captured.append, level='ERROR', format='{message}')
    try:
        step = _DivergingStep()
        try:
            step.execute(context)
        except ProcessingStepError as exc:
            check(
                isinstance(exc.__cause__, ConvergenceError),
                f"причина — ConvergenceError (факт: {exc.__cause__!r})",
            )
            check(
                isinstance(exc.__cause__, PipelineError),
                'ConvergenceError остаётся PipelineError',
            )
        else:
            raise AssertionError('Ожидался ProcessingStepError')
    finally:
        logger.remove(handler)

    messages = [str(message) for message in captured]
    check(
        any('расхождение при сверке' in m for m in messages),
        f"в логе есть тег «расхождение при сверке»: {messages}",
    )
    check(
        not any('несоответствие данным справочникам' in m for m in messages),
        'ConvergenceError не прошёл через ветку справочников (неверный текст ошибки)',
    )
    # проблемные данные дошли до места дампа
    check(step.saved_error is not None, 'декоратор вызвал сохранение проблемных данных')
    if step.saved_error is not None:
        check(
            step.saved_error.reference_name == 'выручка_против_ОСВ',
            f"справочник в дампе назван (факт: {step.saved_error.reference_name})",
        )
        check(
            len(step.saved_error.problem_data) == 1,
            f"в дамп ушли проблемные данные (строк: {len(step.saved_error.problem_data)})",
        )


def test_processing_step_error_is_pipeline_error() -> None:
    """Обёртка лежит в иерархии PipelineError (INC-8)."""
    wrapped = ProcessingStepError('Сбой на этапе')
    check(
        isinstance(wrapped, PipelineError),
        'ProcessingStepError — подкласс PipelineError, иначе except PipelineError '
        'его не поймает',
    )
    try:
        raise ProcessingStepError('Сбой на этапе') from ReferenceMismatchError(
            'нет записи', reference_name='Меппинг_бб',
        )
    except PipelineError as exc:
        caught = exc
    check(
        isinstance(caught, ProcessingStepError),
        f"обёртка не потерялась при перехвате (факт: {caught!r})",
    )
    check(
        isinstance(caught.__cause__, ReferenceMismatchError),
        'цепочка __cause__ сохранена — по ней cli/main.py различает [STOP] и CRITICAL',
    )


def main() -> None:
    test_mapping_columns_missing_gives_reference_error()
    test_missing_reference_keeps_cause()
    test_reference_error_stops_pipeline_as_stop_not_critical()
    test_convergence_error_has_own_stop_message()
    test_processing_step_error_is_pipeline_error()

    total = PASSED + FAILED
    label = 'ошибки остановки как [STOP] (INC-6a, INC-6c, INC-8)'
    if FAILED:
        print(f"SMOKE_FAIL ({FAILED}/{total}) — {label}")
        for line in _failed_messages:
            print(f"  [FAIL] {line}")
        sys.exit(1)
    print(f"SMOKE_OK ({PASSED}/{total}) — {label}")
    sys.exit(0)


if __name__ == '__main__':
    main()
