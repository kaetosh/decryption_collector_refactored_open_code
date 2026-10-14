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
        
        osv_all_df = context.summary_osv_df.copy()
        # Сопоставление счетов ОСВ из шага 1а (после паузы пересобрано по
        # обновлённому Меппинг_бб — см. executors._refresh_derived_reference_data).
        partially_matching_accounts_df = self.get_partial_account_matches(context)
        # Меппинг берём из context.references, а не из снимка в context.data:
        # refresh_references обновляет только references, и после паузы на
        # обновлённом справочнике снимок остался бы старым (шаг 11 падал с
        # теми же позициями, которые пользователь уже исправил).
        mapping_df = context.references['меппинг_баланс']
        
        # Используем методы из ReceivableClassifier
        osv_all_df = ReceivableClassifier.map_accounts_to_mapping(osv_all_df, partially_matching_accounts_df)
        accounts_with_debt_type = ReceivableClassifier.get_accounts_with_debt_type(mapping_df)
        osv_all_df = ReceivableClassifier.classify_debt_type(osv_all_df, accounts_with_debt_type)
        osv_all_df = ReceivableClassifier.handle_special_cases(osv_all_df, context)
        osv_all_df = ReceivableClassifier.clean_subaccounts(osv_all_df, mapping_df)
        
        context.summary_osv_df = osv_all_df
        
        debt_counts = osv_all_df['вид_задолженности'].value_counts().to_dict()
        logger.info(
            "[OK] Классифицирована задолженность: {}",
            ', '.join(f'{k} — {v}' for k, v in sorted(debt_counts.items(), key=lambda x: -x[1]))
        )
        return context

