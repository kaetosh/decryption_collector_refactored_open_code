"""
Шаг 5: Добавление подвида задолженности.
"""
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.classifiers import ReceivableClassifier


class Step5AddReceivableSubtypeStep(Step):
    """Шаг 5: Добавление подвида задолженности."""
    
    def __init__(self):
        super().__init__(
            name="Шаг 5: Идентификация подвида задолженности",
            description="Детальная классификация задолженности: торговая ДЗ/КЗ, авансы, прочая ДЗ/КЗ и т.д."
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Добавление подвида задолженности")
        
        osv_all_df = context.main_df.copy()
        mapping_df = context.data.get('mapping')
        
        if mapping_df is None:
            raise ValueError("Меппинг отсутствует в контексте")
        
        # Используем методы из ReceivableClassifier
        subtype_mapping = ReceivableClassifier.get_subtype_mapping(mapping_df)
        osv_all_df = ReceivableClassifier.merge_subtypes(osv_all_df, subtype_mapping)
        osv_all_df = ReceivableClassifier.handle_missing_subtypes(osv_all_df)  # ← Здесь может всплыть MissingSubtypeError
        osv_all_df = ReceivableClassifier.apply_categorical_subtype(osv_all_df, mapping_df)
        
        context.main_df = osv_all_df
        return context