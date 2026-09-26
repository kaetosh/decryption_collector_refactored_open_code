# -*- coding: utf-8 -*-
"""
Смоук: ошибки справочников в шагах — ReferenceMismatchError вместо ValueError (INC-6a).

Что фиксирует:
  Восемь проверок справочников в шагах 1c, 5, 13 и 19 бросали builtin
  ValueError. Причина не-PipelineError, поэтому cli/main.py печатал
  CRITICAL [!!] Неожиданная ошибка — хотя виноват справочник, который
  пользователю нужно актуализировать. Теперь это
  ReferenceMismatchError: штатная остановка [STOP] и problem_data в
  mismatches/ (для тех мест, где есть что показать).

  Проверяется контракт, а не тексты сообщений:
  - «Справочник Меппинг без обязательных столбцов» -> ReferenceMismatchError
    с problem_data (список реально имеющихся столбцов) и reference_name;
  - «нет справочника в context.references» -> ReferenceMismatchError с
    сохранённой первопричиной KeyError;
  - ReferenceMismatchError доходит через handle_pipeline_errors до
    ProcessingStepError, у которого __cause__ — PipelineError. Именно
    это отличает [STOP] от CRITICAL [!!] в cli/main.py, поэтому тест
    проверяет цепочку целиком, а не только факт raise.

Запуск: python _smoke_reference_error_types.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.base import ProcessingContext, Step
from pipeline.errors import PipelineError, ProcessingStepError, ReferenceMismatchError
from pipeline.steps._step19_base import Step19BaseMixin
from pipeline.steps.step_13_build_balance import Step13BuildBalanceBreakdownStep


def test_mapping_columns_missing_gives_reference_error() -> None:
    """В Меппинг нет ключевых столбцов — справочник, а не программная ошибка."""
    step = Step13BuildBalanceBreakdownStep()
    step.name = 'step_13'
    broken = pd.DataFrame({'счет': ['60.01'], 'совпадение': ['да']})

    try:
        step._build_mapping_dict(broken)
    except ReferenceMismatchError as exc:
        assert isinstance(exc, PipelineError), 'иначе cli/main.py покажет CRITICAL [!!]'
        assert exc.reference_name == 'Меппинг_бб', exc.reference_name
        assert exc.problem_data is not None, 'без problem_data в mismatches/ ничего не попадёт'
        assert 'счет' in set(exc.problem_data['столбец_в_справочнике']), exc.problem_data
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
        assert exc.reference_name == 'меппинг_опу', exc.reference_name
        assert isinstance(exc.__cause__, KeyError), exc.__cause__
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
        assert isinstance(cause, ReferenceMismatchError), cause
        assert isinstance(cause, PipelineError), (
            'первопричина должна быть PipelineError — иначе cli/main.py '
            'напечатает CRITICAL [!!] Неожиданная ошибка вместо [STOP]'
        )
        assert context.step_metrics[-1]['status'] == 'error', context.step_metrics
    else:
        raise AssertionError('Ожидался ProcessingStepError')


def main() -> None:
    test_mapping_columns_missing_gives_reference_error()
    test_missing_reference_keeps_cause()
    test_reference_error_stops_pipeline_as_stop_not_critical()
    print('SMOKE_OK 3 scenarios: ошибки справочников как [STOP] (INC-6a)')


if __name__ == '__main__':
    main()
