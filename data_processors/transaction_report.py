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
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Tuple
from loguru import logger
from utils import cast_columns_to_types, detect_txt_encoding, normalize_ragged_tab_rows
from utils.dataframe_utils import find_header_index

from data_processors.file_processor import FileProcessor
from pipeline.errors import InputDataError

# Заглушка для пустых субконто — значения из 1С; совпадает с Values.UNSPECIFIED
# в pipeline/constants.py (не импортируем pipeline из низкоуровневого пакета)
UNSPECIFIED = 'не_указано'

pd.set_option('future.no_silent_downcasting', True)

class PostingTXTFileProcessor(FileProcessor):
    """Базовый обработчик TXT-выгрузок отчётов по проводкам (загрузка файла)."""

    HEADER_KEYWORD = 'дата'

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

        raise InputDataError(
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
            self.HEADER_KEYWORD,
            encoding=encoding,
            errors=encoding_errors,
        )

        # Нормализация «рваных» строк: склейка табуляций, попавших внутрь
        # свободно-текстовых полей (иначе pd.read_csv падает с ParserError).
        # Позиция разрыва определяется по данным — см. normalize_ragged_tab_rows.

        df_source, repaired, unresolved = normalize_ragged_tab_rows(
            file_path,
            header_row,
            encoding=encoding,
            errors=encoding_errors,
        )
        if unresolved > 0:
            logger.warning(
                "Отчёт по проводкам {}: {} строк не восстановлены и будут "
                "отброшены при разборе. Проверьте выгрузку из 1С.",
                file_path.name, unresolved,
            )

        df = pd.read_csv(
            df_source,
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

        return df


class Posting_UPPFileProcessor(PostingTXTFileProcessor):
    """Обработчик для файлов отчётов по проводкам из 1С УПП (TXT-формат)."""

    HEADER_KEYWORD = 'дата'

    # =========================================================================
    # БАЗОВАЯ ОБРАБОТКА
    # =========================================================================
    
    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Базовая обработка DataFrame: типы, заполнение, очистка."""
        required_cols = ['Дата', 'Документ', 'Содержание', 'Субконто Дт', 'Субконто Кт']
        missing = [col for col in required_cols if col not in df.columns]
        if missing:
            # ★ ИСПРАВЛЕНИЕ: пустой отчёт по проводкам (нет обязательных колонок)
            # — не фатальная ошибка, а warning + пустой DataFrame.
            # Это защищает от падения, когда бухгалтер выгрузил пустой отчёт
            # (например, по счёту 90.07 без оборотов — файл 616 байт, только заголовки).
            logger.warning(
                "Отчёт по проводкам: отсутствуют обязательные столбцы {}. "
                "Файл пустой или некорректный — пропускаем.",
                missing,
            )
            return pd.DataFrame()
        
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
            # ★ ИСПРАВЛЕНИЕ: пустой результат после обработки — не фатальная ошибка,
            # а warning + пустой DataFrame. Защита от пустых отчётов по проводкам.
            logger.warning(
                "Отчёт по проводкам {} пустой после обработки — пропускаем.",
                file_path.name,
            )
            return pd.DataFrame()
        
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
        
        # Пустые субконто УПП-отчёта (pd.NA / пустая строка) заменяем штатной
        # заглушкой 'не_указано'. Это согласует субконто-значения со справочниками,
        # в которых пустые значения имеют заглушку, и защищает хрупкий маппинг:
        # Субконто Дт_1/Кт_1 -> доход_расход (шаг 17, справочник Меппинг_опу),
        # ном_группа/контрагент (шаги 14/15/16). Непустые значения не трогаем.
        subconto_cols = [
            col for col in df.columns
            if col.startswith('Субконто Дт') or col.startswith('Субконто Кт')
        ]
        if subconto_cols:
            df[subconto_cols] = df[subconto_cols].replace(['', pd.NA], UNSPECIFIED)
        
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
            raise InputDataError(f"Файл {file_path.name} пустой после загрузки")
        
        df = self._process_dataframe_optimized(df)
        logger.debug('# 2. Базовая обработка')
        
        result = self._build_operations_pivot(df)
        logger.debug('# 3. Pivot и объединение')
        
        result = self._finalize_result(result, file_path)
        logger.debug('# 4. Финализация')
        
        logger.debug("Обработка {} завершена: {} операций", file_path.name, len(result))
        
        return result, pd.DataFrame()

class Posting_NonUPPFileProcessor(PostingTXTFileProcessor):
    """Обработчик для файлов отчётов по проводкам из 1С не-УПП (TXT-формат)."""

    HEADER_KEYWORD = 'период'

    # =========================================================================
    # ЗАГРУЗКА ФАЙЛА
    # =========================================================================

    @staticmethod
    def _find_header_row(
        file_path: Path,
        keyword: str = 'период',
        encoding: str = 'cp1251',
        max_lines_to_read: int = 50,
        errors: str = 'strict',
    ) -> int:
        """Находит физический номер строки с заголовком.

        Для не-УПП выгрузок заголовок — строка таблицы, где одна из ячеек
        в точности равна ключевому слову (например, 'Период'). Подстрока не
        используется, чтобы не ошибиться на служебной строке 'Период: ...'.
        """
        keyword_lower = keyword.lower()

        with open(file_path, 'r', encoding=encoding, errors=errors) as f:
            for physical_line_idx, line in enumerate(f):
                if physical_line_idx >= max_lines_to_read:
                    break
                cells = [cell.strip().lower() for cell in line.rstrip('\n\r').split('\t')]
                if keyword_lower in cells:
                    logger.debug(
                        "Заголовок найден на строке {}: {}...",
                        physical_line_idx,
                        line.strip()[:50],
                    )
                    return physical_line_idx

        raise InputDataError(
            f"Строка с '{keyword}' не найдена в первых {max_lines_to_read} строках файла"
        )

    # =========================================================================
    # БАЗОВАЯ ОБРАБОТКА
    # =========================================================================

    @staticmethod
    def _rename_columns_after_pokaz(df: pd.DataFrame) -> pd.DataFrame:
        """Корректировка столбцов для версии ERP."""
        pokaz_cols = [col for col in df.columns if str(col).startswith('Показ')]
        if not pokaz_cols:
            return df

        pokaz_idx = df.columns.get_loc(pokaz_cols[0])

        if pokaz_idx + 4 >= len(df.columns):
            return df

        next_cols = df.columns[pokaz_idx + 1:pokaz_idx + 5]
        if not all(pd.isna(col) for col in next_cols):
            return df

        new_names = ['Дебет', 'Дебет_значение', 'Кредит', 'Кредит_значение']
        cols = list(df.columns)
        for i, new_name in enumerate(new_names, start=1):
            cols[pokaz_idx + i] = new_name

        df.columns = cols
        return df

    @staticmethod
    def _split_and_expand(df: pd.DataFrame, col_name: str, prefix: str) -> None:
        """Оптимизированное разбиение столбца с разделителем \\n."""
        if col_name not in df.columns:
            return

        new_cols = df[col_name].str.split('\n', expand=True)
        if new_cols.empty:
            df.drop(columns=[col_name], inplace=True)
            return

        n_cols = new_cols.shape[1]
        new_cols.columns = [f'{prefix}_{i + 1}' for i in range(n_cols)]
        df[new_cols.columns] = new_cols
        df.drop(columns=[col_name], inplace=True)

    def _extract_quantity_currency_sections(
        self, df: pd.DataFrame
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """
        Вычленяет подтаблицы количества (Кол.) и валюты (Вал.) из раздела 'Показ...'.

        Требует колонок 'Дебет'/'Кредит', которые проставляет
        _rename_columns_after_pokaz: до его вызова после 'Показ...' идут
        безымянные столбцы (np.nan) и get_loc этих имён падает.
        """
        df_with_col = pd.DataFrame()
        df_with_currency = pd.DataFrame()

        pokaz_cols = [col for col in df.columns if str(col).startswith('Показ')]
        if not pokaz_cols:
            return df_with_col, df_with_currency

        col_name = pokaz_cols[0]

        for section_name, amount_cols in (
            ('Кол.', ['Дебет_количество', 'Кредит_количество']),
            (
                'Вал.',
                [
                    'Дебет_валюта', 'Дебет_валютное_количество',
                    'Кредит_валюта', 'Кредит_валютное_количество',
                ],
            ),
        ):
            if not (df[col_name] == section_name).any():
                continue

            section = df[df[col_name] == section_name].copy()
            if section.empty:
                continue

            dt_idx = find_header_index(section.columns, 'Дебет')
            kt_idx = find_header_index(section.columns, 'Кредит')
            if dt_idx is None or kt_idx is None:
                logger.warning(
                    "[!] Секция '{}' не извлечена: нет колонок Дебет/Кредит "
                    "(выполните _rename_columns_after_pokaz до извлечения секций)",
                    section_name,
                )
                continue

            has_currency = section_name == 'Вал.'
            for side, idx in (('Дебет', dt_idx), ('Кредит', kt_idx)):
                if has_currency:
                    # Валюта лежит в соседней колонке, количество — через одну.
                    section[f'{side}_валюта'] = section.iloc[:, idx + 1]
                    section[f'{side}_валютное_количество'] = pd.to_numeric(
                        section.iloc[:, idx + 2], errors='coerce'
                    ).fillna(0)
                else:
                    section[f'{side}_количество'] = pd.to_numeric(
                        section.iloc[:, idx + 1], errors='coerce'
                    ).fillna(0)

            section_result = section[amount_cols].iloc[:-1]  # без строки «Итоги»
            if has_currency:
                df_with_currency = section_result
            else:
                df_with_col = section_result

        return df_with_col, df_with_currency

    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:
        """Нормализация не-УПП отчёта по проводкам."""
        if 'Период' not in df.columns:
            raise InputDataError('Не найден заголовок "Период" в шапке таблицы')

        # Приводим «безымянные» столбцы pd.read_csv (Unnamed: N) к NA,
        # как в исходных выгрузках (иначе секции Кол./Вал. не распознаются)
        df.columns = pd.Index([
            col if isinstance(col, str) and col and not str(col).startswith('Unnamed:')
            else np.nan
            for col in df.columns
        ])

        # 1. Переименование столбцов после «Показ...» — обязательно до
        # извлечения секций Кол./Вал.: только здесь безымянные столбцы
        # получают имена 'Дебет'/'Кредит', по которым их и находят.
        df = self._rename_columns_after_pokaz(df)

        # 2. Обработка специальных разделов (Кол./Вал.)
        df_with_col, df_with_currency = self._extract_quantity_currency_sections(df)

        # 3. Фильтрация по дате
        df['Период'] = pd.to_datetime(df['Период'], format='%d.%m.%Y', errors='coerce')
        df = df[df['Период'].notna()].copy().reset_index(drop=True)

        # 4. Добавление специальных разделов
        for section_df in (df_with_col, df_with_currency):
            if section_df.empty:
                continue
            if len(section_df) != len(df):
                logger.warning(
                    "[!] Секция не добавлена: {} строк в секции против {} в отчёте",
                    len(section_df), len(df),
                )
                continue
            df = pd.concat([df, section_df.reset_index(drop=True)], axis=1)

        # 5. Разбиение многострочных столбцов
        # 5a. pd.read_csv выводит числовой dtype для колонок, где есть цифры
        # и пустые ячейки (напр., 'Аналитика Кт'). Приводим к строковому типу,
        # чтобы .str-разбиение не падало, как в исходных XLSX-выгрузках.
        for col_prefix in ['Документ', 'Аналитика Дт', 'Аналитика Кт']:
            if col_prefix in df.columns:
                df[col_prefix] = df[col_prefix].astype('string')
            self._split_and_expand(df, col_prefix, col_prefix)

        # 6. Переименование безымянных колонок («значение» предыдущего столбца)
        new_columns = []
        cols = df.columns.tolist()
        for i, col in enumerate(cols):
            if pd.isna(col) or col == '':
                new_name = f'{cols[i - 1]}_значение' if i > 0 else 'NoNameCol0'
                new_columns.append(new_name)
            else:
                new_columns.append(col)
        df.columns = new_columns

        return df

    # =========================================================================
    # ФИНАЛИЗАЦИЯ
    # =========================================================================

    def _finalize_result(self, df: pd.DataFrame, file_path: Path) -> pd.DataFrame:
        """Финальная очистка и добавление служебных столбцов."""
        df.dropna(how='all', inplace=True)
        df.dropna(axis=1, how='all', inplace=True)

        if df.empty:
            raise InputDataError('Отчет по проводкам 1с пустой, обработка невозможна.')

        df.insert(0, 'Имя_файла', file_path.name)

        return df

    # =========================================================================
    # ГЛАВНЫЙ МЕТОД
    # =========================================================================

    def process_file(self, file_path: Path, file_name: str):
        """Основной метод обработки TXT-файла отчёта по проводкам (не-УПП)."""
        logger.debug("Начата обработка {}", file_path.name)

        df = self._load_txt_file(file_path)
        logger.debug('# 1. Загрузка')

        if df.empty:
            raise InputDataError(f"Файл {file_path.name} пустой после загрузки")

        df = self._process_dataframe_optimized(df)
        logger.debug('# 2. Базовая обработка')

        df = self._finalize_result(df, file_path)
        logger.debug('# 3. Финализация')

        logger.debug("Обработка {} завершена: {} строк", file_path.name, len(df))

        return df, pd.DataFrame()
