"""
Created on Tue Apr 21 11:20:06 2026
@author: a.karabedyan
"""

import re
import numpy as np
import pandas as pd
from typing import Tuple, Optional
from loguru import logger
from io import BytesIO

from data_processors.file_processor import FileProcessor, exclude_values

def find_account_from_text(df: pd.DataFrame, search_text: str = "Оборотно-сальдовая ведомость по счету ") -> Optional[str]:
    """
    Ищет в DataFrame ячейку с search_text и извлекает номер счета.
    """
    for col in df.columns:
        for idx, value in df[col].items():
            if isinstance(value, str) and search_text in value:
                try:
                    after_text = value.split(search_text)[1]
                    account = after_text.split()[0]
                    return account
                except (IndexError, AttributeError):
                    pattern = rf"{re.escape(search_text)}(\S+)"
                    match = re.search(pattern, value)
                    if match:
                        return match.group(1)
    return None


def get_col_or_zeros(df: pd.DataFrame, col: str):
    """Безопасное получение столбца или Series из нулей, если столбца нет"""
    return df[col] if col in df.columns else 0


def calculate_check_values(df: pd.DataFrame, columns_config: dict) -> pd.DataFrame:
    """Расчет контрольных значений для проверки оборотов"""
    df_check = df.copy()
    
    df_check['Сальдо_начало_до_обработки'] = (
        get_col_or_zeros(df_check, columns_config['start_debit']) - 
        get_col_or_zeros(df_check, columns_config['start_credit'])
    )
    df_check['Сальдо_конец_до_обработки'] = (
        get_col_or_zeros(df_check, columns_config['end_debit']) - 
        get_col_or_zeros(df_check, columns_config['end_credit'])
    )
    df_check['Оборот_до_обработки'] = (
        get_col_or_zeros(df_check, columns_config['debit_turnover']) - 
        get_col_or_zeros(df_check, columns_config['credit_turnover'])
    )
    
    return df_check[['Сальдо_начало_до_обработки', 'Сальдо_конец_до_обработки', 'Оборот_до_обработки']].reset_index()


def create_check_after_process(df: pd.DataFrame, columns_config: dict) -> pd.DataFrame:
    """Создание таблицы с данными после обработки"""
    return pd.DataFrame({
        'Сальдо_начало_после_обработки': [
            get_col_or_zeros(df, columns_config['start_debit']).sum() - 
            get_col_or_zeros(df, columns_config['start_credit']).sum()
        ],
        'Оборот_после_обработки': [
            get_col_or_zeros(df, columns_config['debit_turnover']).sum() - 
            get_col_or_zeros(df, columns_config['credit_turnover']).sum()
        ],
        'Сальдо_конец_после_обработки': [
            get_col_or_zeros(df, columns_config['end_debit']).sum() - 
            get_col_or_zeros(df, columns_config['end_credit']).sum()
        ]
    })


