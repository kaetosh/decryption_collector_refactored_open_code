"""
Mixin с константами и базовыми атрибутами для Шага 19.

Содержит имена столбцов, ключи маппинга, технические пороги.
"""
from __future__ import annotations

from typing import Final, Sequence

from pipeline.base import Step
from pipeline.step_config import OpuReportConstants


class Step19BaseMixin(Step):
    """
    Базовый миксин для Шага 19.

    Содержит все константы класса. Конструктор наследуется от Step.
    """

    OPU_REPORT: Final = "ОПУ"
    AMOUNT_COL: Final = OpuReportConstants.AMOUNT_COL
    RUB_AMOUNT_COL: Final = OpuReportConstants.RUB_AMOUNT_COL
    VALUE_COL: Final = "Значение"
    RUB_VALUE_COL: Final = "Значение_руб"
    REPORT_TYPE_COL: Final = "Отчетность"
    ACCOUNT_COL: Final = "Итоговый номер счета"
    TARGET_ACCOUNT_COL: Final = "счет_фо"
    UNMAPPED_ACCOUNT: Final = "не_указано"

    ZERO_ROWS_EPSILON: Final = 1e-6

    RETAINED_EARNINGS_CODE: Final = "240010200"

    MAPPING_REF: Final = "меппинг_опу"
    CHART_OF_ACCOUNTS_REF: Final = "план_счетов_фо"

    MAPPING_KEY_COLS: Final = (
        "счет",
        "вид_дохода_расхода",
        "сегмент",
        "вид_связи",
    )

    PROBLEM_DETAIL_COLS: Final = (
        "контрагент",
        "ном_группа",
        "счет",
        "доход_расход",
        "вид_дохода_расхода",
        "сегмент",
        "группа_ка",
        "сегмент_ка",
        "вид_связи",
        "объект для изм ппа",
        "рбп_кредитные_линии",
    )

    ROW_ID_COL: Final = "__row_id__"
    GROUP_ACCOUNT_COL: Final = "__group_account__"

    DROP_COLUMNS = ['Примечания', 'Уникальность итогового номера счета', 'Есть в меппинге?']
    TAIL_COLUMNS = ['Отчетность', 'Статья отчетности']

    def __init__(self) -> None:
        super().__init__(
            name="Шаг 19: Финальная сборка расшифровки ОПУ",
            description="Финальная сборка расшифровки ОПУ",
        )

    @staticmethod
    def _validate_columns(df, columns: Sequence[str], entity_name: str) -> None:
        """
        Проверяет наличие обязательных столбцов в DataFrame.
        """
        missing_columns = [col for col in columns if col not in df.columns]
        if missing_columns:
            raise ValueError(
                f"В {entity_name} отсутствуют обязательные столбцы: {', '.join(missing_columns)}."
            )

    def _get_reference(self, context, name: str):
        """
        Возвращает справочник из context.references с понятной ошибкой,
        если справочник отсутствует.
        """
        try:
            return context.references[name]
        except KeyError as exc:
            raise ValueError(f"В context.references отсутствует справочник '{name}'.") from exc
