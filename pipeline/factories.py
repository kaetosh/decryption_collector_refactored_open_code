# -*- coding: utf-8 -*-
"""
Фабрики создания пайплайнов обработки данных.

Этот модуль содержит функции для создания пайплайнов обработки,
чтобы отделить конфигурацию пайплайнов от основной логики приложения.
"""
from loguru import logger

from pipeline.base import Pipeline
from pipeline.steps import (
    Step1aListExpectedRegistersStep,
    Step1bVerifyFilesStep,
    Step1cReconcileTotalsStep,
    Step2FlatSummaryOSVStep,
    Step3AddAccountColumnStep,
    Step4AddReceivableTypeStep,
    Step5AddReceivableSubtypeStep,
    Step6AddOSGroupColumnStep,
    Step7AddLongShortTermColumnStep,
    Step8AddBioactiveSegmentColumnStep,
    Step9AddRelatedPartyTypeColumnStep,
    Step10ClassifyLeaseSourceStep,
    Step11Split60AccountDebtByOSStatusStep,
    Step11aCheckContractorSimilarityStep,
    Step12Split84AccountBalanceStep,
    Step13BuildBalanceBreakdownStep,
    Step14BuildOpuFoundationStep,
    Step15AddAdminExpensesToOpuStep,
    Step16AddCommExpensesToOpuStep,
    Step17AddOtherIncomeExpensesToOpuStep,
    Step18AddTaskAndOtherMovementsStep,
    Step19BuildOpuStep
)


def create_preparation_pipeline() -> Pipeline:
    """
    Первый пайплайн: подготовка к выгрузке из 1С.
    
    Выполняет только Шаг 1а — формирует список регистров,
    которые нужно выгрузить из 1С, и сохраняет его в Excel.
    
    Returns:
        Объект Pipeline с первым шагом
    """
    pipeline = Pipeline(name="Подготовка списка выгрузок")
    pipeline.add_step(Step1aListExpectedRegistersStep())
    return pipeline


def create_main_pipeline() -> Pipeline:
    """
    Второй пайплайн: основная обработка данных.
    
    Выполняет шаги 1б-13 после того, как все выгрузки из 1С 
    уже расположены в папке INPUT_DATA.
    
    Returns:
        Объект Pipeline с шагами 2-13
    """
    pipeline = Pipeline(name="Основной конвейер сборки расшифровки ББ и ОПУ")
    
    # ЭТАП 1: Загрузка и подготовка данных (баланс и опу)
    pipeline.add_step(Step1bVerifyFilesStep())
    pipeline.add_step(Step1cReconcileTotalsStep())
    pipeline.add_step(Step2FlatSummaryOSVStep())
    
    # ЭТАП 2: Добавление классификационных столбцов баланс
    pipeline.add_step(Step3AddAccountColumnStep())
    pipeline.add_step(Step4AddReceivableTypeStep())
    pipeline.add_step(Step5AddReceivableSubtypeStep())
    pipeline.add_step(Step6AddOSGroupColumnStep())
    pipeline.add_step(Step7AddLongShortTermColumnStep())
    pipeline.add_step(Step8AddBioactiveSegmentColumnStep())
    pipeline.add_step(Step9AddRelatedPartyTypeColumnStep())
    
    # ЭТАП 3: Специальные расчеты и классификации баланс
    pipeline.add_step(Step10ClassifyLeaseSourceStep())
    pipeline.add_step(Step11Split60AccountDebtByOSStatusStep())
    pipeline.add_step(Step11aCheckContractorSimilarityStep())
    pipeline.add_step(Step12Split84AccountBalanceStep())
    
    # ЭТАП 4: Финальная сборка расшифровки баланса
    pipeline.add_step(Step13BuildBalanceBreakdownStep())
    
    # ЭТАП 5: Добавление классификационных столбцов опу
    pipeline.add_step(Step14BuildOpuFoundationStep())
    pipeline.add_step(Step15AddAdminExpensesToOpuStep())
    pipeline.add_step(Step16AddCommExpensesToOpuStep())
    pipeline.add_step(Step17AddOtherIncomeExpensesToOpuStep())
    pipeline.add_step(Step18AddTaskAndOtherMovementsStep())
    
    # ЭТАП 6: Финальная сборка расшифровки опу
    pipeline.add_step(Step19BuildOpuStep())
    
    return pipeline