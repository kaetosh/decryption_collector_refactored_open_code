# -*- coding: utf-8 -*-
"""
Created on Wed Jul  8 15:52:05 2026

@author: a.karabedyan
"""

# -*- coding: utf-8 -*-
"""
Базовый класс для обработки расходов ОПУ (управленческих и коммерческих).

Реализует общую логику:
1. Загрузка проводок с накопительного счета (26/44) для определения контрагентов
2. Загрузка проводок со счета ОПУ (90.08/90.07) для определения ном_групп
3. Обогащение ном_групп сегментами из справочника УФР
4. Пропорциональное распределение расходов на контрагентов
5. Определение вида связи (3 лица / внутрисегмент / межсегмент)
6. Объединение с основной расшифровкой ОПУ

Наследники параметризуются через __init__:
- account_opu: счет в ОПУ ('90.07' или '90.08')
- account_accumulation: накопительный счет ('44' или '26')
- opu_line_name: название строки ОПУ ('Коммерческие расходы' или 'Управленческие расходы')
"""
import numpy as np
import pandas as pd
from loguru import logger
from pipeline.base import Step, ProcessingContext
from pipeline.errors import ReferenceMismatchError
from io_module import DataLoader


class StepAddExpensesToOpuBase(Step):
    """
    Базовый класс для добавления расходов в расшифровку ОПУ.
    
    Логика:
    1. Загружаем проводки Дт [26/44] Кт 60/76 для определения контрагентов
    2. Загружаем проводки Дт [90.08/90.07] для определения номенклатурных групп
    3. Распределяем расходы с накопительного счета на ном_группы пропорционально
    4. Определяем вид_связи для каждой комбинации контрагент-ном_группа
    5. Добавляем остаток (расходы без контрагентов) как "Прочие расходы"
    6. Объединяем с основной расшифровкой ОПУ
    """
    
    # Счета контрагентов (едины для всех типов расходов)
    ACCOUNTS_CONTRACTORS = ('60', '76')
    
    # Допуск для проверки сходимости с ОСВ (в тыс.ед.)
    TOLERANCE_OSV = 1000
    
    def __init__(
        self,
        name: str,
        description: str,
        account_opu: str,
        account_accumulation: str,
        opu_line_name: str,
    ):
        super().__init__(name=name, description=description)
        
        self.ACCOUNT_OPU = account_opu
        self.ACCOUNT_COST_ACCUMULATION = account_accumulation
        self.OPU_LINE_NAME = opu_line_name
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки."""
        logger.debug(f"Начало обработки: {self.OPU_LINE_NAME}")
        name_company = context.get_metadata('company_name')
        
        # 1. Загрузка данных из контекста
        osv_df, transactions_all_df = self._load_data_from_context(context)
        
        # 2. Обработка проводок с накопительного счета (контрагенты)
        df_accum_clean = self._process_accumulation_transactions(transactions_all_df)
        
        # ★ Защита от отсутствия расходов на накопительном счете
        if df_accum_clean.empty:
            logger.info(
                f"⚠️ Проводки по счету {self.ACCOUNT_COST_ACCUMULATION} отсутствуют. "
                f"{self.OPU_LINE_NAME} не добавлены."
            )
            return context
        
        # 3. Обработка проводок со счета ОПУ (ном_группы)
        df_opu = self._process_opu_transactions(transactions_all_df)
        
        # ★ Защита от отсутствия расходов на счете ОПУ
        if df_opu.empty:
            logger.info(
                f"⚠️ Проводки по счету {self.ACCOUNT_OPU} отсутствуют. "
                f"{self.OPU_LINE_NAME} не добавлены."
            )
            return context
        
        # 4. Обогащение ном_групп сегментами из справочника
        df_opu = self._enrich_with_segment(df_opu, name_company)
        
        # 5. Распределение расходов на контрагентов
        df_result = self._distribute_expenses(df_accum_clean, df_opu)
        
        # 6. Проверка сходимости с ОСВ
        self._validate_against_osv(df_result, osv_df)
        
        # 7. Объединение с основной расшифровкой ОПУ
        df_final = self._merge_with_main_df(context.main_df, df_result)
        
        # Обновляем context
        context.main_df = df_final
        
        logger.info(
            f"✓ {self.OPU_LINE_NAME} добавлены: {len(df_result)} строк "
            f"(виды связи: {df_result['вид_связи'].value_counts().to_dict()})"
        )
        
        return context
    
    # =========================================================================
    # ЗАГРУЗКА ДАННЫХ
    # =========================================================================
    
    def _load_data_from_context(
        self,
        context: ProcessingContext
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Загружает необходимые данные из контекста."""
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
        
        logger.debug(
            f"Загружено из контекста: ОСВ={len(osv_df)} строк, "
            f"проводки={len(transactions_all_df)} строк"
        )
        
        return osv_df, transactions_all_df
    
    # =========================================================================
    # ОБРАБОТКА ПРОВОДОК С НАКОПИТЕЛЬНОГО СЧЕТА
    # =========================================================================
    
    def _process_accumulation_transactions(
        self,
        transactions_all_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Обрабатывает проводки Дт [26/44] Кт 60/76 для определения контрагентов.
        
        Фильтрует только из файлов отчёта по накопительному счету,
        чтобы избежать дублей с файлом отчёта по счету ОПУ.
        """
        logger.debug(f"Обработка проводок Дт {self.ACCOUNT_COST_ACCUMULATION} Кт 60/76")
        
        # Фильтруем проводки
        mask_account = (
            transactions_all_df['Дт'].str.startswith(self.ACCOUNT_COST_ACCUMULATION, na=False) &
            transactions_all_df['Кт'].str.startswith(self.ACCOUNTS_CONTRACTORS, na=False)
        )
        mask_file = transactions_all_df['Имя_файла'].str.contains(
            f"_{self.ACCOUNT_COST_ACCUMULATION}_", na=False
        )
        
        df_accum = transactions_all_df.loc[mask_account & mask_file].copy()
        
        # ★ Защита от пустого результата
        if df_accum.empty:
            logger.debug(
                f"Проводки Дт {self.ACCOUNT_COST_ACCUMULATION} Кт 60/76 не найдены"
            )
            return pd.DataFrame()
        
        # Оставляем только необходимые столбцы
        df_accum_clean = df_accum.loc[:, ['Субконто Кт_1', 'Сумма']]
        df_accum_clean = df_accum_clean.rename(columns={
            'Субконто Кт_1': 'контрагент',
            'Сумма': 'оборот, тыс.ед.'
        })
        
        # Переводим в тысячи
        df_accum_clean['оборот, тыс.ед.'] = df_accum_clean['оборот, тыс.ед.'] / 1000
        
        # Обогащение данными из справочника ВидСвязиКА
        df_accum_clean = self._enrich_with_contractor_info(df_accum_clean)
        
        # Группируем по контрагенту
        df_accum_clean = df_accum_clean.groupby(
            ['группа_ка', 'сегмент_ка', 'контрагент'],
            as_index=False
        )['оборот, тыс.ед.'].sum()
        
        logger.debug(
            f"Обработано проводок Дт {self.ACCOUNT_COST_ACCUMULATION}: "
            f"{len(df_accum_clean)} уникальных контрагентов, "
            f"сумма={df_accum_clean['оборот, тыс.ед.'].sum():,.2f} тыс.ед."
        )
        
        return df_accum_clean
    
    def _enrich_with_contractor_info(
        self,
        df: pd.DataFrame
    ) -> pd.DataFrame:
        """Обогащает DataFrame информацией о контрагентах из справочника."""
        group_companies_df = DataLoader.load_reference_data(
            sheet_name='ВидСвязиКА',
            strings=['ВидСвязиКА', 'сегмент', 'ВариантыНазвания']
        )
        
        group_unique = group_companies_df.drop_duplicates(subset='ВариантыНазвания')
        
        mapping_group = group_unique.set_index('ВариантыНазвания')['ВидСвязиКА'].astype('string')
        mapping_segment_ka = group_unique.set_index('ВариантыНазвания')['сегмент'].astype('string')
        
        df['группа_ка'] = df['контрагент'].map(mapping_group).fillna('3 лица').astype('string')
        df['сегмент_ка'] = df['контрагент'].map(mapping_segment_ka).fillna('3 лица').astype('string')
        
        return df
    
    # =========================================================================
    # ОБРАБОТКА ПРОВОДОК СО СЧЕТА ОПУ
    # =========================================================================
    
    def _process_opu_transactions(
        self,
        transactions_all_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Обрабатывает проводки Дт [90.08/90.07] для определения номенклатурных групп.
        
        Фильтрует только из файлов отчёта по счету ОПУ,
        чтобы избежать дублей с файлом отчёта по накопительному счету.
        """
        logger.debug(f"Обработка проводок Дт {self.ACCOUNT_OPU}")
        
        # Фильтруем проводки
        mask_account = transactions_all_df['Дт'].str.startswith(self.ACCOUNT_OPU, na=False)
        mask_file = transactions_all_df['Имя_файла'].str.contains(
            f"_{self.ACCOUNT_OPU}_", na=False
        )
        
        df_opu = transactions_all_df.loc[mask_account & mask_file].copy()
        
        # ★ Защита от пустого результата
        if df_opu.empty:
            logger.debug(
                f"Проводки Дт {self.ACCOUNT_OPU} не найдены"
            )
            return pd.DataFrame()
        
        # Оставляем только необходимые столбцы
        df_opu = df_opu.loc[:, ['Субконто Дт_1', 'Сумма']]
        df_opu = df_opu.rename(columns={'Субконто Дт_1': 'ном_группа'})
        
        # Группируем по ном_группе
        df_opu = df_opu.groupby('ном_группа', as_index=False)['Сумма'].sum()
        
        # Переводим в тысячи
        df_opu['оборот, тыс.ед.'] = df_opu['Сумма'] / 1000
        df_opu = df_opu.loc[:, ['ном_группа', 'оборот, тыс.ед.']]
        
        logger.debug(
            f"Обработано проводок Дт {self.ACCOUNT_OPU}: {len(df_opu)} ном_групп, "
            f"сумма={df_opu['оборот, тыс.ед.'].sum():,.2f} тыс.ед."
        )
        
        return df_opu
    
    def _enrich_with_segment(
        self,
        df_opu: pd.DataFrame,
        name_company: str
    ) -> pd.DataFrame:
        """Обогащает ном_группы сегментами из справочника УФР."""
        logger.debug("Обогащение ном_групп сегментами")
        
        directory_ufr_df = DataLoader.load_reference_data(
            sheet_name='СправочникУФР',
            strings=['строка_уфр', 'сегмент', 'сокращенное_наименование_компании', 'ном_группа_1с']
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
        
        df_opu['сегмент'] = df_opu['ном_группа'].map(mapping_segment).astype('string')
        
        # Проверка: все ли ном_группы замапились
        unmapped_mask = df_opu['сегмент'].isna()
        if unmapped_mask.any():
            unmapped_groups = df_opu.loc[unmapped_mask, 'ном_группа'].unique()
            
            problem_data = pd.DataFrame({
                'ном_группа_без_сегмента': unmapped_groups,
                'сегмент_в_справочнике': [
                    mapping_segment.get(g, 'ОТСУТСТВУЕТ') for g in unmapped_groups
                ],
            })
            
            self._raise_reference_mismatch(
                error_class=ReferenceMismatchError,
                message=(
                    f"В справочнике УФР отсутствуют сегменты для "
                    f"{len(unmapped_groups)} ном_групп"
                ),
                problem_data=problem_data,
                reference_name="Справочник УФР (directory_ufr)",
            )
        
        logger.debug(f"Сегменты добавлены: {df_opu['сегмент'].value_counts().to_dict()}")
        
        return df_opu
    
    # =========================================================================
    # РАСПРЕДЕЛЕНИЕ РАСХОДОВ
    # =========================================================================
    
    def _distribute_expenses(
        self,
        df_accum_clean: pd.DataFrame,
        df_opu: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Распределяет расходы с накопительного счета на ном_группы пропорционально.
        
        Логика:
        1. Рассчитываем долю каждой ном_группы в общих расходах
        2. Распределяем каждого контрагента пропорционально этим долям
        3. Определяем вид_связи для внутреннего периметра
        4. Добавляем остаток как "Прочие расходы"
        """
        logger.debug("Распределение расходов на контрагентов")
        
        # 1. Расчёт долей ном_групп
        total_opu = df_opu['оборот, тыс.ед.'].sum()
        df_opu['доля_ном_группы'] = df_opu['оборот, тыс.ед.'] / total_opu
        
        # 2. Cross-join: каждая строка df_accum_clean × каждая ном_группа
        df_cross = df_accum_clean.assign(key=1).merge(
            df_opu[['ном_группа', 'сегмент', 'доля_ном_группы']].assign(key=1),
            on='key'
        ).drop('key', axis=1)
        
        # 3. Распределение оборота пропорционально долям
        df_cross['оборот_распределенный'] = df_cross['оборот, тыс.ед.'] * df_cross['доля_ном_группы']
        
        # 4. Определение вид_связи
        df_cross['вид_связи'] = self._calculate_connection_type(df_cross)
        
        # 5. Добавление остатка (расходы без контрагентов)
        total_accum_clean = df_accum_clean['оборот, тыс.ед.'].sum()
        remainder = total_opu - total_accum_clean
        
        if remainder > 0:
            df_remainder = self._create_remainder_rows(df_opu, remainder)
            df_result = pd.concat([df_cross, df_remainder], ignore_index=True)
            logger.debug(
                f"Добавлен остаток: {remainder:,.2f} тыс.ед. "
                f"({remainder/total_opu:.1%} от общей суммы)"
            )
        else:
            df_result = df_cross
        
        # 6. Финальная очистка
        df_result = df_result.drop(columns=['оборот, тыс.ед.', 'доля_ном_группы'])
        df_result = df_result.rename(columns={'оборот_распределенный': 'оборот, тыс.ед.'})
        
        # 7. Добавление служебных столбцов
        df_result = self._add_service_columns(df_result)
        
        logger.debug(
            f"Распределение завершено: {len(df_result)} строк, "
            f"сумма={df_result['оборот, тыс.ед.'].sum():,.2f} тыс.ед."
        )
        
        return df_result
    
    def _calculate_connection_type(self, df: pd.DataFrame) -> pd.Series:
        """Рассчитывает вид_связи на основе группа_ка и сегмент_ка."""
        conditions = [
            df['группа_ка'] == '3 лица',
            df['группа_ка'] == 'Прочие ГАП',
            (df['группа_ка'] == 'ГСК') & (df['сегмент_ка'] == df['сегмент']),
            (df['группа_ка'] == 'ГСК') & (df['сегмент_ка'] != df['сегмент']),
        ]
        choices = [
            '3 лица',
            'Прочие ГАП',
            'ГСК внутрисегмент.',
            'ГСК межсегмент.',
        ]
        result = np.select(conditions, choices, default='не_указано')
        return pd.Series(result, dtype='string')
    
    def _create_remainder_rows(
        self,
        df_opu: pd.DataFrame,
        remainder: float
    ) -> pd.DataFrame:
        """Создаёт строки для остатка (расходы без контрагентов)."""
        df_remainder = df_opu[['ном_группа', 'сегмент', 'доля_ном_группы']].copy()
        df_remainder['контрагент'] = 'Прочие расходы'
        df_remainder['группа_ка'] = '3 лица'
        df_remainder['сегмент_ка'] = '3 лица'
        df_remainder['вид_связи'] = '3 лица'
        df_remainder['оборот_распределенный'] = remainder * df_remainder['доля_ном_группы']
        return df_remainder
    
    def _add_service_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Добавляет служебные столбцы для соответствия структуре main_df."""
        df['счет'] = pd.Series([self.ACCOUNT_OPU] * len(df), dtype='string')
        df['доход_расход'] = pd.Series([self.OPU_LINE_NAME] * len(df), dtype='string')
        df['вид_дохода_расхода'] = pd.Series([self.OPU_LINE_NAME] * len(df), dtype='string')
        return df
    
    # =========================================================================
    # ВАЛИДАЦИЯ И ОБЪЕДИНЕНИЕ
    # =========================================================================
    
    def _validate_against_osv(
        self,
        df_result: pd.DataFrame,
        osv_df: pd.DataFrame
    ) -> None:
        """Проверяет сходимость расходов с общей ОСВ."""
        expenses_osv = osv_df.loc[
            osv_df['Счет'].str.startswith(self.ACCOUNT_OPU), 'Дебет_оборот'
        ].sum() / 1000
        
        expenses_from_result = df_result['оборот, тыс.ед.'].sum()
        difference = abs(expenses_osv - expenses_from_result)
        
        if difference > self.TOLERANCE_OSV:
            raise ValueError(
                f"{self.OPU_LINE_NAME} из отчёта по проводкам ({expenses_from_result:,.2f} тыс.ед.) "
                f"отличаются от общей ОСВ ({expenses_osv:,.2f} тыс.ед.) "
                f"на {difference:,.2f} тыс.ед. (допуск: {self.TOLERANCE_OSV})"
            )
        
        logger.debug(
            f"✓ Сходимость {self.OPU_LINE_NAME}: "
            f"ОСВ={expenses_osv:,.2f}, "
            f"отчёт={expenses_from_result:,.2f}, "
            f"разница={difference:,.2f}"
        )
    
    def _merge_with_main_df(
        self,
        main_df: pd.DataFrame,
        df_result: pd.DataFrame
    ) -> pd.DataFrame:
        """Объединяет результат с основной расшифровкой ОПУ."""
        logger.debug("Объединение с основной расшифровкой ОПУ")
        
        df_final = pd.concat([main_df, df_result], ignore_index=True)
        
        # Явное приведение всех текстовых столбцов к string
        # Это устраняет последствия concat (который часто понижает dtype до object)
        text_cols = [
            'счет', 'контрагент', 'ном_группа', 'доход_расход',
            'вид_дохода_расхода', 'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи'
        ]
        for col in text_cols:
            if col in df_final.columns:
                df_final[col] = df_final[col].astype('string')
        
        logger.debug(
            f"Объединение завершено: {len(main_df)} + {len(df_result)} = "
            f"{len(df_final)} строк"
        )
        
        return df_final