class BaseAccountOSVProcessor(FileProcessor):
    """Базовый обработчик для ОСВ счета 1С"""
    
    def __init__(self):
        super().__init__()
        self.df_type_connection = pd.DataFrame()
        self._columns_config = {
            'start_debit': 'Дебет_начало',
            'start_credit': 'Кредит_начало',
            'end_debit': 'Дебет_конец',
            'end_credit': 'Кредит_конец',
            'debit_turnover': 'Дебет_оборот',
            'credit_turnover': 'Кредит_оборот'
        }
    
    @staticmethod
    def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """Очистка DataFrame от пустых строк и столбцов, удаление лишних пробелов"""
        df = df.replace('', np.nan)
        df.dropna(axis=1, how='all', inplace=True)
        df.dropna(axis=0, how='all', inplace=True)
        
        cols_to_clean = [col for col in df.columns if col not in ['Уровень_группировки', 'Курсив']]
        for col in cols_to_clean:
            if df[col].dtype == 'object':
                df[col] = df[col].str.strip().str.replace(r'\s+', ' ', regex=True)
        
        return df
    
    def _find_header_column(self, df: pd.DataFrame, search_value: str, max_rows: int = 30) -> Tuple[Optional[int], Optional[int]]:
        """Поиск столбца с заголовком и строки с искомым значением"""
        max_rows_to_check = min(max_rows, df.shape[0])
        
        for col_idx in range(df.shape[1]):
            col_values = df.iloc[:max_rows_to_check, col_idx].astype(str).str.strip().str.lower()
            if search_value in col_values.values:
                mask = col_values == search_value
                if mask.any():
                    row_idx = mask.idxmax()
                    return col_idx, row_idx
        
        return None, None
    
    def _process_header(self, df: pd.DataFrame, header_row_idx: int, rename_columns: bool = False) -> pd.DataFrame:
        """Установка заголовков и очистка данных"""
        df.columns = df.iloc[header_row_idx]
        df = df.iloc[header_row_idx + 1:].copy()
        
        if rename_columns:
            df.columns = ['Уровень', 'Курсив'] + df.columns[2:].tolist()
        
        return df
    
    def _rename_balance_columns(self, df: pd.DataFrame, cols: list, target_indices: list) -> pd.DataFrame:
        """Переименование столбцов сальдо и оборотов"""
        new_cols = cols.copy()
        for target_idx, new_names in target_indices:
            for i, new_name in enumerate(new_names):
                if target_idx + i < len(new_cols):
                    new_cols[target_idx + i] = new_name
        
        df.columns = new_cols
        return df
    
    def _clean_after_header(self, df: pd.DataFrame) -> pd.DataFrame:
        """Очистка после установки заголовков"""
        df = df.loc[:, df.columns.notna()]
        df.columns = df.columns.astype(str)
        df = df.iloc[1:] # Удаляем строку, которая стала заголовком
        return df
    
    def _validate_empty_osv(self, df: pd.DataFrame) -> None:
        """Проверка на пустую ОСВ"""
        if df['Уровень'].max() == 0:
            check_columns = ['Дебет_начало', 'Кредит_начало', 'Дебет_оборот', 
                           'Кредит_оборот', 'Дебет_конец', 'Кредит_конец']
            check_columns = [col for col in check_columns if col in df.columns]
            
            if df[check_columns].sum().sum() == 0:
                raise ValueError('ОСВ счета вероятно пустая.')
            else:
                df.loc[:, 'Уровень'] = 1
    
    def _validate_level_columns(self, df: pd.DataFrame) -> None:
        """Проверка наличия пустых значений в столбцах Уровень и Курсив"""
        if df['Уровень'].isnull().any() or df['Курсив'].isnull().any():
            raise ValueError('Найдены пустые значения в столбцах Уровень или Курсив.')
    
    def _process_missing_values(self, df: pd.DataFrame, account_col: str) -> pd.DataFrame:
        """Обработка пропущенных значений"""
        df = df.copy()

        if 'Показа-\nтели' in df.columns:
            mask = df['Показа-\nтели'].str.contains('Кол.|Вал.', na=False)
            df.loc[~mask, account_col] = df.loc[~mask, account_col].fillna('не_указано')
            df[account_col] = df[account_col].ffill()
        else:
            # Проставляем значение "Количество"
            
            mask = df[account_col].isna() & df['Уровень'].eq(df['Уровень'].shift(1))
            df[account_col] = df[account_col].mask(mask, 'Количество')
            
            # Удаляем строки с "Количество" ниже строки с Итого
            if (df[account_col] == 'Итого').any():
                index_total = df[df[account_col] == 'Итого'].index[0]
                df = df[(df.index <= index_total) | ((df.index > index_total) & (df[account_col] != 'Количество'))]
            
            df.loc[:, account_col] = df[account_col].fillna('не_указано')
            
        # Добавление ведущего нуля для счетов до 10
        mask = (df[account_col].str.len() == 1) & self._is_accounting_code_vectorized(df[account_col])
        df.loc[mask, account_col] = '0' + df.loc[mask, account_col]
        
        return df
        

    
    def _spread_vertical_data(self, df: pd.DataFrame, account_col: str) -> pd.DataFrame:
        """Разнос вертикальных данных в горизонтальные уровни"""
        max_level = df['Уровень'].max()
        
        for level in range(max_level + 1):
            level_mask = df['Уровень'] == level
            df[f'Level_{level}'] = df[account_col].where(level_mask)
            df[f'Level_{level}'] = df[f'Level_{level}'].ffill()
            higher_level_mask = df['Уровень'] < level
            df.loc[higher_level_mask, f'Level_{level}'] = None

        return df, max_level
    
    def _fill_until_itogo(self, df, fill_col, itogo_col):
        """Заполняет столбец вышестоящими значениями до ближайшей строки с 'Итого'"""
        if fill_col not in df.columns or itogo_col not in df.columns:
            return df
        
        is_itogo = df[itogo_col].str.startswith('Итого', na=False)
        group = is_itogo.shift(1, fill_value=False).cumsum()
        df[fill_col] = df.groupby(group)[fill_col].ffill()
        
        return df
    
    def _remove_duplicate_rows(self, df: pd.DataFrame, account_col: str, max_level: int) -> pd.DataFrame:
        """Удаление дублирующихся строк (строки итогов, которые дублируют уровень)"""
        conditions = []
        for i in range(max_level):
            condition = (
                (df['Уровень'] == i) & 
                (df[account_col] == df[f'Level_{i}']) & 
                (df['Уровень'].shift(-1) > i)
            )
            conditions.append(condition)
        
        if conditions:
            mask = pd.concat(conditions, axis=1).any(axis=1)
            df = df[~mask]
        
        return df
    
    def _create_check_tables(self, df: pd.DataFrame, df_for_check: pd.DataFrame, file_name: str) -> pd.DataFrame:
        """Создание и возврат таблицы для проверки"""
        df_check_after_process = create_check_after_process(df, self._columns_config)
        pivot_df_check = pd.concat([df_for_check, df_check_after_process], axis=1).fillna(0)
        
        for field in ['Сальдо_начало_разница', 'Оборот_разница', 'Сальдо_конец_разница']:
            pivot_df_check[field] = (
                pivot_df_check[field.replace('_разница', '_до_обработки')] -
                pivot_df_check[field.replace('_разница', '_после_обработки')]
            ).round()
        
        pivot_df_check['Исх.файл'] = file_name
        return pivot_df_check
    
    def _get_desired_columns(self, df: pd.DataFrame) -> Tuple[list, list]:
        """Получение списков необходимых столбцов"""
        desired_order_not_with_suff = [
            col for col in ['Дебет_начало', 'Кредит_начало', 'Дебет_оборот',
                           'Кредит_оборот', 'Дебет_конец', 'Кредит_конец']
            if col in df.columns
        ]
        return desired_order_not_with_suff, desired_order_not_with_suff.copy()


