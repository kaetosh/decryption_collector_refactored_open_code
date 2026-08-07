# -*- coding: utf-8 -*-
"""
Шаг 16: Добавление коммерческих расходов в расшифровку ОПУ (счет 90.07)
"""
from pipeline.steps.base_expenses_step import StepAddExpensesToOpuBase


class Step16AddCommExpensesToOpuStep(StepAddExpensesToOpuBase):
    """
    Шаг 16: Обработка коммерческих расходов (счет 90.07).
    Тонкий наследник базового класса StepAddExpensesToOpuBase.
    """
    
    def __init__(self):
        super().__init__(
            name="Шаг 16: Коммерческие расходы",
            description="Добавление движений по 90.07 счету, разбивка по видам связи КА и сегментам",
            account_opu='90.07',
            account_accumulation='44',
            opu_line_name='Коммерческие расходы',
        )

