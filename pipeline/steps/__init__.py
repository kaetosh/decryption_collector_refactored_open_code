# pipeline/steps/__init__.py
"""
Экспорт всех шагов конвейера обработки.
"""
from pipeline.steps.step_01a_list_registers import Step1aListExpectedRegistersStep
from pipeline.steps.step_01b_verify_files import Step1bVerifyFilesStep
from pipeline.steps.step_02_flat_osv import Step2FlatSummaryOSVStep
from pipeline.steps.step_03_add_account import Step3AddAccountColumnStep
from pipeline.steps.step_04_add_debt_type import Step4AddReceivableTypeStep
from pipeline.steps.step_05_add_debt_subtype import Step5AddReceivableSubtypeStep
from pipeline.steps.step_06_add_os_group import Step6AddOSGroupColumnStep
from pipeline.steps.step_07_add_long_short import Step7AddLongShortTermColumnStep
from pipeline.steps.step_08_add_bioactive import Step8AddBioactiveSegmentColumnStep
from pipeline.steps.step_09_add_related_party import Step9AddRelatedPartyTypeColumnStep
from pipeline.steps.step_10_classify_lease import Step10ClassifyLeaseSourceStep
from pipeline.steps.step_11_split_60 import Step11Split60AccountDebtByOSStatusStep
from pipeline.steps.step_11a_check_contractor_similarity import Step11aCheckContractorSimilarityStep  # ← НОВОЕ
from pipeline.steps.step_12_split_84 import Step12Split84AccountBalanceStep
from pipeline.steps.step_13_build_balance import Step13BuildBalanceBreakdownStep
from pipeline.steps.step_14_build_opu_foundation import Step14BuildOpuFoundationStep
from pipeline.steps.step_15_add_admin_expenses_to_opu import Step15AddAdminExpensesToOpuStep


__all__ = [
    'Step1aListExpectedRegistersStep',
    'Step1bVerifyFilesStep',
    'Step2FlatSummaryOSVStep',
    'Step3AddAccountColumnStep',
    'Step4AddReceivableTypeStep',
    'Step5AddReceivableSubtypeStep',
    'Step6AddOSGroupColumnStep',
    'Step7AddLongShortTermColumnStep',
    'Step8AddBioactiveSegmentColumnStep',
    'Step9AddRelatedPartyTypeColumnStep',
    'Step10ClassifyLeaseSourceStep',
    'Step11Split60AccountDebtByOSStatusStep',
    'Step11aCheckContractorSimilarityStep',
    'Step12Split84AccountBalanceStep',
    'Step13BuildBalanceBreakdownStep',
    'Step14BuildOpuFoundationStep',
    'Step15AddAdminExpensesToOpuStep'
]