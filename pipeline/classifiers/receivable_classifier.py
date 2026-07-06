"""
Классификатор задолженности.

Содержит методы для классификации ДЗ/КЗ и подвидов задолженности.
Переиспользуется в шагах 4, 5, 11.
"""
import pandas as pd
from loguru import logger

from io_module import DataLoader
from pipeline.errors import MissingMappingError, MissingSubtypeError


class ReceivableClassifier:
    """
    Утилитарный класс для классификации задолженности.
    
    Содержит методы, которые могут переиспользоваться в разных шагах
    (Step4, Step5, Step11 и т.д.).
    """
    
    # Константы
    DEBIT = 'Дебиторская задолженность'
    CREDIT = 'Кредиторская задолженность'
    UNSPECIFIED = 'не_указано'
    OTHER_DEBIT = 'Прочая ДЗ'
    OTHER_CREDIT = 'Прочая КЗ'
    
    SPECIAL_ACCOUNTS = {
        '97.21': CREDIT,
        '76.07.9': CREDIT,
        '76.07.5': CREDIT,
        '63': DEBIT,
    }
    
    @staticmethod
    def map_accounts_to_mapping(df: pd.DataFrame, partially_matching_accounts_df: pd.DataFrame) -> pd.DataFrame:
        """Заменяет счета ОСВ на соответствующие счета из справочника"""
        mapping_dict = dict(zip(
            partially_matching_accounts_df['Счет из ОСВ'],
            partially_matching_accounts_df['Совпадающие счета из справочника']
        ))
        df['счет'] = df['счет'].map(mapping_dict).fillna(df['счет']).astype('string')
        return df
    
    @staticmethod
    def get_accounts_with_debt_type(mapping_df: pd.DataFrame) -> set:
        """Получает список счетов, для которых определен вид задолженности"""
        mask = mapping_df['вид_задолженности'].isin([ReceivableClassifier.DEBIT, ReceivableClassifier.CREDIT])
        return set(mapping_df.loc[mask, 'счет'].unique())
    
    @staticmethod
    def classify_debt_type(df: pd.DataFrame, accounts_with_debt: set) -> pd.DataFrame:
        """Классифицирует задолженность по типу (ДЗ/КЗ)"""
        df['вид_задолженности'] = ReceivableClassifier.UNSPECIFIED
        
        # Специальные счета
        for account, debt_type in ReceivableClassifier.SPECIAL_ACCOUNTS.items():
            df.loc[df['счет'] == account, 'вид_задолженности'] = debt_type
        
        # Остальные счета - по знаку сальдо
        mask_common = df['счет'].isin(accounts_with_debt)
        mask_special = df['счет'].isin(ReceivableClassifier.SPECIAL_ACCOUNTS.keys())
        mask_other = mask_common & ~mask_special
        
        balance_col = 'сальдо, тыс.ед.'
        df.loc[mask_other & (df[balance_col] >= 0), 'вид_задолженности'] = ReceivableClassifier.DEBIT
        df.loc[mask_other & (df[balance_col] < 0), 'вид_задолженности'] = ReceivableClassifier.CREDIT
        
        df['вид_задолженности'] = df['вид_задолженности'].astype('string')
        
        return df
    @staticmethod
    def handle_special_cases(df: pd.DataFrame) -> pd.DataFrame:
        """Обрабатывает специальные случаи (РБП для аренды/лизинга)"""
        types_rbp_df = DataLoader.load_reference_data(
            sheet_name='ВидыРБП_АрендаЛизинг',
            strings=['виды_рбп_аренда_лизинг']
        )
        valid_rbp_types = set(types_rbp_df['виды_рбп_аренда_лизинг'].tolist())
        
        mask = (
            (df['счет'] == '97.21') &
            (~df['субконто'].fillna('').isin(valid_rbp_types))
        )
        df.loc[mask, 'вид_задолженности'] = ReceivableClassifier.UNSPECIFIED
        
        return df

    @staticmethod
    def clean_subaccounts(df: pd.DataFrame, mapping_df: pd.DataFrame) -> pd.DataFrame:
        """Очищает субконто для счетов без расшифровки"""
        mask = (
            (mapping_df['субконто'] == ReceivableClassifier.UNSPECIFIED) &
            (mapping_df['детализация_субконто'].isin([ReceivableClassifier.UNSPECIFIED, 'Контрагенты. ИНН']))
        )
        accounts_without_subaccounts = set(mapping_df.loc[mask, 'счет'].tolist())
        
        df.loc[df['счет'].isin(accounts_without_subaccounts), 'субконто'] = ReceivableClassifier.UNSPECIFIED
        
        return df
    
    @staticmethod
    def get_subtype_mapping(mapping_df: pd.DataFrame) -> pd.DataFrame:
        """Получает маппинг подвидов задолженности из справочника"""
        mask = mapping_df['вид_задолженности'].isin([ReceivableClassifier.DEBIT, ReceivableClassifier.CREDIT])
        mapping_df_type_debt = mapping_df[mask].copy()
        
        unique_df = mapping_df_type_debt.drop_duplicates(
            subset=['счет', 'субконто', 'вид_задолженности'],
            keep='first'
        )
        
        return unique_df[['счет', 'субконто', 'вид_задолженности', 'подвид_задолженности']]
    
    @staticmethod
    def merge_subtypes(df: pd.DataFrame, subtype_mapping: pd.DataFrame) -> pd.DataFrame:
        """Применяет маппинг подвидов через merge"""
        # ★ ИСПРАВЛЕНИЕ: указываем suffixes, чтобы избежать конфликта столбцов
        
        df = df.merge(
            subtype_mapping,
            on=['счет', 'субконто', 'вид_задолженности'],
            how='left',
            suffixes=('', '_from_mapping')  # ← Явно указываем суффиксы
        )
        
        # Если образовался дубликат, удаляем его
        if 'подвид_задолженности_from_mapping' in df.columns:
            # Если оригинальный столбец пустой, берём из _from_mapping
            df['подвид_задолженности'] = df['подвид_задолженности'].fillna(
                df['подвид_задолженности_from_mapping']
            )
            df = df.drop(columns=['подвид_задолженности_from_mapping'])
        
        df['подвид_задолженности'] = df['подвид_задолженности'].fillna(ReceivableClassifier.UNSPECIFIED)
        
        return df
    
    @staticmethod
    def handle_missing_subtypes(df: pd.DataFrame) -> pd.DataFrame:
        """
        Проверяет наличие всех подвидов задолженности в Меппинге.
        
        Если есть неучтённые позиции — выбрасывает MissingSubtypeError
        с problem_data. Базовый класс Step сам решит, что делать
        (сохранить Excel, заменить на "Прочая" или упасть с ошибкой)
        в зависимости от флага STRICT_SUBTYPE_CHECK.
        """
        mask_unspecified = df['подвид_задолженности'] == ReceivableClassifier.UNSPECIFIED
        mask_debt = df['вид_задолженности'].isin([
            ReceivableClassifier.DEBIT, 
            ReceivableClassifier.CREDIT
        ])
        
        missing_df = df.loc[
            mask_unspecified & mask_debt,
            ['субконто', 'счет', 'вид_задолженности', 'подвид_задолженности', 'сальдо, тыс.ед.']
        ].drop_duplicates()
        
        if missing_df.empty:
            logger.info('Поле "подвид_задолженности" полностью учтено в Меппинге')
            return df
        
        # ★ Просто выбрасываем исключение с problem_data
        # Вся логика обработки — в базовом классе Step
        raise MissingSubtypeError(
            message="Есть позиции, не учтённые в Меппинге",
            problem_data=missing_df,
            reference_name="Меппинг",
            count=len(missing_df)
        )
    
    @staticmethod
    def apply_categorical_subtype(df: pd.DataFrame, mapping_df: pd.DataFrame) -> pd.DataFrame:
        """
        Преобразует столбец в строковый тип.
        """
        df['подвид_задолженности'] = df['подвид_задолженности'].astype('string')
        
        return df