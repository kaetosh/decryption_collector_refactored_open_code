"""
Шаг 13: Финальная сборка расшифровки баланса.

Выполняет:
1. Векторизованный маппинг всех строк ОСВ на счета ФО (финансовой отчётности)
2. Валидацию полноты маппинга
3. Формирование итогового баланса из плана счетов
"""

import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import MissingMappingError, ConvergenceError
from io_module import DataLoader
from config.settings import TOLERANCE


class Step13BuildBalanceBreakdownStep(Step):
    """
    Шаг 13: Финальная сборка расшифровки баланса.
    
    Собирает финальный отчёт с расшифровкой баланса на основе
    всех предыдущих классификаций и расчётов.
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    BALANCE_REPORT = 'Баланс'
    BALANCE_COL = 'сальдо, тыс.ед.'
    
    # Столбцы, по которым строится маппинг на счёт ФО
    MAPPING_KEYS = [
        'счет',
        'субконто',
        'вид_задолженности',
        'подвид_задолженности',
        'группа_ос_аренды_лизинга',
        'долгая_короткая_часть',
        'сегмент_биоактивов_для_01_02',
        'вид_связи',
        'инвест_договор',
    ]
    
    def __init__(self):
        super().__init__(
            name="Шаг 13: Сборка расшифровки баланса",
            description="Формирование финального отчёта"
        )
    
    # =========================================================================
    # МАППИНГ СЧЕТОВ НА СЧЕТ ФО
    # =========================================================================
    
    def _build_mapping_dict(self, mapping_df: pd.DataFrame) -> dict:
        """
        Строит словарь маппинга из справочника Меппинг.
        
        Ключ — кортеж значений из MAPPING_KEYS,
        значение — счёт ФО.
        
        Args:
            mapping_df: DataFrame справочника Меппинг
            
        Returns:
            Словарь для векторизованного маппинга
        """
        # Валидация наличия всех ключевых столбцов
        missing_cols = [col for col in self.MAPPING_KEYS if col not in mapping_df.columns]
        if missing_cols:
            raise ValueError(f"В справочнике Меппинг отсутствуют столбцы: {missing_cols}")
        
        if 'счет_фо' not in mapping_df.columns:
            raise ValueError("В справочнике Меппинг отсутствует столбец 'счет_фо'")
        
        # Создаём MultiIndex из ключевых столбцов
        mapping_keys = pd.MultiIndex.from_frame(mapping_df[self.MAPPING_KEYS])
        mapping_values = mapping_df['счет_фо'].values
        
        return dict(zip(mapping_keys, mapping_values))
    
    def _map_accounts_to_final(self, osv_all_df: pd.DataFrame,
                               mapping_dict: dict) -> pd.DataFrame:
        """
        ВЕКТОРИЗОВАННЫЙ маппинг счетов ОСВ на счета ФО.
        
        Заменяет медленный apply(axis=1) на MultiIndex.map().
        
        Args:
            osv_all_df: Основной DataFrame ОСВ
            mapping_dict: Словарь маппинга
            
        Returns:
            DataFrame с добавленным столбцом 'счет_фо'
        """
        df = osv_all_df.copy()
        
        
        # Создаём MultiIndex из ключевых столбцов ОСВ
        osv_keys = pd.MultiIndex.from_frame(df[self.MAPPING_KEYS])
        
        # ВЕКТОРИЗОВАННЫЙ маппинг (в 10-50 раз быстрее apply)
        # ★ ВАЖНО: .values в конце — иначе pandas пытается делать reindex
        # и падает с ошибкой "cannot handle a non-unique multi-index"
        df['счет_фо'] = (
            osv_keys.to_series()
            .map(mapping_dict)
            .fillna(self.UNSPECIFIED)
            .astype('string')
            .values
        )
        
        return df
    
    # =========================================================================
    # ВАЛИДАЦИЯ ПОЛНОТЫ МАППИНГА
    # =========================================================================
    
    def _validate_mapping_completeness(self, osv_all_df: pd.DataFrame) -> None:
        """
        Проверяет, что все строки ОСВ замаппились на счёт ФО.
        
        При наличии незамапленных строк выбрасывает MissingMappingError
        с problem_data. Базовый класс Step сам сохранит в Excel.
        """
        unmapped_mask = osv_all_df['счет_фо'] == self.UNSPECIFIED
        
        if not unmapped_mask.any():
            logger.debug('Все позиции соответствуют Меппингу')
            return
        
        # Формируем problem_data — все незамапленные строки ОСВ
        # (не только уникальные комбинации, а все строки для полного анализа)
        problem_data = osv_all_df.loc[
            unmapped_mask, 
            self.MAPPING_KEYS + ['счет_фо', 'сальдо, тыс.ед.']
        ].copy()
        
        # Уникальные комбинации для метаданных
        unmapped_unique = problem_data[self.MAPPING_KEYS].drop_duplicates()
        
        # ★ Выбрасываем MissingMappingError
        # Базовый класс сам сохранит в Excel и залогорирует
        raise MissingMappingError(
            message=(
                f"НЕ ВСЕ позиции соответствуют Меппингу. "
                f"Найдено {len(unmapped_unique)} уникальных незамапленных комбинаций"
            ),
            problem_data=problem_data,
            reference_name="Меппинг",
            unique_combinations_count=len(unmapped_unique),
            total_unmapped_rows=len(problem_data),
        )
    
    # =========================================================================
    # ФОРМИРОВАНИЕ БАЛАНСА
    # =========================================================================
    
    def _build_balance_sheet(self, osv_all_df: pd.DataFrame) -> pd.DataFrame:
        """
        Формирует итоговый баланс на основе плана счетов и сальдо ОСВ.
        
        Args:
            osv_all_df: DataFrame ОСВ с заполненным 'счет_фо'
            
        Returns:
            DataFrame баланса с колонкой 'Значение'
        """
        # 1. Загрузка плана счетов
        # ★ ИСПРАВЛЕНИЕ: используем имена столбцов вместо usecols с индексами
        chart_accounts_df = DataLoader.load_reference_data(
            sheet_name='ПланСчетов',
            strings=['РСБУ Код отчетности', 'Итоговый номер счета']
        )
        
        # 2. Фильтрация только статей баланса
        balance_transcripts = chart_accounts_df[
            chart_accounts_df['Отчетность'] == self.BALANCE_REPORT
        ].copy()
        
        # 3. Валидация структуры
        if 'Итоговый номер счета' not in balance_transcripts.columns:
            raise ValueError("В ПланСчетов отсутствует столбец 'Итоговый номер счета'")
        
        # 4. Устанавливаем индекс
        balance_transcripts = balance_transcripts.set_index('Итоговый номер счета')
        
        # 5. Агрегация сальдо по счёт_фо
        sum_by_account = osv_all_df.groupby('счет_фо')[self.BALANCE_COL].sum()
        
        # 6. Маппинг сальдо в баланс
        balance_transcripts['Значение'] = (
            balance_transcripts.index
            .map(sum_by_account)
            .fillna(0)
        )
        
        # 7. Удаление нулевых строк
        balance_transcripts = balance_transcripts[balance_transcripts['Значение'] != 0]
        
        # 8. Проверка сходимости баланса
        self._validate_balance_convergence(balance_transcripts)
        
        return balance_transcripts
    
    def _validate_balance_convergence(self, balance_df: pd.DataFrame) -> None:
        """
        Проверяет сходимость баланса (актив = пассив).
        
        В балансе сумма всех статей должна быть близка к 0
        (с учётом знака: актив — положительный, пассив — отрицательный,
        или наоборот — зависит от формата).
        """
        balance_sum = balance_df['Значение'].sum()
        abs_balance = abs(int(balance_sum))
        
        if abs_balance > TOLERANCE:
            logger.error(
                f"Расхождение баланса составляет {abs_balance} тыс. ед., "
                f"что превышает допуск {TOLERANCE} тыс. ед."
            )
        else:
            logger.debug(
                f"Баланс сходится: расхождение {abs_balance} тыс. ед. "
                f"(допуск {TOLERANCE} тыс. ед.)"
            )
    
    # =========================================================================
    # ОСНОВНОЙ МЕТОД ОБРАБОТКИ
    # =========================================================================
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """
        Основной метод обработки шага 13.
        
        Выполняет:
        1. Построение маппинга счетов ФО
        2. Векторизованный маппинг ОСВ
        3. Валидацию полноты маппинга
        4. Формирование итогового баланса
        """
        logger.debug("Сборка расшифровки баланса")
        
        osv_all_df = context.main_df.copy()
        mapping_df = context.data.get('mapping')
        
        if mapping_df is None:
            raise ValueError("Справочник Меппинг отсутствует в контексте")
        
        # 1. Построение словаря маппинга
        logger.debug("Этап 1: Построение словаря меппинга")
        mapping_dict = self._build_mapping_dict(mapping_df)
        logger.debug(f"Создан словарь меппинга: {len(mapping_dict)} записей")
        
        # 2. Векторизованный маппинг счетов ОСВ на счета ФО
        logger.debug("Этап 2: Маппинг счетов ОСВ на счета ФО")
        osv_all_df = self._map_accounts_to_final(osv_all_df, mapping_dict)
        logger.debug(f"Замепплено строк: {(osv_all_df['счет_фо'] != self.UNSPECIFIED).sum()}")
        
        # 3. Валидация полноты маппинга
        logger.debug("Этап 3: Проверка полноты меппинга")
        self._validate_mapping_completeness(osv_all_df)
        
        # 4. Формирование итогового баланса
        logger.debug("Этап 4: Формирование баланса")
        balance_sheet = self._build_balance_sheet(osv_all_df)
        
        # 5. Сохранение результатов в контекст
        context.data['final_report'] = balance_sheet
        context.main_df = osv_all_df.loc[:, ~osv_all_df.columns.str.startswith('level_')]
        
        logger.debug(
            f"Расшифровка баланса готова: "
            f"{len(balance_sheet)} статей, "
            f"{len(osv_all_df)} строк в ОСВ"
        )
        
        return context