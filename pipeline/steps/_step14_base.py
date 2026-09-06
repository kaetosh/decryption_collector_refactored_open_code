"""
Mixin с базовыми атрибутами для Шага 14.

Атрибуты:
    ACCOUNTS_REVENUE_COST — счета выручки и себестоимости (90.01, 90.02).
"""
from pipeline.base import Step


class Step14BaseMixin(Step):
    """
    Базовый миксин для Шага 14.

    Содержит константы класса. Конструктор наследуется от Step.
    """

    ACCOUNTS_REVENUE_COST = ['90.01', '90.02']
