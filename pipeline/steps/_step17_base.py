"""
Mixin с базовыми атрибутами для Шага 17.

Атрибуты:
    ACCOUNT_OTHER_INCOME = '91.01'          # счёт прочих доходов
    ACCOUNT_OTHER_EXPENSE = '91.02'         # счёт прочих расходов
    NDS_ACCOUNTS = '68.02'                 # счёт НДС
    PPA_ACCOUNTS = счета ППА из AccountConstants: 01.09/02.03/01.03 + 02.01/01.01
    GROUP_COLS = ключи группировки 91.01/91.02
    FX_* = константы курсовых разниц из OpuReportConstants
"""
from pipeline.base import Step
from pipeline.step_config import AccountConstants, OpuReportConstants


class Step17BaseMixin(Step):
    """
    Базовый миксин для Шага 17.

    Содержит константы класса. Конструктор наследуется от Step.
    """

    ACCOUNT_OTHER_INCOME = '91.01'
    ACCOUNT_OTHER_EXPENSE = '91.02'

    NDS_ACCOUNTS = '68.02'

    PPA_OPPA_ACCOUNTS = AccountConstants.PPA_OPPA_ACCOUNTS
    PPA_TRANSFER_ACCOUNTS = AccountConstants.PPA_TRANSFER_ACCOUNTS
    PPA_ACCOUNTS = AccountConstants.PPA_ACCOUNTS

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

    # Суммовые колонки проводок ОПУ — по ним проверяется инвариант
    # распределения расходов 91.02 по выручке 91.01.
    ORPHAN_AMOUNT_COLS = ('оборот, тыс.ед.', 'оборот, тыс.руб.')

    TOLERANCE_ORPHAN_DISTRIBUTION = 'tolerance_orphan_distribution'
