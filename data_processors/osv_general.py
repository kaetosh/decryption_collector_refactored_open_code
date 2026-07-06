# -*- coding: utf-8 -*-
"""
Created on Tue Jun 16 16:33:50 2026

@author: a.karabedyan
"""

# -*- coding: utf-8 -*-
"""
Created on Tue Apr 21 11:19:49 2026

@author: a.karabedyan
"""

import numpy as np
import pandas as pd
from abc import ABC, abstractmethod
from io import BytesIO
from data_processors.file_processor import FileProcessor

def pivot_hierarchy_to_columns(df, separator='_'):
    """
    Разносит вертикальные иерархические данные в горизонтальные столбцы

    Parameters
    ----------
    df : pandas.DataFrame
        DataFrame с колонками 'Уровень', 'Счет', 'Наименование'
    separator : str, default='_'
        Разделитель между счетом и наименованием

    Returns
    -------
    pandas.DataFrame
        DataFrame с добавленными колонками Level_0, Level_1, ...
    """
    
    
    
    max_level = df['Уровень'].max()
    for level in range(max_level + 1):
        level_mask = df['Уровень'] == level
        combined = df['Счет'] + separator + df['Наименование']
        df[f'Level_{level}'] = combined.where(level_mask)
        df[f'Level_{level}'] = df[f'Level_{level}'].ffill()
        higher_level_mask = df['Уровень'] < level
        df.loc[higher_level_mask, f'Level_{level}'] = np.nan
    return df


def get_col_sum_or_zero(df, col):
    """Возвращает сумму столбца или 0, если столбца нет"""
    if col in df.columns:
        numeric_series = pd.to_numeric(df[col], errors='coerce').fillna(0)
        return numeric_series.sum().round()
    return 0


def keep_leaf_rows_vectorized(df):
    """
    Удаляет родительские строки, оставляя только те, которые не имеют дочерних.
    """
    df = df.copy()
    df['next_level'] = df['Уровень'].shift(-1)
    mask_to_keep = ~((df['next_level'] > df['Уровень']) & df['next_level'].notna())
    result = df[mask_to_keep].drop('next_level', axis=1).reset_index(drop=True)
    return result

class BaseOSVFileProcessor(FileProcessor, ABC):
    """Базовый класс для обработчиков ОСВ 1С."""

    def __init__(self):
        super().__init__()
        self.df_type_connection = pd.DataFrame()
        self.file = None

    @abstractmethod
    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Поиск шапки таблицы, переименование заголовков, очистка.
        Должен быть реализован в наследниках.
        """
        pass

    def _prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Общие шаги подготовки после _process_dataframe_optimized."""
        df=df.copy()
        
        name_col_with_num = ['Дебет_начало',
                             'Кредит_начало',
                             'Дебет_оборот',
                             'Кредит_оборот',
                             'Дебет_конец',
                             'Кредит_конец',
                             'Уровень']
        # Получим список колонок
        columns_osv = df.columns.to_list()
        
        # Исключим из списка колонки с числовыми значниями
        columns_osv_only_str = [i for i in columns_osv if i not in name_col_with_num]
        
        # Сформируем словарь для форматирование колонок в string формат
        reformat_str_com = dict.fromkeys(columns_osv_only_str, 'string')
        
        # Преобразуем колонки без сальдо в строковый формат
        df = df.astype(reformat_str_com)
        
        # Преобразуем колонки с сальдо в числовой формат
        df[name_col_with_num] = df[name_col_with_num].apply(pd.to_numeric, errors='coerce').round(2)
        
        # Добавляем ведущий ноль для однозначных счетов
        df['Счет'] = df['Счет'].str.zfill(2)
        df.dropna(subset=['Наименование'], inplace=True)

        # Добавляем имя исходного файла
        df['Исх.файл'] = self.file
        df['Исх.файл'] = df['Исх.файл'].astype('string')
        
        return df

    def process_file(self, stream: BytesIO, file_name: str):
        """Основной метод обработки файла."""
        self.file = file_name
        
        # Предобработка (добавление столбцов Уровень и Курсив)
        df = self._preprocessor_openpyxl(stream)

        # Установка заголовков и очистка (специфично для формата)
        df = self._process_dataframe_optimized(df)

        # Общая пост-обработка
        df = self._prepare_dataframe(df)
        
        self.table_for_check = pd.DataFrame()  # по умолчанию пустая

        df = pivot_hierarchy_to_columns(df)
        df = keep_leaf_rows_vectorized(df)
        df.drop(columns=['Уровень'], errors='ignore', inplace=True)

        # Создаём проверочную таблицу со свёрнутыми остатками/оборотами
        self.table_for_check = pd.DataFrame({
            'Сальдо_начало_свернуто': [
                get_col_sum_or_zero(df, 'Дебет_начало') - get_col_sum_or_zero(df, 'Кредит_начало')
            ],
            'Оборот_свернуто': [
                get_col_sum_or_zero(df, 'Дебет_оборот') - get_col_sum_or_zero(df, 'Кредит_оборот')
            ],
            'Сальдо_конец_свернуто': [
                get_col_sum_or_zero(df, 'Дебет_конец') - get_col_sum_or_zero(df, 'Кредит_конец')
            ],
            'Исх.файл': [self.file]
        })
        
        df.drop(columns=['Уровень'], errors='ignore', inplace=True)
        return df, self.table_for_check


