"""
Mixin с базовыми атрибутами для Шага 17.

Атрибуты:
    ACCOUNT_OTHER_INCOME = '91.01'          # счёт прочих доходов
    ACCOUNT_OTHER_EXPENSE = '91.02'         # счёт прочих расходов
    NDS_ACCOUNTS = '68.02'                 # счёт НДС
    PPA_ACCOUNTS = кортеж счетов ОС (01.09, 02.01, 01.01)
    GROUP_COLS = ключи группировки 91.01/91.02
    FX_* = константы курсовых разниц из OpuReportConstants
"""
from pipeline.base import Step
from pipeline.step_config import OpuReportConstants


class Step17BaseMixin(Step):
    """
    Базовый миксин для Шага 17.

    Содержит константы класса. Конструктор наследуется от Step.
    """

    ACCOUNT_OTHER_INCOME = '91.01'
    ACCOUNT_OTHER_EXPENSE = '91.02'

    NDS_ACCOUNTS = '68.02'

    PPA_ACCOUNTS = ('01.09', '02.01', '01.01')

    GROUP_COLS = (
        'счет',
        'вид_дохода_расхода',
        'доход_расход',
        'контрагент',
        'объект для изм ппа',
        'группа_ка',
        'сегмент_ка',
        'сегмент',
        'вид_связи',
        'рбп_кредитные_линии',
        'рбп_проценты',
    )

    FX_COL = OpuReportConstants.FX_COL
    RUB_BASE_COL = OpuReportConstants.RUB_BASE_COL
    FX_DIFFERENCE_LABEL = OpuReportConstants.FX_DIFFERENCE_LABEL
    FX_ABS_FLOOR = OpuReportConstants.FX_ABS_FLOOR

    FX_ZERO_AMOUNT_EPSILON = 1e-9
