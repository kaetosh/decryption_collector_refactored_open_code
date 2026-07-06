# -*- coding: utf-8 -*-
"""
Created on Mon Jul  6 18:02:05 2026

@author: a.karabedyan
"""
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext

class Step15AddAdminExpensesToOpuStep(Step):
    """
    Шаг 15: Обработка среза из сводного отчета по проводкам по 90.08,
    управленческие расходы, разделение на ГАП/ГСК и третьи лица,
    добавление обработанных строк к расшифровке ОПУ
    """
    # Счета для обработки на этом шаге
    ACCOUNTS_ADMIN_EXPENSES = ['90.08']
    
    # Допуск для проверки сходимости с ОСВ (в тыс.ед.)
    TOLERANCE_OSV = 1000
    
    def __init__(self):
        super().__init__(
            name="Шаг 15: Управленческие расходы",
            description="Добавление движений по 90.08 счету, разбивка по видам связи КА и сегментам"
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки."""
        logger.debug("Начало формирования основы расшифровки ОПУ")
        
        name_company = context.get_metadata('company_name')
        
        # 1. Загрузка и подготовка данных
        
        # общая осв, чтобы сверить обороты по 90.08
        osv_df = context.data.get('osv', pd.DataFrame())
        
        if osv_df.empty:
            raise ValueError(
                "В контексте нет общей ОСВ. "
                "Убедитесь, что предыдущие шаги (1-13) выполнены успешно."
            )
        
        transactions_all_df = context.data.get('transactions_all_df', pd.DataFrame())
        
        if transactions_all_df.empty:
            raise ValueError(
                "В контексте нет сводного отчета по проводкам. "
                "Убедитесь, что предыдущий шаг (14) выполнен успешно."
            )
        transactions_all_df.to_parquet('intermediate_data.parquet', engine='pyarrow')
        # df26 = transactions_all_df.loc[transactions_all_df[]]