class AccountOSV_UPPFileProcessor(BaseAccountOSVProcessor):
    """Обработчик для ОСВ счета 1С УПП"""
    
    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Поиск шапки таблицы, переименование заголовков, очистка"""
        df = BaseAccountOSVProcessor._clean_dataframe(df)
        col_idx, header_row_idx = self._find_header_column(df, 'субконто')
        
        if col_idx is None or header_row_idx is None:
            raise ValueError('Не найден столбец с "Субконто" в первых 30 строках.')
        
        first_col = df.iloc[:, col_idx].astype(str)
        if not (first_col == 'Субконто').any():
            raise ValueError('Файл не является ОСВ счета 1с.')
        
        df = self._process_header(df, header_row_idx, rename_columns=True)

        cols = df.columns.tolist()
        
        def safe_find_column(cols, target):
            for i, col in enumerate(cols):
                if col is not pd.NA and col == target:
                    return i
            raise ValueError(f"Колонка '{target}' не найдена")

        target_idx_a = safe_find_column(cols, 'Сальдо на начало периода')
        target_idx_b = safe_find_column(cols, 'Оборот за период')
        target_idx_c = safe_find_column(cols, 'Сальдо на конец периода')
        
        def find_credit_index(cols, df, start_idx, word):
            for idx in range(start_idx + 1, len(cols)):
                cell_value = df.iloc[0, idx]
                if pd.notna(cell_value) and str(cell_value).strip() == word:
                    return idx
            logger.warning(f"'{word}' не найден после индекса {start_idx}")
            return None

        target_indices = [
            (target_idx_a, ['Дебет_начало']),
            (find_credit_index(cols, df, target_idx_a, 'Кредит'), ['Кредит_начало']),
            (target_idx_b, ['Дебет_оборот']),
            (find_credit_index(cols, df, target_idx_b, 'Кредит'), ['Кредит_оборот']),
            (target_idx_c, ['Дебет_конец']),
            (find_credit_index(cols, df, target_idx_c, 'Кредит'), ['Кредит_конец'])
        ]
        
        valid_indices = [(idx, names) for idx, names in target_indices if idx is not None]
        df = self._rename_balance_columns(df, cols, valid_indices)
        df = self._clean_after_header(df)

        self._validate_empty_osv(df)
        self._validate_level_columns(df)
        
        return df
    
    def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Основная обработка таблицы"""
        self.file = file_name
        df = self._preprocessor_openpyxl(stream)
        account_for_table = find_account_from_text(df)
        df = self._process_dataframe_optimized(df)
        df['Исх.файл'] = self.file

        # Приведение типов
        list_with_numeric_columns = ['Уровень', 'Курсив', 'Дебет_начало', 'Кредит_начало', 
                                     'Дебет_оборот', 'Кредит_оборот', 'Дебет_конец', 'Кредит_конец']
        
        columns_df_only_str = [i for i in df.columns if i not in list_with_numeric_columns]
        reformat_str_com = dict.fromkeys(columns_df_only_str, 'string')
        df = df.astype(reformat_str_com)
        df[list_with_numeric_columns] = df[list_with_numeric_columns].apply(pd.to_numeric, errors='coerce').round(2)

        # Обработка пропущенных значений
        df = self._process_missing_values(df, 'Субконто')
        
        # Разнос вертикальных данных
        df, max_level = self._spread_vertical_data(df, 'Субконто')
        
        # Получение списков столбцов
        desired_order_not_with_suff, desired_order = self._get_desired_columns(df)
        
        # Создание контрольной таблицы (берем итоги)
        total_rows = df[df['Субконто'] == 'Итого']
        if total_rows.empty:
             # Если явной строки Итого нет, пробуем взять последнюю строку или выбрасываем ошибку
             # В УПП обычно есть Итого
             raise ValueError('Нет значений по строке Итого')
             
        df_for_check = total_rows[['Субконто'] + desired_order_not_with_suff].copy().tail(1)
        df_for_check[desired_order_not_with_suff] = df_for_check[desired_order_not_with_suff].astype(float).fillna(0)
        df_for_check = calculate_check_values(df_for_check, self._columns_config)
        
        # Заполнение вида связи до итогов
        df = self._fill_until_itogo(df, 'Вид связи КА за период', 'Субконто')
        
        # Удаление дублирующихся строк
        df = self._remove_duplicate_rows(df, 'Субконто', max_level)
        
        # Удаление строк с Итого и exclude_values
        if len(df) > 1:
            df = df[~df['Субконто'].str.contains('Итого')]
            df = df[~df['Субконто'].isin(exclude_values)]
            

            
            if account_for_table:
                level_columns = [col for col in df.columns if col.startswith('Level_')]
                for col in level_columns:
                    if self._is_accounting_code_vectorized(df[col]).any():
                        break
                    else:
                        condition = (df[col] == 'Итого') | (df[col].isna())
                        df[col] = df[col].mask(condition, account_for_table)
        else:
            first_index = df.index[0]
            if df.loc[first_index, 'Субконто'] == 'Итого' and account_for_table:
                df.loc[first_index, 'Субконто'] = account_for_table
                level_columns = [col for col in df.columns if col.startswith('Level_')]
                for col in level_columns:
                    condition = (df[col] == 'Итого') | (df[col].isna())
                    df[col] = df[col].mask(condition, account_for_table)
        
        df = df.rename(columns={'Счет': 'Субконто'})
        if 'Уровень' in df.columns:
            df.drop('Уровень', axis=1, inplace=True)
        
        # Фильтрация пустых строк
        df = df[df[desired_order].notna().any(axis=1)]
        
        # Удаление ненужных столбцов
        for col in ['Показа-\nтели', 'Курсив']:
            if col in df.columns:
                df = df.drop(columns=[col])
        
        # Выравнивание столбцов (shiftable_level реализован в FileProcessor)
        df = self.shiftable_level(df)
        
        # Создание таблицы для проверки
        self.table_for_check = self._create_check_tables(df, df_for_check, self.file)
        
        # Дополнительная обработка уровней, если счет не был определен внутри таблицы
        if account_for_table:
            df = self._shift_level_columns_vectorized(df, account_for_table)
        
        return df, self.table_for_check
    
    def _shift_level_columns_vectorized(self, df: pd.DataFrame, account_for_table: str) -> pd.DataFrame:
        """Для ОСВ выгруженных без счетов добавляет столбец вручную"""
        level_columns = [col for col in df.columns if col.startswith('Level_')]
        
        if not level_columns:
            df['Level_0'] = pd.Series([account_for_table] * len(df), dtype='string')
            return df
        
        level_columns.sort()
        level_df = df[level_columns]
        all_values = level_df.stack().dropna().unique()
        
        if len(all_values) > 0:
            check_series = pd.Series(all_values)
            has_accounting = self._is_accounting_code_vectorized(check_series).any()
        else:
            has_accounting = False
        
        if not has_accounting:
            for i in range(len(level_columns) - 1, -1, -1):
                current_col = level_columns[i]
                next_col = f'Level_{i + 1}'
                if next_col not in df.columns:
                    df[next_col] = pd.Series([None] * len(df), dtype='string')
                df[next_col] = df[current_col].astype('string')
            df['Level_0'] = account_for_table
            if df['Level_0'].dtype != 'string':
                df['Level_0'] = df['Level_0'].astype('string')
        
        return df


