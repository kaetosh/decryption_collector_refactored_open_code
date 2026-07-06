"""
Шаг 11a: Проверка похожих названий контрагентов.

Выявляет контрагентов, помеченных как '3 лица', чьи названия
похожи на названия своих компаний из справочника ВидСвязиКА.

Это позволяет бухгалтеру выявить случаи, когда одна и та же компания
записана по-разному (например, 'ООО Рыба Мясо' vs 'Рыба мясо ООО')
и не была распознана как аффилированная.

Результат: Excel-файл с предупреждениями (не прерывает работу).
"""
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from io_module import DataLoader, DataSaver
from utils.text_utils import find_similar_companies
from config.settings import (
    CONTRACTOR_ACCOUNTS_PREFIXES,
    CONTRACTOR_SIMILARITY_THRESHOLD,
    CONTRACTOR_SIMILARITY_LIMIT,
)


class Step11aCheckContractorSimilarityStep(Step):
    """
    Проверка нечёткого совпадения названий контрагентов.
    
    Ищет потенциальные ошибки классификации '3 лица',
    когда название контрагента очень похоже на название своей компании.
    """

    def __init__(self):
        super().__init__(
            name="Шаг 11a: Проверка похожих контрагентов",
            description="Fuzzy-matching контрагентов со своими компаниями"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Проверка похожих названий контрагентов")
        
        osv_all_df = context.main_df
        
        # 1. Загружаем справочник своих компаний (с кэшированием)
        group_companies_df = self._get_group_companies_df(context)
        
        if group_companies_df.empty:
            logger.debug("Справочник ВидСвязиКА пуст, проверка пропущена")
            return context
        
        # 2. Фильтруем контрагентов-третьих лиц из ОСВ и ведомости амортизации
        third_party_contractors = self._get_third_party_contractors(osv_all_df, context)
        
        if third_party_contractors.empty:
            logger.debug("Нет '3 лиц' на счетах контрагентов для проверки")
            return context
        
        # 3. Ищем похожие названия через универсальную функцию
        similarity_df = find_similar_companies(
            data_a=third_party_contractors['допсубконто'],
            data_b=group_companies_df['ВариантыНазвания'],
            similarity_threshold=CONTRACTOR_SIMILARITY_THRESHOLD,
            limit=CONTRACTOR_SIMILARITY_LIMIT,
        )
        
        if similarity_df.empty:
            logger.info(
                f"✓ Похожих названий не найдено "
                f"(проверено {len(third_party_contractors)} контрагентов)"
            )
            return context
        
        # 4. Обогащаем результат дополнительной информацией
        similarity_df = self._enrich_similarity_report(
            similarity_df,
            third_party_contractors,
            group_companies_df
        )
        
        # 5. Сохраняем отчёт с предупреждениями
        self._save_similarity_report(similarity_df, context)
        
        return context

    # =========================================================================
    # ИЗВЛЕЧЕНИЕ ДАННЫХ
    # =========================================================================

    def _get_group_companies_df(self, context: ProcessingContext) -> pd.DataFrame:
        """
        Загружает справочник ВидСвязиКА с кэшированием в контексте.
        
        Кэширование позволяет избежать повторной загрузки справочника
        (используется также в Step 9 и Step 10).
        """
        cache_key = 'ВидСвязиКА_df'
        
        if cache_key in context.data:
            logger.debug("Используем кэшированный справочник ВидСвязиКА")
            return context.data[cache_key]
        
        # Загрузка справочника
        group_companies_df = DataLoader.load_reference_data(
            sheet_name='ВидСвязиКА',
            strings=['ВидСвязиКА', 'сокращенное_наименование_компании']
        )
        
        # Кэшируем в контексте
        context.data[cache_key] = group_companies_df
        logger.debug(f"Загружен и кэширован справочник ВидСвязиКА: {len(group_companies_df)} записей")
        
        return group_companies_df

    def _get_third_party_contractors(self, osv_all_df: pd.DataFrame, context: ProcessingContext) -> pd.DataFrame:
        """
        Извлекает уникальных контрагентов-третьих лиц из двух источников:
        1. Основная ОСВ (osv_all_df) — счета с контрагентами
        2. Ведомость амортизации (из контекста) — арендованные ОС
        
        Returns:
            DataFrame с колонками: допсубконто, количество_строк, суммарное_сальдо, примеры_счетов
        """
        # =========================================================================
        # ИСТОЧНИК 1: Основная ОСВ
        # =========================================================================
        
        # Фильтр по счетам
        mask_accounts = osv_all_df['счет'].str.startswith(
            CONTRACTOR_ACCOUNTS_PREFIXES, na=False
        )
        
        # Фильтр по '3 лица'
        mask_third_party = osv_all_df['вид_связи'] == '3 лица'
        
        # Фильтр по непустым контрагентам
        mask_not_empty = (
            osv_all_df['допсубконто'].notna() &
            (osv_all_df['допсубконто'].astype(str).str.strip() != '') &
            (osv_all_df['допсубконто'] != self.UNSPECIFIED)
        )
        
        filtered_osv = osv_all_df[mask_accounts & mask_third_party & mask_not_empty]
        
        osv_grouped = pd.DataFrame()
        if not filtered_osv.empty:
            osv_grouped = (
                filtered_osv
                .groupby('допсубконто', observed=True)
                .agg(
                    количество_строк=('счет', 'size'),
                    суммарное_сальдо=('сальдо, тыс.ед.', 'sum'),
                    примеры_счетов=('счет', lambda x: ', '.join(sorted(x.unique())[:5]))
                )
                .reset_index()
            )
        
        # =========================================================================
        # ИСТОЧНИК 2: Ведомость амортизации
        # =========================================================================
        
        depreciation_grouped = self._get_third_party_from_depreciation(context)
        
        # =========================================================================
        # ОБЪЕДИНЕНИЕ
        # =========================================================================
        
        if osv_grouped.empty and depreciation_grouped.empty:
            return pd.DataFrame()
        
        if osv_grouped.empty:
            return depreciation_grouped
        
        if depreciation_grouped.empty:
            return osv_grouped.sort_values('суммарное_сальдо', key=abs, ascending=False)
        
        # Объединяем оба источника
        # Для дублирующихся контрагентов суммируем значения
        combined = pd.concat([osv_grouped, depreciation_grouped], ignore_index=True)
        
        # Агрегируем по контрагенту
        result = (
            combined
            .groupby('допсубконто', observed=True)
            .agg({
                'количество_строк': 'sum',
                'суммарное_сальдо': 'sum',
                'примеры_счетов': lambda x: ', '.join(sorted(set(x))[:5])
            })
            .reset_index()
            .sort_values('суммарное_сальдо', key=abs, ascending=False)
        )
        
        logger.debug(
            f"Объединено контрагентов: {len(osv_grouped)} из ОСВ + "
            f"{len(depreciation_grouped)} из ведомости = {len(result)} уникальных"
        )
        
        return result
        
    def _get_third_party_from_depreciation(self, context: ProcessingContext) -> pd.DataFrame:
        """
        Извлекает контрагентов-третьих лиц из ведомости амортизации (шаг 10).
        
        Устойчив к отсутствию столбца 'Контрагент' — в этом случае
        использует 'допсубконто' или пропускает источник.
        
        Returns:
            DataFrame с колонками: допсубконто, количество_строк, суммарное_сальдо, примеры_счетов
        """
        depreciation_df = context.data.get('depreciation_statement_df')
        
        if depreciation_df is None or depreciation_df.empty:
            logger.debug("Ведомость амортизации отсутствует в контексте")
            return pd.DataFrame()
        
        # Определяем столбец с контрагентом
        contractor_col = None
        if 'Контрагент' in depreciation_df.columns:
            contractor_col = 'Контрагент'
        elif 'допсубконто' in depreciation_df.columns:
            contractor_col = 'допсубконто'
            logger.debug(
                "Столбец 'Контрагент' отсутствует в ведомости, "
                "используем 'допсубконто' как fallback"
            )
        else:
            logger.warning(
                "В ведомости амортизации отсутствуют столбцы 'Контрагент' и 'допсубконто'. "
                "Пропускаем этот источник."
            )
            return pd.DataFrame()
        
        # Проверяем наличие столбца 'вид_связи'
        if 'вид_связи' not in depreciation_df.columns:
            logger.debug("В ведомости амортизации отсутствует столбец 'вид_связи'")
            return pd.DataFrame()
        
        # Фильтр по '3 лица'
        mask_third_party = depreciation_df['вид_связи'] == '3 лица'
        
        # Фильтр по непустым контрагентам
        mask_not_empty = (
            depreciation_df[contractor_col].notna() &
            (depreciation_df[contractor_col].astype(str).str.strip() != '') &
            (depreciation_df[contractor_col] != self.UNSPECIFIED)
        )
        
        filtered = depreciation_df[mask_third_party & mask_not_empty].copy()
        
        if filtered.empty:
            logger.debug("Нет '3 лиц' в ведомости амортизации")
            return pd.DataFrame()
        
        # ★ ИСПРАВЛЕНИЕ: переименовываем СРАЗУ, чтобы дальше работать единообразно
        if contractor_col != 'допсубконто':
            filtered = filtered.rename(columns={contractor_col: 'допсубконто'})
        
        # Определяем столбец с суммой
        saldo_col = None
        if 'сальдо, тыс.ед.' in filtered.columns:
            saldo_col = 'сальдо, тыс.ед.'
        elif 'Стоимость на конец периода' in filtered.columns:
            saldo_col = 'Стоимость на конец периода'
        
        # ★ Формируем словарь агрегации с УЖЕ ПРАВИЛЬНЫМ именем столбца
        agg_dict = {
            'количество_строк': ('допсубконто', 'size'),
        }
        
        if saldo_col:
            agg_dict['суммарное_сальдо'] = (saldo_col, 'sum')
        
        # ★ Агрегируем по 'допсубконто' (столбец уже переименован)
        grouped = (
            filtered
            .groupby('допсубконто', observed=True)
            .agg(**agg_dict)
            .reset_index()
        )
        
        # Добавляем примеры_счетов
        grouped['примеры_счетов'] = 'Ведомость амортизации (01.03, 02.03)'
        
        # Если нет столбца сальдо, добавляем с нулями
        if 'суммарное_сальдо' not in grouped.columns:
            grouped['суммарное_сальдо'] = 0.0
        
        grouped = grouped.sort_values('суммарное_сальдо', key=abs, ascending=False)
        
        logger.debug(
            f"Извлечено {len(grouped)} контрагентов из ведомости амортизации "
            f"(использован столбец '{contractor_col}')"
        )
        
        return grouped
    
    def _enrich_similarity_report(
        self,
        similarity_df: pd.DataFrame,
        third_party_contractors: pd.DataFrame,
        group_companies_df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        Обогащает отчёт о похожих названиях дополнительной информацией:
        - количество_строк, суммарное_сальдо, примеры_счетов для контрагента
        - вид_связи_в_справочнике для похожей компании
        """
        # Создаём словари для быстрого поиска
        contractor_info = third_party_contractors.set_index('допсубконто').to_dict('index')
        relation_mapping = dict(zip(
            group_companies_df['ВариантыНазвания'],
            group_companies_df['ВидСвязиКА']
        ))
        
        # Обогащаем каждую строку
        enriched_rows = []
        
        for _, row in similarity_df.iterrows():
            contractor = row['original_a']
            matched_company = row['original_b']
            
            # Получаем информацию о контрагенте
            info = contractor_info.get(contractor, {})
            
            enriched_rows.append({
                'контрагент_в_осв': contractor,
                'контрагент_очищенный': row['cleaned_a'],
                'похожая_своя_компания': matched_company,
                'своя_очищенная': row['cleaned_b'],
                'процент_совпадения': row['score'],
                'вид_связи_в_справочнике': relation_mapping.get(matched_company, 'не_указано'),
                'количество_строк': info.get('количество_строк', 0),
                'суммарное_сальдо': info.get('суммарное_сальдо', 0.0),
                'примеры_счетов': info.get('примеры_счетов', ''),
            })
        
        df = pd.DataFrame(enriched_rows)
        
        # Сортируем: сначала по проценту совпадения (desc), потом по сальдо (desc по модулю)
        df = df.sort_values(
            ['процент_совпадения', 'суммарное_сальдо'],
            ascending=[False, False],
            key=lambda x: abs(x) if x.name == 'суммарное_сальдо' else x
        ).reset_index(drop=True)
        
        return df

    # =========================================================================
    # СОХРАНЕНИЕ ОТЧЁТА
    # =========================================================================

    def _save_similarity_report(
        self,
        similarity_df: pd.DataFrame,
        context: ProcessingContext
    ) -> None:
        """
        Сохраняет отчёт о похожих названиях в Excel.
        
        Это предупреждение — не прерывает работу пайплайна.
        """
        name_company = context.get_metadata('company_name', 'unknown')
        period = context.get_metadata('period', 'unknown')
        
        filename = f'WARNING_similar_contractors_{name_company}_{period}.xlsx'
        
        try:
            output_path = DataSaver.save_to_excel(
                df=similarity_df,
                filename=filename,
                subfolder='warnings'  # Отдельная папка для предупреждений
            )
            
            logger.warning(
                f"⚠️ Найдено {len(similarity_df)} потенциальных совпадений "
                f"контрагентов со своими компаниями!\n"
                f"📁 Подробности в: {output_path.parent.name}/{output_path.name}\n"
                f"🔍 Проверьте, не являются ли '3 лица' на самом деле аффилированными."
            )
            
        except PermissionError as e:
            logger.error(
                f"Не удалось сохранить отчёт о похожих контрагентах: {e}. "
                f"Закройте файл и повторите."
            )
        except Exception as e:
            logger.error(f"Ошибка при сохранении отчёта: {e}")