class GeneralOSV_UPPFileProcessor(BaseOSVFileProcessor):
    """Обработчик для ОСВ из 1С УПП (старый формат)."""

    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        MAX_HEADER_ROWS = 30

        df = df.replace('', np.nan)
        df.dropna(axis=1, how='all', inplace=True)
        df.dropna(axis=0, how='all', inplace=True)

        max_rows_to_check = min(MAX_HEADER_ROWS, len(df))

        # Поиск столбца со словом "счет"
        account_col_idx = None
        for col_idx in range(df.shape[1]):
            col_values = df.iloc[:max_rows_to_check, col_idx].astype(str).str.strip().str.lower()
            if 'счет' in col_values.values:
                account_col_idx = col_idx
                break
        if account_col_idx is None:
            raise ValueError('Не найден столбец с "Счет" в первых 30 строках.')

        first_col = df.iloc[:, account_col_idx].astype(str)
        mask = first_col == 'Счет'
        if not mask.any():
            raise ValueError('Файл не является ОСВ 1с.')

        date_row_idx = mask.idxmax()
        df.columns = df.iloc[date_row_idx]
        df = df.iloc[date_row_idx + 1:].copy()

        df.columns = ['Уровень', 'Курсив'] + df.columns[2:].tolist()

        # Поиск столбца "Наименование"
        first_row = df.iloc[0]
        mask_name = first_row == 'Наименование'
        cols_name_acc = np.where(mask_name)[0]
        if cols_name_acc.size == 0:
            raise ValueError('ОСВ выгружена без наименований счета')
        col_index_name_acc = cols_name_acc[0]

        required_cols = [
            'Сальдо на начало периода',
            'Оборот за период',
            'Сальдо на конец периода'
        ]
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f'Отсутствует обязательный столбец: {col}')

        cols = df.columns.tolist()
        target_idx_a = df.columns.get_loc('Сальдо на начало периода')
        target_idx_b = df.columns.get_loc('Оборот за период')
        target_idx_c = df.columns.get_loc('Сальдо на конец периода')

        new_cols = list(df.columns)
        new_cols[col_index_name_acc] = 'Наименование'

        def find_column_index(cols, df, start_idx, word):
            for idx in range(start_idx + 1, len(cols)):
                if df.iloc[0, idx] == word:
                    return idx
            return None

        new_cols[target_idx_a] = 'Дебет_начало'
        cred_start_idx = find_column_index(cols, df, target_idx_a, 'Кредит')
        if cred_start_idx is not None:
            new_cols[cred_start_idx] = 'Кредит_начало'

        new_cols[target_idx_b] = 'Дебет_оборот'
        cred_turn_idx = find_column_index(cols, df, target_idx_b, 'Кредит')
        if cred_turn_idx is not None:
            new_cols[cred_turn_idx] = 'Кредит_оборот'

        new_cols[target_idx_c] = 'Дебет_конец'
        cred_end_idx = find_column_index(cols, df, target_idx_c, 'Кредит')
        if cred_end_idx is not None:
            new_cols[cred_end_idx] = 'Кредит_конец'

        df.columns = new_cols

        df = df.loc[:, df.columns.notna()]
        df.columns = df.columns.astype(str)
        df = df.iloc[1:]  # удаляем строку "Дебет Кредит ..."

        if df['Уровень'].max() == 0:
            raise ValueError('ОСВ пустая.')

        if df['Уровень'].isnull().any() or df['Курсив'].isnull().any():
            raise ValueError('Найдены пустые значения в столбцах Уровень или Курсив.')

        df.drop(columns=['Курсив'], errors='ignore', inplace=True)
        return df


class GeneralOSV_NonUPPFileProcessor(BaseOSVFileProcessor):
    """Обработчик для ОСВ из 1С (стандартный формат)."""

    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.replace('', np.nan)
        df.dropna(axis=1, how='all', inplace=True)
        df.dropna(axis=0, how='all', inplace=True)

        # Поиск столбца "Счет"
        account_col_idx = None
        max_rows_to_check = min(30, df.shape[0])
        for col_idx in range(df.shape[1]):
            col_values = df.iloc[:max_rows_to_check, col_idx].astype(str).str.strip().str.lower()
            if 'счет' in col_values.values:
                account_col_idx = col_idx
                break
        if account_col_idx is None:
            raise ValueError('Не найден столбец с "Счет" в первых 30 строках.')

        first_col = df.iloc[:, account_col_idx].astype(str)
        mask = first_col == 'Счет'
        if not mask.any():
            raise ValueError('Файл не является ОСВ 1с.')

        date_row_idx = mask.idxmax()
        df.columns = df.iloc[date_row_idx]
        df = df.iloc[date_row_idx + 1:].copy()

        df.columns = ['Уровень', 'Курсив'] + df.columns[2:].tolist()

        cols = df.columns.tolist()
        target_idx_0 = cols.index('Наименование счета')
        target_idx_a = cols.index('Сальдо на начало периода')
        target_idx_b = cols.index('Обороты за период')
        target_idx_c = cols.index('Сальдо на конец периода')

        new_cols = cols.copy()
        new_cols[target_idx_0] = 'Наименование'
        new_cols[target_idx_a] = 'Дебет_начало'
        new_cols[target_idx_a + 1] = 'Кредит_начало'
        new_cols[target_idx_b] = 'Дебет_оборот'
        new_cols[target_idx_b + 1] = 'Кредит_оборот'
        new_cols[target_idx_c] = 'Дебет_конец'
        new_cols[target_idx_c + 1] = 'Кредит_конец'

        df.columns = new_cols
        df = df.loc[:, df.columns.notna()]
        df.columns = df.columns.astype(str)
        df = df.iloc[1:]  # удаляем строку с "Дебет Кредит ..."

        if df['Уровень'].max() == 0:
            raise ValueError('ОСВ пустая.')

        if df['Уровень'].isnull().any() or df['Курсив'].isnull().any():
            raise ValueError('Найдены пустые значения в столбцах Уровень или Курсив.')

        df.drop(columns=['Курсив'], errors='ignore', inplace=True)
        return df