"""
Шаг 9: Добавление вида связи.

Определяет вид связи с контрагентом (аффилированное лицо, связанная сторона и т.д.)
на основе справочника.
"""
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from io_module import DataLoader


class Step9AddRelatedPartyTypeColumnStep(Step):
    """
    Шаг 9: Добавление вида связи.
    
    Определяет вид связи с контрагентом (аффилированное лицо, связанная сторона и т.д.)
    на основе справочника.
    """
    
    # Константы
    UNSPECIFIED = 'не_указано'
    THIRD_PARTY = '3 лица'
    OTHER_GAP = 'Прочие ГАП'
    
    def __init__(self):
        super().__init__(
            name="Шаг 9: Вид связи КА",
            description="Столбец Вид связи КА на основе соотвествующего признака в ОСВ (для УПП баз) и справочника КА"
        )
    
    def _get_related_party_mapping(self, mapping_df: pd.DataFrame) -> dict:
        """
        Создаёт словарь маппинга (счет, субконто) -> вид_связи из справочника.
        
        Returns:
            Словарь для векторизованного маппинга
        """
        mapping_df_filtered = mapping_df[mapping_df['вид_связи'] != self.UNSPECIFIED]
        
        # Создаём MultiIndex для быстрого поиска
        mapping_index = pd.MultiIndex.from_frame(
            mapping_df_filtered[['счет', 'субконто']]
        )
        
        return dict.fromkeys(mapping_index, self.THIRD_PARTY)
    
    def _normalize_other_gap(self, osv_all_df: pd.DataFrame) -> pd.DataFrame:
        """
        Заменяет варианты 'Прочие ГАП ...' на единое значение 'Прочие ГАП'.
        """
        if 'вид_связи' not in osv_all_df.columns:
            return osv_all_df
        
        mask = osv_all_df['вид_связи'].str.startswith(self.OTHER_GAP, na=False)
        if mask.any():
            osv_all_df.loc[mask, 'вид_связи'] = self.OTHER_GAP
            logger.debug(f"Нормализовано {mask.sum()} значений 'Прочие ГАП'")
        
        return osv_all_df
    
    def _refine_third_party_by_directory(
        self, 
        osv_all_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Уточняет вид связи для '3 лица' на основе справочника ВидСвязиКА.
        
        Если допсубконто есть в справочнике, заменяет '3 лица' на конкретный вид связи
        (например, 'ГАП', 'ГСК' и т.д.).
        """
        if self.THIRD_PARTY not in osv_all_df['вид_связи'].values:
            logger.debug("Нет значений '3 лица' для уточнения")
            return osv_all_df
        
        # Загрузка справочника
        group_companies_df = DataLoader.load_reference_data(
            sheet_name='ВидСвязиКА',
            strings=['ВидСвязиКА', 'сокращенное_наименование_компании']
        )
        
        # Очистка пробелов
        group_companies_df = self.clean_whitespace(group_companies_df)
        
        # Создаём словарь маппинга
        mapping_dict = dict(zip(
            group_companies_df['ВариантыНазвания'],
            group_companies_df['ВидСвязиКА']
        ))
        
        # Маска для замены
        mask = (
            (osv_all_df['вид_связи'] == self.THIRD_PARTY) &
            (osv_all_df['допсубконто'].isin(group_companies_df['ВариантыНазвания']))
        )
        
        if mask.any():
            osv_all_df.loc[mask, 'вид_связи'] = (
                osv_all_df.loc[mask, 'допсубконто'].map(mapping_dict)
            )
            logger.debug(f"Уточнено {mask.sum()} значений '3 лица' по справочнику")
        
        return osv_all_df
    
    def _normalize_contradictory_third_party(self, osv_all_df: pd.DataFrame) -> pd.DataFrame:
        """
        Нормализует противоречивые значения '3 лица' в рамках одного договора.
        """
        if 'вид_связи' not in osv_all_df.columns:
            return osv_all_df
        
        if self.THIRD_PARTY not in osv_all_df['вид_связи'].values:
            return osv_all_df
        
        # Группируем по договору и находим уникальные значения
        grouped = osv_all_df.groupby('договор')['вид_связи'].agg(set)
        
        # Фильтруем договоры, где ровно 2 уникальных значения и одно из них '3 лица'
        contracts_to_fix = grouped[grouped.apply(len) == 2]
        contracts_to_fix = contracts_to_fix[
            contracts_to_fix.apply(lambda x: self.THIRD_PARTY in x)
        ]
        # ★ УБРАЛИ .index — теперь это Series, а не Index
        
        if contracts_to_fix.empty:
            return osv_all_df
        
        # Создаём маппинг: договор -> другое значение (не '3 лица')
        # ★ Теперь contracts_to_fix — это Series, у него есть .items()
        mapping = {
            contract: (values - {self.THIRD_PARTY}).pop()
            for contract, values in contracts_to_fix.items()
        }
        
        # Применяем замену векторизованно
        mask = (
            osv_all_df['договор'].isin(mapping.keys()) &
            (osv_all_df['вид_связи'] == self.THIRD_PARTY)
        )
        
        if mask.any():
            osv_all_df.loc[mask, 'вид_связи'] = osv_all_df.loc[mask, 'договор'].map(mapping)
            logger.debug(f"Нормализовано {mask.sum()} противоречивых значений '3 лица'")
        
        return osv_all_df
    
    def _process(self, context: ProcessingContext) -> ProcessingContext:
        """Основной метод обработки шага 9."""
        logger.debug("Добавление вида связи")
        
        # Делаем копию, чтобы не модифицировать оригинал
        osv_all_df = context.main_df.copy()
        mapping_df = context.data.get('mapping')
        
        # 1. Создаём маппинг из справочника
        mapping_dict = self._get_related_party_mapping(mapping_df)
        
        # 2. ВЕКТОРИЗИРОВАННЫЙ маппинг (вместо apply)
        osv_index = pd.MultiIndex.from_frame(osv_all_df[['счет', 'субконто']])
        temp_col = (
            osv_index.to_series()
            .map(mapping_dict)
            .fillna(self.UNSPECIFIED)
            .values
        )
        
        # 3. Инициализация или обновление столбца 'вид_связи'
        if 'вид_связи' not in osv_all_df.columns:
            osv_all_df['вид_связи'] = temp_col
        else:
            # Нормализуем 'Прочие ГАП ...'
            osv_all_df = self._normalize_other_gap(osv_all_df)
            
            # Заменяем пустые значения на temp_col
            empty_mask = (
                osv_all_df['вид_связи'].isna() |
                (osv_all_df['вид_связи'].astype(str).str.strip() == '')
            )
            # Заменяем "пустые" значения на temp_col
            osv_all_df['вид_связи'] = osv_all_df['вид_связи'].mask(empty_mask, temp_col)
        
        # 4. Уточняем '3 лица' по справочнику ВидСвязиКА
        osv_all_df = self._refine_third_party_by_directory(osv_all_df)
        
        # 5. Удаляем технические столбцы
        osv_all_df = osv_all_df.drop(
            columns=['начало периода  для вида связи', 'конец  периода для вида связи'],
            errors='ignore'
        )
        
        # 6. Нормализуем противоречивые '3 лица'
        osv_all_df = self._normalize_contradictory_third_party(osv_all_df)
        
        # 7. ★ Устанавливаем строковый тип
        osv_all_df['вид_связи'] = osv_all_df['вид_связи'].astype('string')
        
        # Логирование результата
        classified_count = (osv_all_df['вид_связи'] != self.UNSPECIFIED).sum()
        logger.debug(f"Классифицировано видов связи: {classified_count}")
        
        context.main_df = osv_all_df
        return context