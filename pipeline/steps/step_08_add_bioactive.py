"""
Шаг 8: Добавление сегмента биоактивов.
Для счетов 01 и 02 определяет сегмент биоактивов
(животные, растения и т.д.) на основе справочника.
"""
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.errors import ReferenceMismatchError  # ← НОВОЕ
from io_module import DataLoader


class Step8AddBioactiveSegmentColumnStep(Step):
    """
    Шаг 8: Добавление сегмента биоактивов.
    
    Для счетов 01 и 02 определяет сегмент биоактивов
    (животные, растения и т.д.) на основе справочника.
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    DEFAULT_BIOACTIVE = 'Прочие'
    
    def __init__(self):
        super().__init__(
            name="Шаг 8: Сегмент биоактивов 01/02 счетов",
            description="Для счетов 01 и 02 определяет сегмент биоактивов"
        )
    
    def _get_company_bioactive_type(self, name_company: str) -> str:
        """
        Получает тип биоактивов для компании из справочника.
        
        При несоответствии выбрасывает ReferenceMismatchError с problem_data.
        Базовый класс Step сам сохранит проблемные данные в Excel.
        """
        company_directory_df = DataLoader.load_reference_data(
            sheet_name='КомпанииГруппы',
            strings=['биоактивы', 'сокращенное_наименование_компании']
        )
        
        matching_rows = company_directory_df[
            company_directory_df['сокращенное_наименование_компании'] == name_company
        ]
        
        # ★ Компания не найдена в справочнике
        if matching_rows.empty:
            # Формируем problem_data — список всех компаний из справочника
            # чтобы бухгалтер видел, какие компании есть
            problem_data = (
                company_directory_df[['сокращенное_наименование_компании']]
                .drop_duplicates()
                .rename(columns={
                    'сокращенное_наименование_компании': 'компания_в_справочнике'
                })
            )
            
            raise ReferenceMismatchError(
                message=f"Компания '{name_company}' не найдена в справочнике",
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                searched_company=name_company,
            )
        
        # ★ Найдено более одной записи (дубликаты в справочнике)
        if len(matching_rows) > 1:
            # Формируем problem_data — дублирующиеся записи
            problem_data = matching_rows.copy()
            
            raise ReferenceMismatchError(
                message=(
                    f"У компании '{name_company}' найдено {len(matching_rows)} записей. "
                    f"Ожидается одна."
                ),
                problem_data=problem_data,
                reference_name="КомпанииГруппы",
                duplicate_count=len(matching_rows),
            )
        
        bioact_type = matching_rows['биоактивы'].iloc[0]
        
        if pd.isna(bioact_type) or bioact_type == self.UNSPECIFIED:
            bioact_type = self.DEFAULT_BIOACTIVE
        
        logger.debug(f"Тип биоактивов для {name_company}: {bioact_type}")
        
        return bioact_type
    
    def _map_bioactive_segment(
        self,
        osv_all_df: pd.DataFrame,
        mapping_df: pd.DataFrame,
        bioact_type: str
    ) -> pd.DataFrame:
        """
        Векторизованное добавление столбца сегмента биоактивов.
        
        Args:
            osv_all_df: Основной DataFrame
            mapping_df: Справочник меппинга
            bioact_type: Тип биоактивов для компании
            
        Returns:
            DataFrame с добавленным столбцом 'сегмент_биоактивов_для_01_02'
        """
        # Фильтруем mapping для 01 и 02 счетов
        mapping_df_bioactive = mapping_df[
            mapping_df['сегмент_биоактивов_для_01_02'] != self.UNSPECIFIED
        ]
        
        if mapping_df_bioactive.empty:
            logger.warning("В меппинге нет записей с сегментом биоактивов для 01/02")
            osv_all_df['сегмент_биоактивов_для_01_02'] = self.UNSPECIFIED
        else:
            # ВЕКТОРИЗИРОВАННЫЙ МАППИНГ через MultiIndex (вместо apply)
            mapping_index = pd.MultiIndex.from_frame(
                mapping_df_bioactive[['счет', 'субконто']]
            )
            mapping_dict = dict.fromkeys(mapping_index, bioact_type)
            
            osv_index = pd.MultiIndex.from_frame(osv_all_df[['счет', 'субконто']])
            
            osv_all_df['сегмент_биоактивов_для_01_02'] = (
                osv_index.to_series()
                .map(mapping_dict)
                .fillna(self.UNSPECIFIED)
                .values
            )
        
        # Устанавливаем строковый тип
        osv_all_df['сегмент_биоактивов_для_01_02'] = osv_all_df['сегмент_биоактивов_для_01_02'].astype('string')
        
        # Логирование результата
        classified_count = (osv_all_df['сегмент_биоактивов_для_01_02'] != self.UNSPECIFIED).sum()
        logger.debug(f"Классифицировано строк с биоактивами: {classified_count}")
        
        return osv_all_df
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 8."""
        logger.debug("Добавление сегмента биоактивов")
        
        osv_all_df = context.main_df.copy()
        name_company = context.get_metadata('company_name')
        mapping_df = context.data.get('mapping')
        
        # 1. Получаем тип биоактивов для компании
        bioact_type = self._get_company_bioactive_type(name_company)
        
        # 2. Добавляем столбец сегмента биоактивов
        osv_all_df = self._map_bioactive_segment(osv_all_df, mapping_df, bioact_type)
        
        context.main_df = osv_all_df
        return context

