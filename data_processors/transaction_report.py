# -*- coding: utf-8 -*-
"""
Created on Wed Jul  1 11:16:39 2026

@author: a.karabedyan
"""

# -*- coding: utf-8 -*-
"""
Created on Mon Aug 25 12:20:46 2025

@author: a.karabedyan
"""
import pandas as pd
from pathlib import Path
from loguru import logger
from utils import cast_columns_to_types, detect_txt_encoding

from data_processors.file_processor import FileProcessor

pd.set_option('future.no_silent_downcasting', True)

class Posting_UPPFileProcessor(FileProcessor):
    """Обработчик для файлов отчётов по проводкам из 1С УПП (TXT-формат)."""

    # =========================================================================
    # ЗАГРУЗКА ФАЙЛА
    # =========================================================================
    
    @staticmethod
    def _find_header_row(
        file_path: Path, 
        keyword: str = 'дата', 
        encoding: str = 'cp1251',
        max_lines_to_read: int = 50,
        errors: str = 'strict',
    ) -> int:
        """Находит физический номер строки с заголовком.

        Args:
            file_path: Путь к TXT-файлу.
            keyword: Ключевое слово в строке заголовка (без учёта регистра).
            encoding: Кодировка файла.
            max_lines_to_read: Сколько первых строк просматривать.
            errors: Режим обработки ошибок декодирования ('strict'/'replace').
        """
        keyword_lower = keyword.lower()
        
        with open(file_path, 'r', encoding=encoding, errors=errors) as f:
            for physical_line_idx, line in enumerate(f):
                if physical_line_idx >= max_lines_to_read:
                    break
                if keyword_lower in line.lower():
                    logger.debug(
                        "Заголовок найден на строке {}: {}...",
                        physical_line_idx,
                        line.strip()[:50],
                    )
                    return physical_line_idx
        
        raise ValueError(
            f"Строка с '{keyword}' не найдена в первых {max_lines_to_read} строках файла"
        )
    
    def _load_txt_file(self, file_path: Path) -> pd.DataFrame:
        """Загружает TXT-файл с автоопределением кодировки и заголовка."""
        encoding, encoding_errors = detect_txt_encoding(file_path)
        logger.debug(
            "Загрузка {}: кодировка {}, errors={}",
            file_path.name, encoding, encoding_errors,
        )
        
        header_row = self._find_header_row(
            file_path,
            'дата',
            encoding=encoding,
            errors=encoding_errors,
        )
        
        df = pd.read_csv(
            file_path,
            sep='\t',
            encoding=encoding,
            encoding_errors=encoding_errors,
            skiprows=range(header_row),
            header=0,
            skip_blank_lines=False,
            decimal=',',
            low_memory=False,
            # thousands='\xa0',
            # on_bad_lines='skip',
            # dtype=str,
            # engine='python',
        )
        
        return df.dropna(axis=1, how='all')

    # =========================================================================
    # БАЗОВАЯ ОБРАБОТКА
    # =========================================================================
    
    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Базовая обработка DataFrame: типы, заполнение, очистка."""
        required_cols = ['Дата', 'Документ', 'Содержание', 'Субконто Дт', 'Субконто Кт']
        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            raise ValueError(f"Отсутствуют обязательные столбцы: {missing}")
        
        other_cols = [c for c in df.columns if c not in ['Дата', 'Сумма']]
        type_mapping = {
            'string': other_cols,
            'numeric': ['Сумма'],
            'datetime': ['Дата'],
        }
        df = cast_columns_to_types(df, type_mapping)
        
        # Добавляем суффикс ТОЛЬКО к дубликатам документов
        mask_doc = df['Документ'].notna()
        if mask_doc.any():
            doc_counts = df.loc[mask_doc, 'Документ'].value_counts()
            duplicated_docs = doc_counts[doc_counts > 1].index
            
            dup_mask = mask_doc & df['Документ'].isin(duplicated_docs)
            if dup_mask.any():
                df.loc[dup_mask, 'Документ'] = (
                    df.loc[dup_mask, 'Документ']
                    + '_end'
                    + df.loc[dup_mask].groupby('Документ').cumcount().add(1).astype('string')
                )
        
        df['Дата'] = df['Дата'].ffill()
        df['Документ'] = df['Документ'].ffill()
        df = df[df['Дата'].notna()].copy()
        
        return df.dropna(how='all').dropna(how='all', axis=1)

    # =========================================================================
    # PIVOT И ОБЪЕДИНЕНИЕ
    # =========================================================================
    
    def _build_operations_pivot(self, df: pd.DataFrame, max_rows_per_doc: int = 30) -> pd.DataFrame:
        """Векторная обработка с защитой от OOM."""
        if df.empty:
            return pd.DataFrame()
    
        cols_to_fill = ['Содержание', 'Субконто Дт', 'Субконто Кт']
        fill_dict = {c: '' for c in cols_to_fill if c in df.columns}
        if fill_dict:
            df = df.fillna(fill_dict)
            
        df['_row_num'] = df.groupby(['Дата', 'Документ']).cumcount() + 1
        
        if max_rows_per_doc > 0:
            initial_len = len(df)
            df = df[df['_row_num'] <= max_rows_per_doc]
            dropped = initial_len - len(df)
            if dropped > 0:
                logger.warning(
                    "Отброшено {} строк из-за превышения лимита ({})",
                    dropped,
                    max_rows_per_doc,
                )
    
        attrs_cols = [c for c in ['Дт', 'Кт', 'Сумма'] if c in df.columns]
        if not attrs_cols:
            logger.warning("Отсутствуют столбцы Дт/Кт/Сумма")
        
        if attrs_cols:
            df_attrs = df.groupby(['Дата', 'Документ']).nth(0)[['Дата', 'Документ'] + attrs_cols].copy()
        else:
            df_attrs = df[['Дата', 'Документ']].drop_duplicates().reset_index(drop=True)
            
        pivot_cols = [c for c in cols_to_fill if c in df.columns]
        
        if pivot_cols:
            df_pivot = df.set_index(['Дата', 'Документ', '_row_num'])[pivot_cols].unstack('_row_num')
            
            if df_pivot.empty:
                logger.warning("Pivot таблица пуста после unstack")
                return df_attrs
            
            df_pivot.columns = [f'{col}_{num}' for col, num in df_pivot.columns]
            df_pivot = df_pivot.reset_index().fillna('')
            
            result = df_attrs.merge(df_pivot, on=['Дата', 'Документ'], how='left')
        else:
            result = df_attrs
            
        return result

    # =========================================================================
    # ФИНАЛИЗАЦИЯ
    # =========================================================================
    
    def _finalize_result(self, df: pd.DataFrame, file_path: Path) -> pd.DataFrame:
        """Финальная очистка и добавление служебных столбцов."""
        if df.empty:
            raise ValueError("Отчет по проводкам 1С пустой, обработка невозможна.")
        
        cols = list(df.columns)
        if 'Дата' in cols:
            cols.insert(0, cols.pop(cols.index('Дата')))
        df = df[cols]
        
        df = df.drop(columns=['Содержание'], errors='ignore')
        df.insert(0, 'Имя_файла', file_path.name)
        df['Имя_файла'] = df['Имя_файла'].astype('string')
        
        df = df.replace(r'^\s*$', pd.NA, regex=True).replace('', pd.NA)
        df['Документ'] = df['Документ'].str.replace(r'_end\d+$', '', regex=True)
        df = df[df['Сумма'].notna() & (df['Сумма'] != 0)]
        df = df.dropna(how='all').dropna(how='all', axis=1)
        
        return df

    # =========================================================================
    # ГЛАВНЫЙ МЕТОД
    # =========================================================================
    
    def process_file(self, file_path: Path, file_name: str):
        """Основной метод обработки TXT-файла отчёта по проводкам."""
        logger.debug("Начата обработка {}", file_path.name)
        
        df = self._load_txt_file(file_path)
        logger.debug('# 1. Загрузка')
        
        if df.empty:
            raise ValueError(f"Файл {file_path.name} пустой после загрузки")
        
        df = self._process_dataframe_optimized(df)
        logger.debug('# 2. Базовая обработка')
        
        result = self._build_operations_pivot(df)
        logger.debug('# 3. Pivot и объединение')
        
        result = self._finalize_result(result, file_path)
        logger.debug('# 4. Финализация')
        
        logger.debug("Обработка {} завершена: {} операций", file_path.name, len(result))
        
        return result, pd.DataFrame()


class Posting_NonUPPFileProcessor(FileProcessor):
    """Обработчик для файлов из 1С (не УПП)"""


    @staticmethod
    def _split_and_expand(df: pd.DataFrame, col_name: str, prefix: str) -> None:
        """Оптимизированное разбиение столбца с разделителем \n"""
        if col_name not in df.columns:
            return
            
        new_cols = df[col_name].str.split('\n', expand=True)
        if new_cols.empty:
            df.drop(columns=[col_name], inplace=True)
            return
            
        n_cols = new_cols.shape[1]
        new_cols.columns = [f'{prefix}_{i+1}' for i in range(n_cols)]
        df[new_cols.columns] = new_cols
        df.drop(columns=[col_name], inplace=True)
    
    @staticmethod    
    def _rename_columns_after_pokaz(df: pd.DataFrame) -> pd.DataFrame:
        """Корректировка столбцов для версии ERP"""
        # Поиск столбца "Показ"
        pokaz_cols = [col for col in df.columns if str(col).startswith("Показ")]
        if not pokaz_cols:
            return df
            
        pokaz_idx = df.columns.get_loc(pokaz_cols[0])
        
        # Проверка следующих 4 столбцов
        if pokaz_idx + 4 >= len(df.columns):
            return df
            
        # Проверка пустых имен
        next_cols = df.columns[pokaz_idx+1:pokaz_idx+5]
        if not all(pd.isna(col) for col in next_cols):
            return df
            
        # Переименование
        new_names = ["Дебет", "Дебет_значение", "Кредит", "Кредит_значение"]
        cols = list(df.columns)
        for i, new_name in enumerate(new_names, start=1):
            cols[pokaz_idx + i] = new_name
            
        df.columns = cols
        return df

    def process_file(self, file_path: Path) -> pd.DataFrame:
        fixed_data = fix_1c_excel_case(file_path)
        df = pd.read_excel(fixed_data, header=None)
        df.dropna(axis=1, how='all', inplace=True)

        # Поиск строки с заголовками
        period_rows = df.index[df.iloc[:, 0] == 'Период'].tolist()
        if not period_rows:
            raise RegisterProcessingError('Не найден заголовок Период в шапке таблицы')
            
        header_row = period_rows[0]
        df.columns = df.iloc[header_row]
        df = df.iloc[header_row + 1:].reset_index(drop=True)
        
        # Обработка специальных разделов
        df_with_col = pd.DataFrame()
        df_with_currency = pd.DataFrame()
        
        pokaz_cols = [col for col in df.columns if str(col).startswith('Показ')]
        if pokaz_cols:
            col_name = pokaz_cols[0]
            
            # Обработка количества
            if (df[col_name] == 'Кол.').any():
                df_with_col = df[df[col_name]=='Кол.'].copy()
                if not df_with_col.empty:
                    try:
                        dt_idx = df_with_col.columns.get_loc('Дебет')
                        df_with_col['Дебет_количество'] = pd.to_numeric(
                            df_with_col.iloc[:, dt_idx + 1], errors='coerce').fillna(0)
                    except (KeyError, IndexError):
                        pass
                        
                    try:
                        kt_idx = df_with_col.columns.get_loc('Кредит')
                        df_with_col['Кредит_количество'] = pd.to_numeric(
                            df_with_col.iloc[:, kt_idx + 1], errors='coerce').fillna(0)
                    except (KeyError, IndexError):
                        pass
                    
                    # Фильтрация валидных колонок
                    cols = ['Дебет_количество', 'Кредит_количество']
                    df_with_col = df_with_col[[col for col in cols if col in df_with_col.columns]].copy()
                    df_with_col = df_with_col.iloc[:-1]  # Удаление последней строки

            # Обработка валюты
            if (df[col_name] == 'Вал.').any():
                df_with_currency = df[df[col_name]=='Вал.'].copy()
                if not df_with_currency.empty:
                    try:
                        dt_idx = df_with_currency.columns.get_loc('Дебет')
                        df_with_currency['Дебет_валюта'] = df_with_currency.iloc[:, dt_idx + 1]
                        df_with_currency['Дебет_валютное_количество'] = pd.to_numeric(
                            df_with_currency.iloc[:, dt_idx + 2], errors='coerce').fillna(0)
                    except (KeyError, IndexError):
                        pass
                        
                    try:
                        kt_idx = df_with_currency.columns.get_loc('Кредит')
                        df_with_currency['Кредит_валюта'] = df_with_currency.iloc[:, kt_idx + 1]
                        df_with_currency['Кредит_валютное_количество'] = pd.to_numeric(
                            df_with_currency.iloc[:, kt_idx + 2], errors='coerce').fillna(0)
                    except (KeyError, IndexError):
                        pass
                    
                    cols = ['Дебет_валюта', 'Дебет_валютное_количество', 
                           'Кредит_валюта', 'Кредит_валютное_количество']
                    df_with_currency = df_with_currency[[col for col in cols if col in df_with_currency.columns]].copy()
                    df_with_currency = df_with_currency.iloc[:-1]
        
        # Фильтрация по дате
        df['Период'] = pd.to_datetime(df['Период'], format='%d.%m.%Y', errors='coerce')
        df = df[df['Период'].notna()].copy().reset_index(drop=True)
        
        # Добавление специальных разделов
        for section_df in [df_with_col, df_with_currency]:
            if not section_df.empty and len(section_df) == len(df):
                df = pd.concat([df, section_df.reset_index(drop=True)], axis=1)
        
        # Дополнительная обработка
        df.dropna(axis=1, how='all', inplace=True)
        df = self._rename_columns_after_pokaz(df)
        
        
        
        # Разбиение столбцов
        for col_prefix in ['Документ', 'Аналитика Дт', 'Аналитика Кт']:
            self._split_and_expand(df, col_prefix, col_prefix)
            # self._split_and_expand(df, col_prefix, col_prefix.replace(' ', '_'))
        
        
        # Переименование колонок
        new_columns = []
        cols = df.columns.tolist()
        for i, col in enumerate(cols):
            if pd.isna(col) or col == '':
                new_name = f'{cols[i-1]}_значение' if i > 0 else 'NoNameCol0'
                new_columns.append(new_name)
            else:
                new_columns.append(col)
                
        df.columns = new_columns
        
        # Очистка
        df.dropna(how='all', inplace=True)
        df.dropna(how='all', axis=1, inplace=True)

        # Добавление имени файла
        df.insert(0, 'Имя_файла', file_path.name)
        
        if df.empty:
            raise RegisterProcessingError("Отчет по проводкам 1с пустой, обработка невозможна.")
            
        return df, self.table_for_check
