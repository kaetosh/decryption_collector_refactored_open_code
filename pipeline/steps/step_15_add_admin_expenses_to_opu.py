# -*- coding: utf-8 -*-
"""
Created on Mon Jul  6 18:02:05 2026

@author: a.karabedyan
"""
import numpy as np
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext
from io_module import DataLoader

# ПРОВЕРЯТЬ НА ДУБЛИРУЮЩИЕ ОБОРОТЫ ПО ОТЧЕТАМ 90.08 И 26, 90.07 И 44

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
        
        # Загрузка и подготовка данных
        
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
        # transactions_all_df.to_parquet('intermediate_data.parquet', engine='pyarrow')
        # Получим проводки Дт 26 Кт 60,76 - по контрагентам мы можем определить сегмент
        df26=transactions_all_df.loc[(transactions_all_df['Дт'].str.startswith('26') & transactions_all_df['Кт'].str.startswith(('60', '76')))]
        
        # Оставляем необходимые столбцы
        df26_clean = df26.loc[:, ['Субконто Кт_1', 'Сумма']]
        
        # Переименовываем столцы
        df26_clean = df26_clean.rename(columns={'Субконто Кт_1': 'контрагент', 'Сумма': 'оборот, тыс.ед.'})
        
        # Переводим значения в тысячи
        df26_clean.loc[:, 'оборот, тыс.ед.'] = df26_clean.loc[:, 'оборот, тыс.ед.']/1000
        
        # группа_ка и сегмент_ка (из group_companies_df по контрагенту)
        group_companies_df = DataLoader.load_reference_data(
            sheet_name='ВидСвязиКА',
            strings=['ВидСвязиКА', 'сегмент', 'ВариантыНазвания']
        )
        group_unique = group_companies_df.drop_duplicates(subset='ВариантыНазвания')
        mapping_group = group_unique.set_index('ВариантыНазвания')['ВидСвязиКА'].astype('string')
        mapping_segment_ka = group_unique.set_index('ВариантыНазвания')['сегмент'].astype('string')
        
        df26_clean['группа_ка'] = df26_clean['контрагент'].map(mapping_group).fillna('3 лица').astype('string')
        df26_clean['сегмент_ка'] = df26_clean['контрагент'].map(mapping_segment_ka).fillna('3 лица').astype('string')
        
        df26_clean = df26_clean.groupby(['группа_ка', 'сегмент_ка', 'контрагент'], as_index=False)['оборот, тыс.ед.'].sum()
        
        # Получим проводки Дт 90.08
        df_9008 = transactions_all_df.loc[transactions_all_df['Дт'].str.startswith('90.08')]
        df_9008 = df_9008.loc[:, ['Субконто Дт_1', 'Сумма']]
        df_9008 = df_9008.groupby('Субконто Дт_1', as_index=False)['Сумма'].sum()
        df_9008['оборот, тыс.ед.'] = df_9008.loc[:, 'Сумма']/1000
        df_9008 = df_9008.rename(columns={'Субконто Дт_1': 'ном_группа'})
        df_9008 = df_9008.loc[:, ['ном_группа', 'оборот, тыс.ед.']]
        
        directory_ufr_df = DataLoader.load_reference_data(
            sheet_name='СправочникУФР',
            strings=['строка_уфр', 'сегмент',
                    'сокращенное_наименование_компании', 'ном_группа_1с']
        )
        directory_ufr_df = directory_ufr_df.loc[
            directory_ufr_df["сокращенное_наименование_компании"] == name_company
        ]
        directory_ufr_df = self.clean_whitespace(directory_ufr_df)
        
        mapping_segment = (
            directory_ufr_df
            .drop_duplicates(subset='ном_группа_1с')
            .set_index('ном_группа_1с')['сегмент']
        )
        df_9008['сегмент'] = df_9008['ном_группа'].map(mapping_segment).astype('string')
        
        df_9008.to_parquet('df_9008.parquet', engine='pyarrow')
        
        # 1. Расчёт долей ном_групп в общих расходах
        total_9008 = df_9008['оборот, тыс.ед.'].sum()
        df_9008['доля_ном_группы'] = df_9008['оборот, тыс.ед.'] / total_9008
        
        # 2. Cross-join: каждая строка df26_clean × каждая ном_группа
        df_cross = df26_clean.assign(key=1).merge(
            df_9008[['ном_группа', 'сегмент', 'доля_ном_группы']].assign(key=1), 
            on='key'
        ).drop('key', axis=1)
        
        # 3. Распределение оборота пропорционально долям ном_групп
        df_cross['оборот_распределенный'] = df_cross['оборот, тыс.ед.'] * df_cross['доля_ном_группы']
        
        # 4. Определение вид_связи (векторизованно через np.select)
        conditions = [
            df_cross['группа_ка'] == '3 лица',
            df_cross['группа_ка'] == 'Прочие ГАП',
            (df_cross['группа_ка'] == 'ГСК') & (df_cross['сегмент_ка'] == df_cross['сегмент']),
            (df_cross['группа_ка'] == 'ГСК') & (df_cross['сегмент_ка'] != df_cross['сегмент']),
        ]
        choices = ['3 лица', 'Прочие ГАП', 'ГСК', 'межсегмент']
        df_cross['вид_связи'] = np.select(conditions, choices, default='не_указано')
        
        # 5. Добавление остатка (расходы без контрагентов: зарплата, материалы и т.д.)
        total_26_clean = df26_clean['оборот, тыс.ед.'].sum()
        remainder = total_9008 - total_26_clean
        
        if remainder > 0:
            df_remainder = df_9008[['ном_группа', 'сегмент', 'доля_ном_группы']].copy()
            df_remainder['контрагент'] = 'Прочие расходы'
            df_remainder['группа_ка'] = '3 лица'
            df_remainder['сегмент_ка'] = '3 лица'
            df_remainder['вид_связи'] = '3 лица'
            df_remainder['оборот_распределенный'] = remainder * df_remainder['доля_ном_группы']
            
            df_result = pd.concat([df_cross, df_remainder], ignore_index=True)
        else:
            df_result = df_cross
        
        # 6. Финальная очистка и переименование
        df_result = df_result.drop(columns=['оборот, тыс.ед.', 'доля_ном_группы'])
        df_result = df_result.rename(columns={'оборот_распределенный': 'оборот, тыс.ед.'})
        
        # Приведение типов к string
        text_cols = ['ном_группа', 'сегмент', 'контрагент', 'группа_ка', 'сегмент_ка', 'вид_связи']
        for col in text_cols:
            if col in df_result.columns:
                df_result[col] = df_result[col].astype('string')
        