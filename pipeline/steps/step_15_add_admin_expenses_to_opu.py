
# -*- coding: utf-8 -*-
"""
Шаг 15: Добавление управленческих расходов в расшифровку ОПУ (счет 90.08)
"""
from pipeline.steps.base_expenses_step import StepAddExpensesToOpuBase


class Step15AddAdminExpensesToOpuStep(StepAddExpensesToOpuBase):
    """
    Шаг 15: Обработка управленческих расходов (счет 90.08).
    Тонкий наследник базового класса StepAddExpensesToOpuBase.
    """
    
    def __init__(self):
        super().__init__(
            name="Шаг 15: Управленческие расходы",
            description="Добавление движений по 90.08 счету, разбивка по видам связи КА и сегментам",
            account_opu='90.08',
            account_accumulation='26',
            opu_line_name='Управленческие расходы',
        )
