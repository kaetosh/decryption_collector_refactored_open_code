# -*- coding: utf-8 -*-
"""
Шаг 14: Формирование основы расшифровки ОПУ — выручка и себестоимость (90.01/90.02)
"""
import numpy as np
import pandas as pd
from loguru import logger
from typing import Tuple

from pipeline.base import Step, ProcessingContext
from pipeline.errors import MissingMappingError, ReferenceMismatchError
from io_module import DataLoader
from config.settings import REFERENCE_CONFIGS


class Step14BuildOpuFoundationStep(Step):
    """
    Шаг 14: Формирование основы расшифровки ОПУ —
    сбор выручки и себестоимости из проводок 90.01/90.02.
    
    Создаёт первичную таблицу расшифровки ОПУ, которая будет
    дополняться данными в последующих шагах:
    - Шаг 15: коммерческие расходы (90.07)
    - Шаг 16: управленческие расходы (90.08)
    - Шаг 17: прочие доходы/расходы (91.01/91.02)
    - Шаг 18: налог на прибыль (99)
    """
    
    # Счета для обработки на этом шаге
    ACCOUNTS_REVENUE_COST = ['90.01', '90.02']
    
    # Допуск для проверки сходимости с ОСВ (в тыс.ед.)
    TOLERANCE_OSV = 1000
    
    def __init__(self):
        super().__init__(
            name="Шаг 14: Формирование основы расшифровки ОПУ — выручка и себестоимость (90.01/90.02)",
            description="Создание первичной таблицы ОПУ из отчёта по проводкам 90 счета"
        )
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки."""
        logger.debug("Начало формирования основы расшифровки ОПУ")
        
        name_company = context.get_metadata('company_name')
        
        # 1. Загрузка и подготовка данных
        transactions_all_df = self._load_transactions()
        osv_df = self._get_osv_from_context(context)
        
        # Сохраним сводный отчет по проводкам для использование в следующих шагах
        context.data['transactions_all_df'] = transactions_all_df
        
        # 2. Обработка выручки (90.01)
        df9001 = self._process_revenue_9001(transactions_all_df, osv_df)
        
        # 3. Обработка себестоимости (90.02) с выделением переоценки
        df9002, df9002_16 = self._process_cost_9002(
            transactions_all_df, osv_df, name_company
        )
        
        # 4. Распределение себестоимости на контрагентов
        df_result = self._distribute_cost_to_buyers(df9001, df9002)
        
        # 5. Преобразование в длинный формат (melt)
        df_result = self._reshape_to_long_format(df_result)
        
        # 6. Загрузка и сохранение справочников в context
        mapping_opu_df, directory_ufr_df, group_companies_df = self._load_reference_data(name_company)
        context.data['mapping_opu'] = mapping_opu_df
        
        # 7. Обогащение данными из справочников
        df_result = self._enrich_with_mappings(
            df_result, mapping_opu_df, directory_ufr_df, group_companies_df
        )
        
        # 8. Объединение с переоценкой (df9002_16)
        df_final = self._merge_with_reassessment(df_result, df9002_16)
                
        # Обновляем context
        context.main_df = df_final
        df_final.to_parquet('df_final.parquet', engine='pyarrow')
        
        logger.info(
            f"✓ Основа ОПУ сформирована: {len(df_final)} строк "
            f"({df_final['счет'].value_counts().to_dict()})"
        )
        
        return context
    
    # =========================================================================
    # ЗАГРУЗКА ДАННЫХ
    # =========================================================================
    
    def _load_transactions(self) -> pd.DataFrame:
        """Загружает общий отчёт по проводкам."""
        logger.debug("Загрузка отчёта по проводкам")
        transactions_all_df = DataLoader.load_transaction_report()
        transactions_all_df = self.clean_whitespace(transactions_all_df)
        
        logger.debug(
            f"Загружено {len(transactions_all_df)} проводок, "
            f"{transactions_all_df['Кт'].nunique()} уникальных Кт счетов"
        )
        
        return transactions_all_df
    
    def _get_osv_from_context(self, context: ProcessingContext) -> pd.DataFrame:
        """Получает общую ОСВ из context."""
        osv_df = context.data.get('osv', pd.DataFrame())
        
        if osv_df.empty:
            raise ValueError(
                "В контексте нет общей ОСВ. "
                "Убедитесь, что предыдущие шаги (1-13) выполнены успешно."
            )
        
        return osv_df.copy()
    
    # =========================================================================
    # ОБРАБОТКА ВЫРУЧКИ (90.01)
    # =========================================================================
    
    def _process_revenue_9001(
        self, 
        transactions_all_df: pd.DataFrame, 
        osv_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Обрабатывает выручку по счету 90.01."""
        logger.debug("Обработка выручки (90.01)")
        
        # Фильтруем кредитовые обороты по 90.01
        df9001 = transactions_all_df.loc[
            transactions_all_df['Кт'].str.startswith("90.01")
        ].copy()
        
        # Переименование столбцов
        df9001 = df9001.rename(columns={
            'Субконто Дт_1': 'контрагент',
            'Субконто Кт_1': 'ном_группа',
            'Кт': 'счет'
        })
        
        # Замена контрагентов на 76.15 на "пайщики"
        df9001.loc[df9001['Дт'].str.startswith('76.15'), 'контрагент'] = 'пайщики'
        
        # Приведение ставки НДС к числовому типу
        df9001['ндс_ставка'] = (
            df9001['Субконто Кт_2']
            .astype(str)
            .str.replace('%', '', regex=False)
            .str.strip()
            .replace('', pd.NA)
        )
        df9001['ндс_ставка'] = pd.to_numeric(df9001['ндс_ставка'], errors='coerce') / 100
        
        # Очистка выручки от НДС
        df9001['выручка_без_ндс_тыс_ед'] = (df9001['Сумма'] / 1000) / (1 + df9001['ндс_ставка'])
        
        # Оставляем только нужные столбцы и группируем
        df9001 = df9001.loc[:, ['Документ', 'контрагент', 'ном_группа', 'выручка_без_ндс_тыс_ед']]
        df9001 = df9001.groupby(
            ['Документ', 'контрагент', 'ном_группа'], 
            as_index=False
        )['выручка_без_ндс_тыс_ед'].sum()
        
        # Проверка сходимости с ОСВ
        self._validate_revenue_against_osv(df9001, osv_df)
        
        logger.debug(f"Выручка обработана: {len(df9001)} строк")
        
        return df9001
    
    def _validate_revenue_against_osv(
        self, 
        df9001: pd.DataFrame, 
        osv_df: pd.DataFrame
    ) -> None:
        """Проверяет сходимость выручки с общей ОСВ."""
        revenue_osv_9001 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.01'), 'Кредит_оборот'
        ].sum()
        revenue_osv_9003 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.03'), 'Дебет_оборот'
        ].sum()
        revenue_without_vat = (revenue_osv_9001 - revenue_osv_9003) / 1000
        
        revenue_from_df9001 = df9001['выручка_без_ндс_тыс_ед'].sum()
        
        difference = abs(revenue_without_vat - revenue_from_df9001)
        
        if difference > self.TOLERANCE_OSV:
            raise ValueError(
                f"Выручка из отчёта по проводкам ({revenue_from_df9001:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({revenue_without_vat:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {self.TOLERANCE_OSV})"
            )
        
        logger.debug(
            f"✓ Сходимость выручки: ОСВ={revenue_without_vat:,.2f}, "
            f"отчёт={revenue_from_df9001:,.2f}, разница={difference:,.2f}"
        )
    
    # =========================================================================
    # ОБРАБОТКА СЕБЕСТОИМОСТИ (90.02)
    # =========================================================================
    
    def _process_cost_9002(
        self,
        transactions_all_df: pd.DataFrame,
        osv_df: pd.DataFrame,
        name_company: str
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Обрабатывает себестоимость по счету 90.02.
        
        Выделяет отдельно переоценку активов (Дт 90.02 Кт 16) в df9002_16.
        
        Returns:
            Tuple[df9002, df9002_16]: Основная себестоимость и переоценка
        """
        logger.debug("Обработка себестоимости (90.02)")
        
        # Фильтруем дебетовые обороты по 90.02
        df9002 = transactions_all_df.loc[
            transactions_all_df['Дт'].str.startswith("90.02")
        ].copy()
        
        df9002 = df9002.rename(columns={
            'Субконто Дт_1': 'ном_группа',
            'Дт': 'счет'
        })
        
        # Разделение на основную себестоимость и переоценку (16 счет)
        mask_16 = df9002['Кт'].astype(str).str.startswith('16', na=False)
        
        if mask_16.any():
            df9002_16 = self._process_reassessment(df9002[mask_16].copy(), name_company)
            df9002 = df9002[~mask_16].copy()
            turn_df9002_16 = df9002_16['оборот, тыс.ед.'].sum()
        else:
            df9002_16 = pd.DataFrame()
            turn_df9002_16 = 0
        
        # Обработка основной себестоимости
        df9002['себестоимость_тыс_ед'] = df9002.loc[:, 'Сумма'] / 1000
        df9002 = df9002.loc[:, ['Документ', 'ном_группа', 'себестоимость_тыс_ед']]
        df9002 = df9002.groupby(
            ['Документ', 'ном_группа'], 
            as_index=False
        )['себестоимость_тыс_ед'].sum()
        
        # Проверка сходимости с ОСВ
        self._validate_cost_against_osv(df9002, osv_df, turn_df9002_16)
        
        logger.debug(
            f"Себестоимость обработана: {len(df9002)} строк основной, "
            f"{len(df9002_16)} строк переоценки"
        )
        
        return df9002, df9002_16
    
    def _process_reassessment(
        self, 
        df9002_16: pd.DataFrame, 
        name_company: str
    ) -> pd.DataFrame:
        """Обрабатывает переоценку активов (Дт 90.02 Кт 16)."""
        logger.debug("Обработка переоценки активов (Дт 90.02 Кт 16)")
        
        # Расчёт оборота
        df9002_16['оборот, тыс.ед.'] = df9002_16.loc[:, 'Сумма'] / 1000
        df9002_16 = df9002_16.groupby(['ном_группа'], as_index=False)['оборот, тыс.ед.'].sum()
        
        # Установка признаков
        for col_name, value in [
            ('счет', '90.02'),
            ('доход_расход', 'Изменение в оценке'),
            ('сегмент', 'не_указано'),
            ('вид_связи', 'не_указано'),
        ]:
            df9002_16[col_name] = pd.Series([value] * len(df9002_16), dtype='string')
        
        # Загрузка справочника для определения вида_дохода_расхода
        company_directory_df = DataLoader.load_reference_data(
            sheet_name='КомпанииГруппы',
            strings=['вид_продукции_переоценки', 'сокращенное_наименование_компании']
        )
        
        matching_rows = company_directory_df[
            company_directory_df['сокращенное_наименование_компании'] == name_company
        ]
        
        # Проверка: компания не найдена
        if matching_rows.empty:
            problem_data = (
                company_directory_df[['сокращенное_наименование_компании']]
                .drop_duplicates()
                .rename(columns={'сокращенное_наименование_компании': 'компания_в_справочнике'})
            )
            raise ReferenceMismatchError(
                message=f"Компания '{name_company}' не найдена в справочнике",
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                searched_company=name_company,
            )
        
        # Проверка: дубликаты
        if len(matching_rows) > 1:
            raise ReferenceMismatchError(
                message=(
                    f"У компании '{name_company}' найдено {len(matching_rows)} записей. "
                    f"Ожидается одна."
                ),
                problem_data=matching_rows.copy(),
                reference_name="КомпанииГруппы",
                duplicate_count=len(matching_rows),
            )
        
        # Установка вида_дохода_расхода
        products_reassessment_type = matching_rows['вид_продукции_переоценки'].iloc[0]
        df9002_16['вид_дохода_расхода'] = products_reassessment_type
        
        logger.debug(
            f"Переоценка: {len(df9002_16)} строк, "
            f"вид_дохода_расхода='{products_reassessment_type}'"
        )
        
        return df9002_16
    
    def _validate_cost_against_osv(
        self,
        df9002: pd.DataFrame,
        osv_df: pd.DataFrame,
        turn_df9002_16: float
    ) -> None:
        """Проверяет сходимость себестоимости с общей ОСВ."""
        cost_price_osv_9002 = osv_df.loc[
            osv_df['Счет'].str.startswith('90.02'), 'Дебет_оборот'
        ].sum() / 1000
        
        cost_price_from_df9002 = df9002['себестоимость_тыс_ед'].sum() + turn_df9002_16
        
        difference = abs(cost_price_osv_9002 - cost_price_from_df9002)
        
        if difference > self.TOLERANCE_OSV:
            raise ValueError(
                f"Себестоимость из отчёта по проводкам ({cost_price_from_df9002:,.2f} тыс.ед.) "
                f"отличается от общей ОСВ ({cost_price_osv_9002:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {self.TOLERANCE_OSV})"
            )
        
        logger.debug(
            f"✓ Сходимость себестоимости: ОСВ={cost_price_osv_9002:,.2f}, "
            f"отчёт={cost_price_from_df9002:,.2f}, разница={difference:,.2f}"
        )
    
    # =========================================================================
    # РАСПРЕДЕЛЕНИЕ СЕБЕСТОИМОСТИ
    # =========================================================================
    
    def _distribute_cost_to_buyers(
        self, 
        df9001: pd.DataFrame, 
        df9002: pd.DataFrame
    ) -> pd.DataFrame:
        """Распределяет себестоимость на контрагентов пропорционально выручке."""
        logger.debug("Распределение себестоимости на контрагентов")
        
        # Объединение выручки и себестоимости
        df_merged = df9001.merge(df9002, on=['Документ', 'ном_группа'], how='outer')
        
        # Маска для пустых покупателей (себестоимость без выручки)
        mask_empty = df_merged['контрагент'].isna() | (df_merged['контрагент'] == '')
        
        # Оставляем только строки с покупателями
        df_buyers = df_merged[~mask_empty].copy()
        
        # Выручка по группам (транслируется на каждую строку)
        revenue_sum = df_buyers.groupby('ном_группа')['выручка_без_ндс_тыс_ед'].transform('sum')
        
        # Затраты к распределению (из строк без покупателей)
        cost_to_distribute = df_merged[mask_empty].groupby('ном_группа')['себестоимость_тыс_ед'].sum()
        
        # Маппинг затрат на строки с покупателями
        df_buyers['затраты_группы'] = df_buyers['ном_группа'].map(cost_to_distribute).fillna(0)
        
        # Коэффициент распределения (защита от деления на 0)
        ratio = np.where(revenue_sum > 0, df_buyers['затраты_группы'] / revenue_sum, 0)
        
        # Итоговая себестоимость
        df_buyers['Итоговая_себестоимость'] = (
            df_buyers['себестоимость_тыс_ед'].fillna(0) +
            df_buyers['выручка_без_ндс_тыс_ед'] * ratio
        )
        
        df_result = df_buyers.drop(columns=['затраты_группы']).reset_index(drop=True)
        
        # Группировка по контрагенту и ном_группе (без разбивки по документам)
        df_result = df_result.groupby(
            ['контрагент', 'ном_группа']
        )[['выручка_без_ндс_тыс_ед', 'Итоговая_себестоимость']].sum().reset_index()
        
        df_result = df_result.rename(columns={'Итоговая_себестоимость': 'себестоимость_тыс_ед'})
        
        distributed_count = mask_empty.sum()
        logger.debug(
            f"Распределено {distributed_count} строк себестоимости без покупателей "
            f"на {len(df_result)} строк с покупателями"
        )
        
        return df_result
    
    # =========================================================================
    # ПРЕОБРАЗОВАНИЕ ФОРМАТА
    # =========================================================================
    
    def _reshape_to_long_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Преобразует DataFrame в длинный формат (melt)."""
        logger.debug("Преобразование в длинный формат")
        
        # Инвертируем знак выручки (делаем отрицательной)
        df['выручка_без_ндс_тыс_ед'] = -df['выручка_без_ндс_тыс_ед']
        
        # Melt
        df_long = df.melt(
            id_vars=['контрагент', 'ном_группа'],
            value_vars=['выручка_без_ндс_тыс_ед', 'себестоимость_тыс_ед'],
            var_name='счет',
            value_name='оборот, тыс.ед.'
        )
        
        # Маппинг имён столбцов на номера счетов
        account_mapping = {
            'выручка_без_ндс_тыс_ед': '90.01',
            'себестоимость_тыс_ед': '90.02'
        }
        
        # ★ ЯВНОЕ ПРИВЕДЕНИЕ К STRING: map возвращает object, нужен string
        df_long['счет'] = df_long['счет'].map(account_mapping).astype('string')
        
        # Удаление нулевых оборотов
        df_long = df_long[df_long['оборот, тыс.ед.'] != 0].reset_index(drop=True)
        
        logger.debug(f"После melt: {len(df_long)} строк")
        
        return df_long
    
    # =========================================================================
    # ЗАГРУЗКА СПРАВОЧНИКОВ
    # =========================================================================
    
    def _load_reference_data(
        self, 
        name_company: str
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Загружает все необходимые справочники."""
        logger.debug("Загрузка справочников")
        
        # 1. Справочник УФР
        directory_ufr_df = DataLoader.load_reference_data(
            sheet_name='СправочникУФР',
            strings=['строка_уфр', 'сегмент',
                    'сокращенное_наименование_компании', 'ном_группа_1с']
        )
        directory_ufr_df = directory_ufr_df.loc[
            directory_ufr_df["сокращенное_наименование_компании"] == name_company
        ]
        directory_ufr_df = self.clean_whitespace(directory_ufr_df)
        
        # 2. Меппинг ОПУ
        mapping_opu_df = DataLoader.load_reference_data(
            'Меппинг_опу', 
            **REFERENCE_CONFIGS['Меппинг_опу']
        )
        
        # 3. Виды связей КА
        group_companies_df = DataLoader.load_reference_data(
            sheet_name='ВидСвязиКА',
            strings=['ВидСвязиКА', 'сегмент', 'ВариантыНазвания']
        )

        logger.debug(
            f"Загружено: УФР={len(directory_ufr_df)}, "
            f"МеппингОПУ={len(mapping_opu_df)}, "
            f"ВидСвязиКА={len(group_companies_df)}"
        )
        
        return mapping_opu_df, directory_ufr_df, group_companies_df
    
    # =========================================================================
    # ОБОГАЩЕНИЕ ДАННЫМИ ИЗ СПРАВОЧНИКОВ
    # =========================================================================
    
    def _enrich_with_mappings(
        self,
        df_result: pd.DataFrame,
        mapping_opu_df: pd.DataFrame,
        directory_ufr_df: pd.DataFrame,
        group_companies_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Обогащает DataFrame данными из справочников."""
        logger.debug("Обогащение данными из справочников")
        
        # 1. доход_расход (из mapping_opu_df по счету)
        mapping_account = (
            mapping_opu_df
            .drop_duplicates(subset='счет')
            .set_index('счет')['доход_расход']
        )
        df_result['доход_расход'] = df_result['счет'].map(mapping_account).astype('string')
        
        # 2. вид_дохода_расхода (из directory_ufr_df по составному ключу счет+ном_группа)
        directory_ufr_df['_key'] = (
            directory_ufr_df['счет'].astype(str) + '_' +
            directory_ufr_df['ном_группа_1с'].astype(str)
        )
        df_result['_key'] = (
            df_result['счет'].astype(str) + '_' +
            df_result['ном_группа'].astype(str)
        )
        
        mapping_revenue = (
            directory_ufr_df
            .drop_duplicates(subset='_key')
            .set_index('_key')['строка_уфр']
        )
        df_result['вид_дохода_расхода'] = df_result['_key'].map(mapping_revenue).astype('string')
        
        # Удаляем временный ключ
        df_result.drop(columns='_key', inplace=True)
        directory_ufr_df.drop(columns='_key', inplace=True)
        
        # 3. сегмент (из directory_ufr_df по ном_группа)
        mapping_segment = (
            directory_ufr_df
            .drop_duplicates(subset='ном_группа_1с')
            .set_index('ном_группа_1с')['сегмент']
        )
        df_result['сегмент'] = df_result['ном_группа'].map(mapping_segment).astype('string')
        
        # Проверка: все ли ном_группы замапились
        self._validate_mapping_completeness(
            df_result, mapping_revenue, mapping_segment
        )
        
        # 4. группа_ка и сегмент_ка (из group_companies_df по контрагенту)
        group_unique = group_companies_df.drop_duplicates(subset='ВариантыНазвания')
        
        mapping_group = group_unique.set_index('ВариантыНазвания')['ВидСвязиКА'].astype('string')
        mapping_segment_ka = group_unique.set_index('ВариантыНазвания')['сегмент'].astype('string')
        
        df_result['группа_ка'] = df_result['контрагент'].map(mapping_group).fillna('3 лица').astype('string')
        df_result['сегмент_ка'] = df_result['контрагент'].map(mapping_segment_ka).fillna('3 лица').astype('string')
        
        # Проверка неожиданных значений
        self._validate_group_ka_values(df_result)
        
        # 5. вид_связи (на основе группа_ка и сегмент_ка)
        df_result['вид_связи'] = self._calculate_connection_type(df_result)
        
        logger.debug(
            f"Обогащение завершено: {df_result['вид_связи'].value_counts().to_dict()}"
        )
        
        return df_result
    
    def _validate_mapping_completeness(
        self,
        df_result: pd.DataFrame,
        mapping_revenue: pd.Series,
        mapping_segment: pd.Series
    ) -> None:
        """Проверяет полноту маппинга вид_дохода_расхода и сегмент."""
        unmapped_mask = df_result['вид_дохода_расхода'].isna() | df_result['сегмент'].isna()
        
        if unmapped_mask.any():
            problem_groups = df_result.loc[unmapped_mask, 'ном_группа'].unique()
            
            problem_data = pd.DataFrame({
                'ном_группа_без_маппинга': problem_groups,
                'строка_уфр_в_справочнике': [
                    mapping_revenue.get(g, 'ОТСУТСТВУЕТ') for g in problem_groups
                ],
                'сегмент_в_справочнике': [
                    mapping_segment.get(g, 'ОТСУТСТВУЕТ') for g in problem_groups
                ],
            })
            
            raise MissingMappingError(
                message=(
                    f"В справочнике УФР отсутствуют записи для "
                    f"{len(problem_groups)} ном_групп из отчёта по проводкам"
                ),
                problem_data=problem_data,
                reference_name="Справочник строк УФР (directory_ufr)",
            )
    
    def _validate_group_ka_values(self, df_result: pd.DataFrame) -> None:
        """Проверяет наличие неожиданных значений в группа_ка."""
        expected_groups = {'3 лица', 'Прочие ГАП', 'ГСК'}
        actual_groups = set(df_result['группа_ка'].unique())
        unexpected_groups = actual_groups - expected_groups
        
        if unexpected_groups:
            logger.warning(
                f"⚠️ В столбце 'группа_ка' обнаружены неожиданные значения: "
                f"{unexpected_groups}. Ожидались только: {expected_groups}"
            )
    
    def _calculate_connection_type(self, df_result: pd.DataFrame) -> pd.Series:
        """Рассчитывает вид_связи на основе группа_ка и сегмент_ка."""
        conditions = [
            df_result['группа_ка'] == '3 лица',
            df_result['группа_ка'] == 'Прочие ГАП',
            (df_result['группа_ка'] == 'ГСК') & (df_result['сегмент_ка'] == df_result['сегмент']),
            (df_result['группа_ка'] == 'ГСК') & (df_result['сегмент_ка'] != df_result['сегмент']),
        ]
        choices = [
            '3 лица',
            'Прочие ГАП',
            'ГСК внутрисегмент.',
            'ГСК межсегмент.',
        ]
        # ★ STRING вместо category
        result = np.select(conditions, choices, default='не_указано')
        return pd.Series(result, index=df_result.index, dtype='string')
    
    # =========================================================================
    # ОБЪЕДИНЕНИЕ РЕЗУЛЬТАТОВ
    # =========================================================================
    
    def _merge_with_reassessment(
                self,
                df_result: pd.DataFrame,
                df9002_16: pd.DataFrame
            ) -> pd.DataFrame:
        """Объединяет основной результат с переоценкой."""
        logger.debug("Объединение с переоценкой")
        
        if df9002_16.empty:
            logger.debug("Переоценка отсутствует, объединение не требуется")
            return df_result
        
        # ★ ПРОСТОЙ подход: reindex с fill_value и финальное приведение к string
        df9002_16_aligned = df9002_16.reindex(
            columns=df_result.columns, 
            fill_value="не_указано"
        )
        
        df_final = pd.concat([df_result, df9002_16_aligned], ignore_index=True)
        
        # ★ ФИНАЛЬНОЕ приведение всех текстовых столбцов к string
        # Это устраняет последствия concat (который часто понижает dtype до object)
        text_cols = [
            'счет', 'контрагент', 'ном_группа', 'доход_расход',
            'вид_дохода_расхода', 'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи'
        ]
        for col in text_cols:
            if col in df_final.columns:
                df_final[col] = df_final[col].astype('string')
        
        logger.debug(
            f"Объединение завершено: {len(df_result)} + {len(df9002_16)} = "
            f"{len(df_final)} строк"
        )
        
        return df_final

        
        
        
        

        
        
        
        
        
        
        
        
        