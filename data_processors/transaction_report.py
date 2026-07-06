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
# import numpy as np
import pandas as pd
from pathlib import Path
from loguru import logger
from utils import cast_columns_to_types

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
        max_lines_to_read: int = 50
    ) -> int:
        """Находит физический номер строки с заголовком."""
        keyword_lower = keyword.lower()
        
        with open(file_path, 'r', encoding=encoding) as f:
            for physical_line_idx, line in enumerate(f):
                if physical_line_idx >= max_lines_to_read:
                    break
                if keyword_lower in line.lower():
                    logger.debug(
                        f"Заголовок найден на строке {physical_line_idx}: "
                        f"{line.strip()[:50]}..."
                    )
                    return physical_line_idx
        
        raise ValueError(
            f"Строка с '{keyword}' не найдена в первых {max_lines_to_read} строках файла"
        )
    
    def _load_txt_file(self, file_path: Path) -> pd.DataFrame:
        """Загружает TXT-файл с автоопределением заголовка."""
        header_row = self._find_header_row(file_path, 'дата')
        
        df = pd.read_csv(
            file_path,
            sep='\t',
            encoding='cp1251',
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
                    f"Отброшено {dropped} строк из-за превышения лимита ({max_rows_per_doc})"
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
        logger.debug(f"Начата обработка {file_path.name}")
        
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
        
        logger.debug(f"Обработка {file_path.name} завершена: {len(result)} операций")
        
        return result, pd.DataFrame()

# import numpy as np
# import pandas as pd
# from pathlib import Path
# from loguru import logger
# from utils import cast_columns_to_types

# from data_processors.file_processor import FileProcessor

# pd.set_option('future.no_silent_downcasting', True)


# class Posting_UPPFileProcessor(FileProcessor):
#     """Обработчик для файлов отчётов по проводкам из 1С УПП (TXT-формат)."""

#     # =========================================================================
#     # ЗАГРУЗКА ФАЙЛА
#     # =========================================================================
    
#     @staticmethod
#     def _find_header_row(
#         file_path: Path, 
#         keyword: str = 'дата', 
#         encoding: str = 'cp1251',
#         max_lines_to_read: int = 50
#     ) -> int:
#         """Находит физический номер строки с заголовком."""
#         keyword_lower = keyword.lower()
        
#         with open(file_path, 'r', encoding=encoding) as f:
#             for physical_line_idx, line in enumerate(f):
#                 if physical_line_idx >= max_lines_to_read:
#                     break
#                 if keyword_lower in line.lower():
#                     return physical_line_idx
        
#         raise ValueError(
#             f"Строка с '{keyword}' не найдена в первых {max_lines_to_read} строках файла"
#         )
    
#     def _load_txt_file(self, file_path: Path) -> pd.DataFrame:
#         """Загружает TXT-файл с автоопределением заголовка."""
#         header_row = self._find_header_row(file_path, 'дата')
        
#         df = pd.read_csv(
#             file_path,
#             sep='\t',
#             encoding='cp1251',
#             skiprows=range(header_row),
#             header=0,
#             skip_blank_lines=False,
#             decimal=',',
#             thousands='\xa0',
#             on_bad_lines='skip',         # ★ ПРОПУСКАЕМ битые строки (подписи в конце)
#             dtype=str,
#             keep_default_na=False,
#             engine='python',             # Python-движок корректно обрабатывает on_bad_lines
#         )
        
#         return df.dropna(axis=1, how='all')


#     # =========================================================================
#     # БАЗОВАЯ ОБРАБОТКА
#     # =========================================================================
    
#     def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
#         """Базовая обработка DataFrame: типы, заполнение, очистка."""
#         # Проверка обязательных столбцов
#         required_cols = ['Дата', 'Документ', 'Содержание', 'Субконто Дт', 'Субконто Кт']
#         missing = [col for col in required_cols if col not in df.columns]
#         if missing:
#             raise ValueError(f"Отсутствуют обязательные столбцы: {missing}")
        
#         # Приведение типов
#         other_cols = [c for c in df.columns if c not in ['Дата', 'Сумма']]
#         type_mapping = {
#             'string': other_cols,
#             'numeric': ['Сумма'],
#             'datetime': ['Дата'],
#         }
#         df = cast_columns_to_types(df, type_mapping)
        
#         # ★ УБРАЛИ хак с _end — он не нужен для pivot_table
        
#         df = df.replace(
#             r'^\s*$', pd.NA, regex=True
#         ).replace('', pd.NA)
        
#         # 3. Добавляем суффикс ТОЛЬКО к дубликатам документов
#         mask_doc = df['Документ'].notna()
#         if mask_doc.any():
#             doc_counts = df.loc[mask_doc, 'Документ'].value_counts()
#             duplicated_docs = doc_counts[doc_counts > 1].index
            
#             dup_mask = mask_doc & df['Документ'].isin(duplicated_docs)
#             if dup_mask.any():
#                 df.loc[dup_mask, 'Документ'] = (
#                     df.loc[dup_mask, 'Документ']
#                     + '_end'
#                     + df.loc[dup_mask].groupby('Документ').cumcount().add(1).astype('string')
#                 )
        
#         # Forward fill для даты и документа
#         df['Дата'] = df['Дата'].ffill()
#         df['Документ'] = df['Документ'].ffill()
        
#         # Удаление строк без даты
#         df = df[df['Дата'].notna()].copy()
        
#         return df.dropna(how='all').dropna(how='all', axis=1)

#     # =========================================================================
#     # PIVOT И ОБЪЕДИНЕНИЕ
#     # =========================================================================
    
#     def _build_operations_pivot(self, df: pd.DataFrame, max_rows_per_doc: int = 50) -> pd.DataFrame:
#         """
#         Векторная обработка с защитой от OOM (Out Of Memory).
        
#         :param max_rows_per_doc: Максимальное число строк на документ. 
#                                  Если в документе больше строк, они будут отброшены.
#         """
#         if df.empty:
#             return pd.DataFrame()
    
#         # Заполняем пропуски
#         cols_to_fill = ['Содержание', 'Субконто Дт', 'Субконто Кт']
#         fill_dict = {c: '' for c in cols_to_fill if c in df.columns}
#         if fill_dict:
#             df = df.fillna(fill_dict)
            
#         # 1. Нумеруем строки внутри каждого документа
#         df['_row_num'] = df.groupby(['Дата', 'Документ']).cumcount() + 1
        
#         # 🛡️ 2. ЗАЩИТА ОТ АНОМАЛИЙ: Отбрасываем всё, что превышает лимит
#         # Это векторная операция, она не требует циклов и выполняется мгновенно.
#         if max_rows_per_doc > 0:
#             initial_len = len(df)
#             df = df[df['_row_num'] <= max_rows_per_doc]
#             dropped = initial_len - len(df)
#             if dropped > 0:
#                 print(f"⚠️ Отброшено {dropped} строк из-за превышения лимита ({max_rows_per_doc}) в аномальных документах.")
    
#         # 3. Атрибуты из первой строки (nth(0) берет строго первую)
#         attrs_cols = [c for c in ['Дт', 'Кт', 'Сумма'] if c in df.columns]
#         if attrs_cols:
#             df_attrs = df.groupby(['Дата', 'Документ']).nth(0)[['Дата', 'Документ'] + attrs_cols].copy()
#         else:
#             df_attrs = df[['Дата', 'Документ']].drop_duplicates().reset_index(drop=True)
            
#         # 4. Pivot (разворот)
#         pivot_cols = [c for c in cols_to_fill if c in df.columns]
        
#         if pivot_cols:
#             df_pivot = df.set_index(['Дата', 'Документ', '_row_num'])[pivot_cols].unstack('_row_num')
#             df_pivot.columns = [f'{col}_{num}' for col, num in df_pivot.columns]
#             df_pivot = df_pivot.reset_index().fillna('')
            
#             result = df_attrs.merge(df_pivot, on=['Дата', 'Документ'], how='left')
#         else:
#             result = df_attrs
            
#         return result

#     @staticmethod
#     def _keep_first_unique_per_row(df: pd.DataFrame) -> pd.DataFrame:
#         """
#         Векторизованная очистка дубликатов в строках.
#         Оставляет первое вхождение значения, остальные заменяет на NaN.
#         """
#         if df.empty:
#             return df
        
#         # Работаем только со столбцами pivoted (Содержание_N, Субконто_*)
#         # Исключаем ключевые столбцы
#         key_cols = ['Дата', 'Документ']
#         data_cols = [c for c in df.columns if c not in key_cols]
        
#         if not data_cols:
#             return df
        
#         result = df.copy()
#         data_df = df[data_cols]
        
#         # Транспонируем и применяем duplicated по строкам
#         dup_mask = data_df.T.apply(
#             lambda col: col.duplicated(keep='first') & col.notna()
#         ).T
        
#         result.loc[:, data_cols] = data_df.mask(dup_mask, np.nan)
        
#         return result

#     # =========================================================================
#     # ФИНАЛИЗАЦИЯ
#     # =========================================================================
    
#     def _finalize_result(self, df: pd.DataFrame, file_path: Path) -> pd.DataFrame:
#         """Финальная очистка и добавление служебных столбцов."""
#         if df.empty:
#             raise ValueError("Отчет по проводкам 1С пустой, обработка невозможна.")
        
#         # Реорганизация: Дата — первая
#         cols = list(df.columns)
#         if 'Дата' in cols:
#             cols.insert(0, cols.pop(cols.index('Дата')))
#         df = df[cols]
        
#         # Удаление ненужных столбцов
#         df = df.drop(columns=['Содержание'], errors='ignore')
        
#         # Добавление имени файла
#         df.insert(0, 'Имя_файла', file_path.name)
        
#         # ОДНА векторизованная замена пустых значений
#         df = df.replace(
#             r'^\s*$', pd.NA, regex=True
#         ).replace('', pd.NA)
        
#         # Удаление _end в конце текста в поле Документ
#         df['Документ'] = df['Документ'].str.replace(r'_end\d+$', '', regex=True)
        
#         # Удаление строк с 0 в поле Сумма
#         df = df[df['Сумма'].notna() & (df['Сумма'] != 0)]
        
#         # Удаление полностью пустых строк/столбцов
#         df = df.dropna(how='all').dropna(how='all', axis=1)
        
#         return df

#     # =========================================================================
#     # ГЛАВНЫЙ МЕТОД
#     # =========================================================================
    
#     def process_file(self, file_path: Path, file_name: str):
#         """Основной метод обработки TXT-файла отчёта по проводкам."""
#         logger.debug(f"Начата обработка {file_path.name}")
        
#         # 1. Загрузка
#         df = self._load_txt_file(file_path)
        
#         logger.info('# 1. Загрузка')
        
#         if df.empty:
#             raise ValueError(f"Файл {file_path.name} пустой после загрузки")
        
#         # 2. Базовая обработка
#         df = self._process_dataframe_optimized(df)
#         logger.info('# 2. Базовая обработка')
        
#         # 3. Pivot и объединение
#         result = self._build_operations_pivot(df)
#         logger.info('# 3. Pivot и объединение')
        
#         # 4. Финализация
#         result = self._finalize_result(result, file_path)
#         logger.info('# 4. Финализация')
        
#         logger.debug(
#             f"Обработка {file_path.name} завершена: {len(result)} операций"
#         )
#         # result.to_excel(f'{file_name}.xlsx')
#         # ★ ИСПРАВЛЕНО: self.table_for_check не определён
#         return result, pd.DataFrame()

# class Posting_UPPFileProcessor(FileProcessor):
#     """Обработчик для файлов из 1С УПП"""
    
#     @staticmethod
#     def _fast_keep_first_unique_per_row(df: pd.DataFrame) -> pd.DataFrame:
#         """
#         Оптимизированная версия без использования stack().
#         Оставляет только первое вхождение значения в строке, остальные заменяет на NaN.
#         Работает во всех версиях Pandas.
#         """
#         result = df.copy()
        
#         # Применяем к каждой строке
#         for idx in df.index:
#             row = df.loc[idx]
#             # Находим дубликаты в строке
#             duplicates = row.duplicated()
#             # Заменяем дубликаты на NaN
#             result.loc[idx, duplicates] = np.nan
        
#         return result

#     @staticmethod
#     def _process_quantity_section(df: pd.DataFrame) -> pd.DataFrame:
#         """Обработка раздела с количеством"""
#         try:
#             if not (df['Содержание'] == 'Количество').any():
#                 return pd.DataFrame()
#         except KeyError:
#             raise ValueError('Не найден или пустой столбец Содержание в Отчете по проводка из УПП баз.')
            
#         df_with_count = df[df['Содержание'] == 'Количество'].copy()
        
#         # Получаем индексы столбцов 'Дт' и 'Кт'
#         dt_idx = df_with_count.columns.get_loc('Дт')
#         kt_idx = df_with_count.columns.get_loc('Кт')
        
#         # Преобразование в числовые типы
#         dt_col = pd.to_numeric(df_with_count.iloc[:, dt_idx], errors='coerce').fillna(0)
#         dt_next_col = pd.to_numeric(df_with_count.iloc[:, dt_idx + 1], errors='coerce').fillna(0)
        
#         kt_col = pd.to_numeric(df_with_count.iloc[:, kt_idx], errors='coerce').fillna(0)
#         kt_next_col = pd.to_numeric(df_with_count.iloc[:, kt_idx + 1], errors='coerce').fillna(0)
        
#         # Суммирование
#         df_with_count.loc[:, 'Дт_количество'] = dt_col + dt_next_col
#         df_with_count.loc[:, 'Кт_количество'] = kt_col + kt_next_col
        
#         return df_with_count[['Документ', 'Дт_количество', 'Кт_количество']]

#     @staticmethod
#     def _process_currency_section(df: pd.DataFrame) -> pd.DataFrame:
#         """Обработка раздела с валютой"""
#         try:
#             if not (df['Содержание'] == 'Валюта').any():
#                 return pd.DataFrame()
#         except KeyError:
#             raise ValueError('Не найден или пустой столбец Содержание в Отчете по проводка из УПП баз.')
            
#         df_with_currency = df[df['Содержание'] == 'Валюта'].copy()
        
#         # Получаем индексы столбцов 'Дт' и 'Кт'
#         dt_idx = df_with_currency.columns.get_loc('Дт')
#         kt_idx = df_with_currency.columns.get_loc('Кт')
        
#         # Комплексная замена пустых значений
#         df_with_currency.replace(['', '\n', '\t', ' ', r'^\s+$'], np.nan, 
#                                inplace=True)
#         df_with_currency.replace([r'^\s+$'], np.nan, 
#                                inplace=True, regex=True)
        
#         # Создание новых столбцов
#         df_with_currency['Дт_валюта'] = df_with_currency.iloc[:, dt_idx]
#         df_with_currency['Дт_валюта_количество'] = df_with_currency.iloc[:, dt_idx + 1]
#         df_with_currency['Кт_валюта'] = df_with_currency.iloc[:, kt_idx]
#         df_with_currency['Кт_валюта_количество'] = df_with_currency.iloc[:, kt_idx + 1]
        
#         return df_with_currency[['Документ', 'Дт_валюта', 'Дт_валюта_количество', 
#                                'Кт_валюта', 'Кт_валюта_количество']]
    
#     @staticmethod
#     def _find_header_row(
#         file_path: Path, 
#         keyword: str = 'дата', 
#         encoding: str = 'cp1251',
#         max_lines_to_read: int = 50
#     ) -> int:
#         """
#         Находит ФИЗИЧЕСКИЙ номер строки с заголовком в файле.
        
#         Возвращает номер строки (0-indexed), учитывая ВСЕ строки, включая пустые.
#         Это необходимо для корректной работы с pd.read_csv(skiprows=...).
        
#         Args:
#             file_path: Путь к TXT-файлу
#             keyword: Ключевое слово для поиска
#             encoding: Кодировка файла
#             max_lines_to_read: Максимум строк для чтения
            
#         Returns:
#             Физический номер строки с заголовком (0-indexed)
#         """
#         keyword_lower = keyword.lower()
        
#         with open(file_path, 'r', encoding=encoding) as f:
#             for physical_line_idx, line in enumerate(f):
#                 if physical_line_idx >= max_lines_to_read:
#                     break
                
#                 if keyword_lower in line.lower():
#                     logger.debug(
#                         f"Заголовок '{keyword}' найден на физической строке {physical_line_idx}"
#                     )
#                     return physical_line_idx
        
#         raise ValueError(
#             f"Строка с '{keyword}' не найдена в первых {max_lines_to_read} строках файла"
#         )
    
#     @staticmethod
#     def _process_dataframe_optimized(df: pd.DataFrame) -> pd.DataFrame:
#         """Оптимизированная обработка DataFrame отчёта по проводкам."""
        
#         # 1. Проверка обязательных столбцов
#         required_cols = ['Дата', 'Документ']
#         missing = [col for col in required_cols if col not in df.columns]
#         if missing:
#             raise ValueError(
#                 f"В выгрузке отсутствуют обязательные столбцы: {missing}. "
#                 f"Доступные столбцы: {list(df.columns)}"
#             )
        
#         # 2. Преобразуем типы данных в столбцах
#         date_col = 'Дата'
#         sum_col = 'Сумма'
#         other_col = list(set(df.columns) - set([date_col, sum_col]))
#         type_mapping = {
#             'string': other_col,
#             'numeric': [sum_col],
#             'datetime': [date_col]
#             }
#         df = cast_columns_to_types(df, type_mapping)
        
#         print(df.info())
        
#         # 3. Добавляем суффикс ТОЛЬКО к дубликатам документов
#         mask_doc = df['Документ'].notna()
#         if mask_doc.any():
#             doc_counts = df.loc[mask_doc, 'Документ'].value_counts()
#             duplicated_docs = doc_counts[doc_counts > 1].index
            
#             dup_mask = mask_doc & df['Документ'].isin(duplicated_docs)
#             if dup_mask.any():
#                 df.loc[dup_mask, 'Документ'] = (
#                     df.loc[dup_mask, 'Документ']
#                     + '_end'
#                     + df.loc[dup_mask].groupby('Документ').cumcount().add(1).astype(str)
#                 )
        
#         # 7. Заполнение пропусков (forward fill для сгруппированных данных)
#         df['Дата'] = df['Дата'].ffill()
#         df['Документ'] = df['Документ'].ffill()
        
#         # 8. Удаление пустых строк и столбцов
#         df = df[df['Дата'].notna()].copy()
#         df = df.dropna(how='all').dropna(how='all', axis=1)
        
#         return df
    
#     def process_file(self, file_path: Path, file_name: str) -> pd.DataFrame:
#         """Основной метод обработки файла УПП"""

#         # Использование
#         header_row = Posting_UPPFileProcessor._find_header_row(file_path, 'дата')
        
#         df = pd.read_csv(
#             file_path,
#             sep='\t',
#             encoding='cp1251',
#             skiprows=range(header_row),  # Пропускаем все строки ДО заголовка
#             header=0,                     # Первая строка после skiprows = заголовок
#             skip_blank_lines=False,       # НЕ пропускаем пустые (они уже пропущены через skiprows)
#             decimal=',',
#             thousands='\xa0',
#             on_bad_lines='skip',
#             dtype=str
#         )
        
#         df = df.dropna(axis=1, how='all')
        
#         # Обработка DataFrame
#         df = self._process_dataframe_optimized(df)

#         # Обработка специальных разделов
#         # df_with_count = self._process_quantity_section(df)
#         # df_with_currency = self._process_currency_section(df)
        
#         if df.empty:
#             raise ValueError("Отчет по проводкам 1с пустой в файле, обработка невозможна.")

#         # Подготовка к pivot
#         df = df.fillna({'Содержание': '', 'Субконто Дт': '', 'Субконто Кт': ''})
        
#         # Создание сводной таблицы
#         operations_pivot = (
#             df.assign(row_num=df.groupby(['Дата', 'Документ']).cumcount() + 1)
#             .pivot_table(
#                 index=['Дата', 'Документ'], 
#                 columns='row_num', 
#                 values=['Содержание', 'Субконто Дт', 'Субконто Кт'], 
#                 aggfunc='first', 
#                 fill_value=''
#             )
#             .reset_index()
#         )
        
#         # Удаление служебных строк
#         df = df[~df['Содержание'].isin(['Количество', 'Валюта'])]
#         # Обработка дубликатов
#         operations_pivot = self._fast_keep_first_unique_per_row(operations_pivot)
#         operations_pivot = operations_pivot.dropna(how='all').dropna(how='all', axis=1)
    
#         # Упрощение мультииндекса
#         operations_pivot.columns = [
#             '_'.join(map(str, col)).strip() if isinstance(col, tuple) else col
#             for col in operations_pivot.columns.values
#         ]
        
#         operations_pivot.columns = [col.rstrip('_') if col.endswith('_') else col for col in operations_pivot.columns]

#         # Атрибуты документов (без дубликатов)
#         doc_attributes = (
#             df.drop_duplicates(subset=['Документ', 'Дт', 'Кт'])
#             .set_index('Документ')
#             .drop(columns=['Дата', 'Субконто Дт', 'Субконто Кт'])
#         )
        

#         # Объединение результатов
#         result = doc_attributes.join(operations_pivot.set_index('Документ'), how='left')
        
#         # Добавление специальных разделов
#         # for section_df in [df_with_count, df_with_currency]:
#         #     if not section_df.empty:
#         #         result = result.join(section_df.set_index('Документ'), how='left')
        
#         result = result.reset_index()
        
        
#         # Финальная очистка
#         # result = result.dropna(subset=['Дт', 'Кт'], how='all')
#         result = result.dropna(subset=['Дт', 'Кт'], how='any')
#         result['Документ'] = result['Документ'].str.replace(r'_end\d+$', '', regex=True)
#         result = result.dropna(how='all').dropna(how='all', axis=1)
        
#         # Переименование колонок
#         # new_columns = []
#         # cols = result.columns.tolist()
#         # for i, col in enumerate(cols):
#         #     if str(col).startswith("NoNameCol"):
#         #         new_name = f'{cols[i-1]}_значение' if i > 0 else 'NoNameCol0'
#         #         new_columns.append(new_name)
#         #     else:
#         #         new_columns.append(col)
        
#         # result.columns = new_columns
        
        
        
#         # Реорганизация колонок
#         updated_cols = list(result.columns)
#         updated_cols.insert(0, updated_cols.pop(updated_cols.index('Дата')))
#         result = result[updated_cols]
#         result = result.drop(columns=['Содержание'], errors='ignore')
        
#         # Добавление имени файла
#         result['Имя_файла'] = file_path.name
#         updated_cols = ['Имя_файла'] + [col for col in result.columns if col != 'Имя_файла']
#         result = result[updated_cols]
        
#         # Замена пустых значений
#         result.replace(
#             ['', '\n', '\t', ' ', r'^\s+$'], 
#             pd.NA, 
#             inplace=True,
#         )
#         result.replace(
#             [r'^\s+$'], 
#             pd.NA, 
#             inplace=True,
#             regex=True
#         )
        
#         # Удаление пустых строк и столбцов
#         result.dropna(how='all', inplace=True)
#         result.dropna(how='all', axis=1, inplace=True)
        
#         return result, self.table_for_check
    

class Posting_NonUPPFileProcessor(FileProcessor):
    """Обработчик для файлов из 1С (не УПП)"""


# for col_prefix in ['Документ', 'Аналитика Дт', 'Аналитика Кт']:
#     self._split_and_expand(df, 'Аналитика Дт', 'Аналитика_Дт')



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