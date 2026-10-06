# -*- coding: utf-8 -*-
"""Шаг 18а: применение ручных корректировок признаков к журналу ОПУ.

Ставится между шагом 18 (налог и прочие движения) и шагом 19 (сборка
расшифровки ОПУ) — последним перед маппингом журнала на счёт ФО.

Зачем: у журнала ОПУ ключ маппинга короче, чем у баланса
(OpuReportConstants.MAPPING_KEY_COLS — «счет», «вид_дохода_расхода»,
«сегмент», «вид_связи»), но принцип тот же: выгрузка 1С может нести неверный
признак, и строка уходит в неверную статью отчёта. Причём здесь править можно
точнее — в журнале есть «контрагент» и «номер_группы», поэтому правило удобно
писать «по этому контрагенту», а не «по всем строкам счёта 91.02».

Почему шаг именно здесь, а не раньше: шаг 18 безусловно нормализует журнал
(«вид_связи» → «3 лица», обрезка «счет» до 5 знаков) при любом наличии
оборотов по счёту 99. Правка, сделанная до шага 18, была бы затёрта его
нормализацией, а сделанная после 19 — попала бы в уже собранный отчёт.

Что делает: применяет правила файла `_INPUT_DATA/_corrections/Правки.xlsx`
с областью «опу» (см. io_module/corrections.py и
pipeline/steps/_corrections_base.py). Вся бизнес-логика — в миксине.
"""

from pipeline.base import Step
from pipeline.step_config import (
    ManualCorrectionsConstants as MC,
    OpuReportConstants,
)
from pipeline.steps._corrections_base import ManualCorrectionsMixin


class Step18aApplyCorrectionsOpuStep(ManualCorrectionsMixin, Step):
    """Шаг 18а: ручные корректировки признаков журнала ОПУ (до меппинга ОПУ)."""

    CORRECTION_AREA = MC.AREA_OPU
    CORRECTION_AREA_LABEL = "ОПУ"
    CORRECTION_TARGET_ATTR = "journal_df"
    CORRECTION_TARGET_LABEL = "журнал ОПУ"
    CORRECTION_AMOUNT_COLS = (OpuReportConstants.AMOUNT_COL, OpuReportConstants.RUB_AMOUNT_COL)

    # «счет» — ключ строки журнала, а не классификация: править его означало бы
    # переименовать проводку, а не перенести сумму в другую статью
    CORRECTION_ALLOWED_FEATURES = tuple(
        key for key in OpuReportConstants.MAPPING_KEY_COLS if key != "счет"
    )

    # «Кто именно задет» в журнале ОПУ: контрагент и номер группы — два
    # уровня детализации проводок, по ним правило удобно сужать точечно.
    CORRECTION_DETAIL_COLUMNS = ("счет", "контрагент", "ном_группа")

    def __init__(self):
        super().__init__(
            name="Шаг 18а: Ручные корректировки признаков (ОПУ)",
            description="Применение правил файла Правки.xlsx к журналу ОПУ",
        )
