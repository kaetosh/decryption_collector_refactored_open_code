"""
Шаг 5: Добавление подвида задолженности.
"""
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.classifiers import ReceivableClassifier
from pipeline.errors import ReferenceMismatchError


class Step5AddReceivableSubtypeStep(Step):
    """Шаг 5: Добавление подвида задолженности."""
    
    def __init__(self):
        super().__init__(
            name="Шаг 5: Идентификация подвида задолженности",
            description="Детальная классификация задолженности: торговая ДЗ/КЗ, авансы, прочая ДЗ/КЗ и т.д."
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Добавление подвида задолженности")
        
        osv_all_df = context.summary_osv_df.copy()
        mapping_df = context.references["меппинг_баланс"]
        
        if mapping_df is None:
            raise ReferenceMismatchError(
                "Меппинг отсутствует в контексте",
                reference_name='Меппинг_бб',
            )
        
        # Используем методы из ReceivableClassifier
        subtype_mapping = ReceivableClassifier.get_subtype_mapping(mapping_df)
        osv_all_df = ReceivableClassifier.merge_subtypes(osv_all_df, subtype_mapping)
        osv_all_df = ReceivableClassifier.handle_missing_subtypes(osv_all_df)  # ← Здесь может всплыть MissingSubtypeError
        osv_all_df = ReceivableClassifier.apply_categorical_subtype(osv_all_df, mapping_df)
        
        context.summary_osv_df = osv_all_df
        
        subtype_counts = osv_all_df['подвид_задолженности'].value_counts().to_dict()
        total_records = sum(subtype_counts.values())
        subtypes_without_unknown = {k: v for k, v in subtype_counts.items() if k != 'не_указано'}
        logger.info(
            "[OK] Определены подвиды задолженности: {} видов (всего {} записей)",
            len(subtypes_without_unknown), total_records
        )
        for subtype, count in sorted(subtypes_without_unknown.items(), key=lambda x: -x[1]):
            logger.info("  {} — {}", subtype, count)
        return context