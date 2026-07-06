"""
Шаг 4: Добавление вида задолженности (ДЗ/КЗ).
"""
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.classifiers import ReceivableClassifier

class Step4AddReceivableTypeStep(Step):
    """Шаг 4: Добавление вида задолженности (ДЗ/КЗ)."""
    
    def __init__(self):
        super().__init__(
            name="Шаг 4: Идентификация ДЗ/КЗ",
            description="Классификация ДЗ/КЗ на основе меппинга"
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Классификация задолженности")
        
        osv_all_df = context.main_df.copy()
        partially_matching_accounts_df = context.data.get('partially_matching_accounts_df')
        mapping_df = context.data.get('mapping')
        
        if partially_matching_accounts_df is None or mapping_df is None:
            raise ValueError("Необходимые данные отсутствуют в контексте (partially_matching_accounts_df)")
        
        # Используем методы из ReceivableClassifier
        osv_all_df = ReceivableClassifier.map_accounts_to_mapping(osv_all_df, partially_matching_accounts_df)
        accounts_with_debt_type = ReceivableClassifier.get_accounts_with_debt_type(mapping_df)
        osv_all_df = ReceivableClassifier.classify_debt_type(osv_all_df, accounts_with_debt_type)
        osv_all_df = ReceivableClassifier.handle_special_cases(osv_all_df)
        osv_all_df = ReceivableClassifier.clean_subaccounts(osv_all_df, mapping_df)
        
        context.main_df = osv_all_df
        return context

