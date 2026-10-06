# -*- coding: utf-8 -*-
"""Шаг 12а: применение ручных корректировок признаков к сводной ОСВ.

Ставится между шагом 12 (разбиение 84) и шагом 13 (сборка расшифровки
баланса) — последним перед маппингом на счёт ФО.

Зачем: выгрузки 1С иногда содержат неверные признаки. Классический случай —
на договоре по счёту 60 «инвест_договор» помечен «нет», а по факту «да».
Ключ маппинга (BalanceReportConstants.MAPPING_KEYS) строится из признаков
ОСВ, поэтому ошибка молча уезжает в счёт_фо: справочник «Меппинг_бб» исправен,
комбинация с признаком «нет» в нём есть, шаг 13 доволен, отчёт «зелёный»,
а сумма попала не в ту статью.

Что делает: применяет правила файла `_INPUT_DATA/_corrections/Правки.xlsx`
(см. io_module/corrections.py и pipeline/steps/_corrections_base.py).
Вся бизнес-логика — в миксине; шаг задаёт только рамку: какой кадр, какая
область, какие признаки допустимы.
"""

from pipeline.base import Step
from pipeline.step_config import (
    BalanceReportConstants,
    ManualCorrectionsConstants as MC,
    OpuReportConstants,
)
from pipeline.steps._corrections_base import ManualCorrectionsMixin


class Step12aApplyCorrectionsBalanceStep(ManualCorrectionsMixin, Step):
    """Шаг 12а: ручные корректировки признаков сводной ОСВ (до меппинга баланса)."""

    CORRECTION_AREA = MC.AREA_BALANCE
    CORRECTION_AREA_LABEL = "баланс"
    CORRECTION_TARGET_ATTR = "summary_osv_df"
    CORRECTION_TARGET_LABEL = "сводная ОСВ"
    CORRECTION_AMOUNT_COLS = (OpuReportConstants.AMOUNT_COL, BalanceReportConstants.BALANCE_COL)

    # Признаки баланса, которые реально меняют счёт_фо. «счет» и «субконто»
    # исключены осознанно: это ключ строки ОСВ, а не классификация. Правка
    # «счёта» переименовала бы позицию вместо того, чтобы перенести её
    # в другую статью, — это уже другое действие (и не то, о котором файл).
    CORRECTION_ALLOWED_FEATURES = tuple(
        key for key in BalanceReportConstants.MAPPING_KEYS if key not in ("счет", "субконто")
    )

    # «Кто именно задет»: уровни детализации ОСВ. Считаются не по наличию в
    # Меппинге, а по тому, что реально лежит в выгрузке — субконто может быть
    # видом расчётов (60) или договором (76.07), допсубконто — контрагентом,
    # а у части счетов субконто пуст. Показываем все четыре: бухгалтеру нужна
    # возможность увидеть задетую строку целиком.
    CORRECTION_DETAIL_COLUMNS = ("счет", "субконто", "допсубконто", "договор")

    def __init__(self):
        super().__init__(
            name="Шаг 12а: Ручные корректировки признаков (баланс)",
            description="Применение правил файла Правки.xlsx к сводной ОСВ",
        )