class AccountOSV_NonUPPFileProcessor(BaseAccountOSVProcessor):
    """Обработчик для ОСВ счета 1С не УПП"""
    
    @staticmethod
    def _process_dataframe_optimized(df: pd.DataFrame) -> pd.DataFrame:
        """Поиск шапки таблицы, переименование заголовков, очистка"""
        df = BaseAccountOSVProcessor._clean_dataframe(df)
        
        # Создаем экземпляр только для доступа к методам поиска, т.к. они не static
        processor = AccountOSV_NonUPPFileProcessor()
        col_idx, header_row_idx = processor._find_header_column(df, 'счет')
        
        if col_idx is None or header_row_idx is None:
            raise ValueError('Не найден столбец с "Счет" в первых 30 строках.')
        
        first_col = df.iloc[:, col_idx].astype(str)
        if not (first_col == 'Счет').any():
            raise ValueError('Файл не является ОСВ счета 1с.')
        
        df = processor._process_header(df, header_row_idx, rename_columns=True)
        
        cols = df.columns.tolist()
        # В Non-UPP структура обычно проще: Сальдо на начало (Дебет, Кредит) идут подряд
        try:
            target_indices = [
                (cols.index('Сальдо на начало периода'), ['Дебет_начало', 'Кредит_начало']),
                (cols.index('Обороты за период'), ['Дебет_оборот', 'Кредит_оборот']),
                (cols.index('Сальдо на конец периода'), ['Дебет_конец', 'Кредит_конец'])
            ]
            df = processor._rename_balance_columns(df, cols, target_indices)
        except ValueError as e:
            raise ValueError(f"Ошибка структуры столбцов: {e}")
            
        df = processor._clean_after_header(df)
        processor._validate_empty_osv(df)
        processor._validate_level_columns(df)
        
        return df
    
    def process_file(self, stream: BytesIO, file_name: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Основная обработка таблицы"""
        self.file = file_name
        df = self._preprocessor_openpyxl(stream)
        df = self._process_dataframe_optimized(df)
        df['Исх.файл'] = self.file
        
        # Обработка пропущенных значений
        df = self._process_missing_values(df, 'Счет')
        
        # Разнос вертикальных данных
        df, max_level = self._spread_vertical_data(df, 'Счет')
        
        # Получение списков столбцов
        desired_order_not_with_suff, desired_order = self._get_desired_columns(df)
        
        # Создание контрольной таблицы
        total_rows = df[df['Счет'] == 'Итого']
        if total_rows.empty:
            raise ValueError('Нет значений по строке Итого')
            
        df_for_check = total_rows[['Счет'] + desired_order_not_with_suff].copy().tail(1)
        df_for_check[desired_order_not_with_suff] = df_for_check[desired_order_not_with_suff].astype(float).fillna(0)
        df_for_check = calculate_check_values(df_for_check, self._columns_config)
        
        # Удаление дублирующихся строк
        df = self._remove_duplicate_rows(df, 'Счет', max_level)
        
        # Удаление служебных строк
        df = df[~df['Счет'].isin(exclude_values)]
        
        df = df.rename(columns={'Счет': 'Субконто'})
        if 'Уровень' in df.columns:
            df.drop('Уровень', axis=1, inplace=True)
        
        # Фильтрация строк с данными
        df = df[df[desired_order].notna().any(axis=1)]
        
        # Удаление ненужных столбцов
        for col in ['Показа-\nтели', 'Курсив']:
            if col in df.columns:
                df = df.drop(columns=[col])
        
        # Выравнивание столбцов
        df = self.shiftable_level(df)
        
        # Создание таблицы для проверки
        self.table_for_check = self._create_check_tables(df, df_for_check, self.file)
        
        return df, self.table_for_check