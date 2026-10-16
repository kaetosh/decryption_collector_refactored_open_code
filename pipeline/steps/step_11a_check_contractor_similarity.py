"""
Шаг 11a: Проверка похожих названий контрагентов.

Выявляет контрагентов, помеченных как '3 лица', чьи названия
похожи на названия своих компаний из справочника ВидСвязиКА.

Это позволяет бухгалтеру выявить случаи, когда одна и та же компания
записана по-разному (например, 'ООО Рыба Мясо' vs 'Рыба мясо ООО')
и не была распознана как аффилированная.

Ложные срабатывания гасит справочник исключений (лист «ИсключенияПохожих»
в Справочники.xlsx, см. SimilarityExclusionConstants): он убирает из отчёта
пары «контрагент — своя компания», которые заведомо ложны, и НИЧЕГО НЕ
МЕНЯЕТ в цифрах отчёта.

Результат: Excel-файл с предупреждениями (не прерывает работу).
"""
import pandas as pd
from loguru import logger

from pipeline.base import Step, ProcessingContext
from pipeline.step_config import SimilarityExclusionConstants
from io_module import DataLoader, DataSaver
from io_module.output_manager import get_output_dir
from utils.text_utils import clean_company_name, find_similar_companies
from config.settings import (
    CONTRACTOR_ACCOUNTS_PREFIXES,
    CONTRACTOR_SIMILARITY_THRESHOLD,
    CONTRACTOR_SIMILARITY_LIMIT,
    to_relative,
)

# Ключ справочника исключений в context.references (pipeline/executors.py:
# REFERENCE_REGISTRY). Объявлен здесь явно, чтобы шаг не зависел от порядка
# импортов в реестре.
EXCLUSIONS_REF_KEY = 'исключения_похожих'


class Step11aCheckContractorSimilarityStep(Step):
    """
    Проверка нечёткого совпадения названий контрагентов.

    Ищет потенциальные ошибки классификации '3 лица',
    когда название контрагента очень похоже на название своей компании.
    """

    # Константы
    UNSPECIFIED = 'не_указано'

    # Колонки отчёта о похожих контрагентах. Объявлены здесь, потому что
    # лист «Исключено» получает те же колонки плюс основание исключения:
    # пустой результат должен сохраняться с той же схемой, иначе файл
    # получается без заголовков.
    REPORT_COLUMNS = (
        'контрагент_в_осв',
        'контрагент_очищенный',
        'похожая_своя_компания',
        'своя_очищенная',
        'процент_совпадения',
        'вид_связи_в_справочнике',
        'количество_строк',
        'суммарное_сальдо',
        'примеры_счетов',
    )

    # Колонки результата fuzzy-поиска, которые _enrich_similarity_report
    # переносит сама. Всё остальное (служебное — например, правило
    # исключения, погасившее пару) проносится автоматически.
    _BASE_COLUMNS = (
        'original_a',
        'original_b',
        'cleaned_a',
        'cleaned_b',
        'score',
    )

    def __init__(self):
        super().__init__(
            name="Шаг 11a: Проверка похожих контрагентов",
            description="Fuzzy-matching контрагентов со своими компаниями"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        logger.debug("Проверка похожих названий контрагентов")

        osv_all_df = context.summary_osv_df.copy()

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
                "[OK] Похожих контрагентов не обнаружено (проверено {} позиций)",
                len(third_party_contractors),
            )
            return context

        # 4. Исключения из листа «ИсключенияПохожих». Листа может не быть —
        #    тогда шаг работает ровно как раньше (фича выключена).
        exclusions, skipped_rules = self._prepare_exclusions(context)
        excluded_df = self._empty_report_frame()
        # Условие — «есть что проверять», а не «есть применимые правила»:
        # строка без контрагента тоже обязана быть сказана вслух, иначе
        # бухгалтер правил файл и не понимал, почему исключения не работают.
        if exclusions or skipped_rules:
            similarity_df, excluded_raw = self._apply_exclusions(
                similarity_df, exclusions, context
            )
            self._warn_unmatched_exclusions(
                exclusions, excluded_raw, skipped_rules, context
            )
            if not excluded_raw.empty:
                excluded_df = self._enrich_similarity_report(
                    excluded_raw,
                    third_party_contractors,
                    group_companies_df,
                )
                excluded_df['основание'] = [
                    self._exclusion_reason(rule) for rule in excluded_df['_rule']
                ]
                excluded_df = excluded_df.drop(columns=['_rule'])

        if similarity_df.empty and excluded_df.empty:
            logger.info(
                "[OK] Похожих контрагентов не обнаружено (проверено {} позиций)",
                len(third_party_contractors),
            )
            return context

        # 5. Обогащаем результат дополнительной информацией
        similarity_df = self._enrich_similarity_report(
            similarity_df,
            third_party_contractors,
            group_companies_df
        )

        # 6. Сохраняем отчёт с предупреждениями. Если остались только
        #    исключённые пары, WARNING не выдаётся (в этом весь смысл
        #    справочника), но файл пишется — иначе применённое исключение
        #    было бы видно только в логе.
        self._save_similarity_report(similarity_df, excluded_df, context)

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
        group_companies_df = context.references['вид_связи_ка']

        # Кэшируем в контексте
        context.data[cache_key] = group_companies_df
        logger.debug(
            "Загружен и кэширован справочник ВидСвязиКА: {} записей",
            len(group_companies_df),
        )

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
            "Объединено контрагентов: {} из ОСВ + {} из ведомости = {} уникальных",
            len(osv_grouped),
            len(depreciation_grouped),
            len(result),
        )

        return result

    def _get_third_party_from_depreciation(self, context: ProcessingContext) -> pd.DataFrame:
        """
        Извлекает контрагентов-третьих лиц из ведомости амортизации (шаг 10).

        Устойчив к отсутствию столбца 'Контрагент' — в этом случае
        используется 'допсубконто' или пропускает источник.

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
            "Извлечено {} контрагентов из ведомости амортизации "
            "(использован столбец '{}')",
            len(grouped),
            contractor_col,
        )

        return grouped

    # =========================================================================
    # ИСКЛЮЧЕНИЯ ЛОЖНЫХ СРАБАТЫВАНИЙ (лист «ИсключенияПохожих»)
    # =========================================================================

    @staticmethod
    def _cell_text(value) -> str:
        """Значение ячейки справочника как обрезанный текст ('' — пусто).

        Справочник читается с dtype='string', поэтому пропуск — это pd.NA,
        а не None: str(pd.NA) даёт '<NA>', и такое значение молча стало бы
        контрагентом в нормализованном имени.
        """
        if value is None or pd.isna(value):
            return ''
        return str(value).strip()

    def _empty_report_frame(self, extra_columns=()) -> pd.DataFrame:
        """Пустой отчёт с той же схемой, что и непустой."""
        return pd.DataFrame(columns=list(self.REPORT_COLUMNS) + list(extra_columns))

    @staticmethod
    def _empty_frame_like(df: pd.DataFrame) -> pd.DataFrame:
        """Пустой кадр с той же схемой — чтобы не плодить проверки на None."""
        return df.head(0).copy()

    def _known_companies(self, context: ProcessingContext) -> set:
        """Написания компаний группы — чем сверяется колонка «компания».

        Колонка нужна только для одной вещи: поймать опечатку в названии
        компании. Правило с чужим названием молча не применилось бы, и
        бухгалтер искал бы причину в нормализации, а не в своём файле.
        """
        names = {self._cell_text(context.company).lower()}

        companies_df = (context.references or {}).get('компании_группы')
        if companies_df is not None and not companies_df.empty:
            from pipeline.constants import ColumnNames
            col = ColumnNames.SHORT_COMPANY_NAME
            if col in companies_df.columns:
                names |= {
                    self._cell_text(value).lower()
                    for value in companies_df[col]
                    if self._cell_text(value)
                }

        return names

    def _prepare_exclusions(self, context: ProcessingContext):
        """Читает лист «ИсключенияПохожих» и превращает строки в правила.

        Returns:
            (правила, непригодные_строки). Правила — список словарей с уже
            нормализованными именами; пустой список означает «работаем без
            исключений», и это штатный случай, а не сбой.
        """
        df = (context.references or {}).get(EXCLUSIONS_REF_KEY)
        if df is None or df.empty:
            logger.debug(
                "Справочник исключений '{}' пуст или отсутствует — работаем без исключений",
                SimilarityExclusionConstants.SHEET_NAME,
            )
            return [], []

        if SimilarityExclusionConstants.COL_CONTRACTOR not in df.columns:
            logger.warning(
                "[!] В листе справочника '{}' нет колонки '{}' — исключения не применяются",
                SimilarityExclusionConstants.SHEET_NAME,
                SimilarityExclusionConstants.COL_CONTRACTOR,
            )
            return [], []

        known_companies = self._known_companies(context)
        rules: list = []
        skipped: list = []

        for position, (_, row) in enumerate(df.iterrows(), start=1):
            contractor = self._cell_text(row.get(SimilarityExclusionConstants.COL_CONTRACTOR))
            if not contractor or contractor == self.UNSPECIFIED:
                skipped.append({
                    "строка": position,
                    "причина": "не заполнена колонка «контрагент»",
                })
                continue

            company = self._cell_text(row.get(SimilarityExclusionConstants.COL_COMPANY))
            if company and company.lower() not in known_companies:
                skipped.append({
                    "строка": position,
                    "причина": (
                        f"неизвестная компания '{company}' — "
                        "правило не применится, проверьте написание"
                    ),
                })
                continue

            like_company = self._cell_text(row.get(SimilarityExclusionConstants.COL_LIKE_COMPANY))
            if like_company == self.UNSPECIFIED:
                like_company = ''

            rules.append({
                "строка": position,
                "контрагент": contractor,
                "похожая_компания": like_company,
                "компания": company,
                "комментарий": self._cell_text(row.get(SimilarityExclusionConstants.COL_COMMENT)),
                # Нормализуем тем же хелпером, что и сам fuzzy-поиск: иначе
                # «РЕСУРС ПО ООО» (нормализованное «ресурс») не совпало бы с
                # тем же контрагентом в отчёте.
                "norm_контрагент": clean_company_name(contractor),
                "norm_похожая": clean_company_name(like_company) if like_company else '',
                "norm_компания": company.lower() if company else '',
            })

        logger.debug(
            "Справочник исключений '{}': {} правил, {} пропущено",
            SimilarityExclusionConstants.SHEET_NAME,
            len(rules),
            len(skipped),
        )
        return rules, skipped

    def _apply_exclusions(self, similarity_df: pd.DataFrame, rules: list, context: ProcessingContext):
        """Убирает из находок пары, покрытые справочником исключений.

        Правило срабатывает, если нормализованное имя контрагента совпало,
        а непустые селекторы «похожая_компания» и «компания» — тоже.
        Пустая ячейка означает «не важно», как в файле ручных правок.

        Returns:
            (оставшиеся_находки, исключённые_находки).
        """
        if not rules:
            return similarity_df, self._empty_frame_like(similarity_df)

        current_company = self._cell_text(context.company).lower()
        hits = [self._match_rule(row, rules, current_company) for _, row in similarity_df.iterrows()]

        matched_mask = pd.Series(
            [hit is not None for hit in hits], index=similarity_df.index
        )
        matched_rules = pd.Series(hits, index=similarity_df.index)

        excluded = similarity_df[matched_mask].copy()
        excluded['_rule'] = matched_rules[matched_mask].tolist()
        remaining = similarity_df[~matched_mask].copy()

        return remaining, excluded

    @staticmethod
    def _match_rule(row: pd.Series, rules: list, current_company: str):
        """Первое правило, покрывающее пару «контрагент — своя компания»."""
        for rule in rules:
            if row['cleaned_a'] != rule["norm_контрагент"]:
                continue
            if rule["norm_похожая"] and row['cleaned_b'] != rule["norm_похожая"]:
                continue
            if rule["norm_компания"] and rule["norm_компания"] != current_company:
                continue
            return rule
        return None

    @staticmethod
    def _exclusion_reason(rule: dict) -> str:
        """Человекочитаемое основание исключения для листа «Исключено»."""
        parts = [f"контрагент='{rule['контрагент']}'"]
        parts.append(
            f"похожая_компания='{rule['похожая_компания']}'"
            if rule['похожая_компания'] else "похожая_компания=любая"
        )
        if rule['компания']:
            parts.append(f"компания='{rule['компания']}'")
        if rule['комментарий']:
            parts.append(f"комментарий: {rule['комментарий']}")
        return "; ".join(parts)

    def _warn_unmatched_exclusions(
        self,
        rules: list,
        excluded_df: pd.DataFrame,
        skipped: list,
        context: ProcessingContext,
    ) -> None:
        """Предупреждает о правилах, которые ничего не погасили.

        Без этого опечатка в названии контрагента выглядит как «программа не
        нашла похожих», а исключение молча не работает. Правило, которое не
        нашло совпадений, — это вопрос к справочнику, а не поломка.
        """
        used_rows = set()
        if not excluded_df.empty:
            used_rows = set(excluded_df['_rule'].map(lambda rule: rule['строка']))

        unmatched = [rule for rule in rules if rule['строка'] not in used_rows]
        for rule in unmatched:
            # Два WARNING, а не один: и консольный формат логгера режет строку
            # до 500 символов, и сводка предупреждений (а с ней титульный
            # лист) — до 200. Длинный монотекст обрезался бы ровно на
            # названии контрагента, ради которого бухгалтер и открывает лог.
            logger.warning(
                "[!] Исключение не сработало (строка {} листа '{}'): '{}'.",
                rule['строка'],
                SimilarityExclusionConstants.SHEET_NAME,
                rule['контрагент'],
            )
            logger.warning(
                "[!] Проверьте написание контрагента '{}' — такого не было среди "
                "похожих находок этого прогона (возможно, в ОСВ он записан иначе).",
                rule['контрагент'],
            )

        for item in skipped:
            logger.warning(
                "[!] Исключение пропущено (строка {} листа '{}'): {}.",
                item['строка'],
                SimilarityExclusionConstants.SHEET_NAME,
                item['причина'],
            )

        if not rules and not skipped:
            return

        self._update_summary(context, rules, excluded_df, len(unmatched) + len(skipped))

    def _update_summary(
        self,
        context: ProcessingContext,
        rules: list,
        excluded_df: pd.DataFrame,
        unmatched: int,
    ) -> None:
        """Сводка исключений в context.data — её читает титульный лист."""
        SE = SimilarityExclusionConstants
        context.data[SE.SUMMARY_KEY] = {
            SE.SUMMARY_RULES_LOADED: len(rules) + unmatched,
            SE.SUMMARY_RULES_APPLIED: len(rules) - unmatched,
            SE.SUMMARY_RULES_UNMATCHED: unmatched,
            SE.SUMMARY_PAIRS_EXCLUDED: len(excluded_df),
            SE.SUMMARY_CONTRACTORS_EXCLUDED: int(
                excluded_df['original_a'].nunique() if not excluded_df.empty else 0
            ),
            SE.SUMMARY_SHEET: SE.SHEET_NAME,
        }

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

        Служебные колонки исходного кадра (например, `_rule` — правило
        исключения, погасившее пару) пронесутся в результат: без них лист
        «Исключено» не смог бы объяснить, по какому правилу убрана пара.
        """
        if similarity_df.empty:
            extra = [col for col in similarity_df.columns if col != '_rule']
            return self._empty_report_frame(extra)

        # Создаём словари для быстрого поиска
        contractor_info = third_party_contractors.set_index('допсубконто').to_dict('index')
        relation_mapping = dict(zip(
            group_companies_df['ВариантыНазвания'],
            group_companies_df['ВидСвязиКА']
        ))

        service_columns = [
            col for col in similarity_df.columns if col not in self._BASE_COLUMNS
        ]

        # Обогащаем каждую строку
        enriched_rows = []

        for _, row in similarity_df.iterrows():
            contractor = row['original_a']
            matched_company = row['original_b']

            # Получаем информацию о контрагенте
            info = contractor_info.get(contractor, {})

            record = {
                'контрагент_в_осв': contractor,
                'контрагент_очищенный': row['cleaned_a'],
                'похожая_своя_компания': matched_company,
                'своя_очищенная': row['cleaned_b'],
                'процент_совпадения': row['score'],
                'вид_связи_в_справочнике': relation_mapping.get(matched_company, 'не_указано'),
                'количество_строк': info.get('количество_строк', 0),
                'суммарное_сальдо': info.get('суммарное_сальдо', 0.0),
                'примеры_счетов': info.get('примеры_счетов', ''),
            }
            record.update({col: row[col] for col in service_columns})
            enriched_rows.append(record)

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

    def _to_sheet(self, frame: pd.DataFrame, writer, sheet_name: str) -> None:
        """Пишет лист с общим форматированием (шапка, автофильтр, ширины)."""
        if frame.empty:
            # Пустой лист сохраняем с той же схемой: файл без заголовков
            # нечитаем, а пустой лист «Исключено» читается сразу.
            frame = self._empty_report_frame()
        frame.to_excel(writer, sheet_name=sheet_name, index=False)
        DataSaver._apply_excel_formatting(writer.sheets[sheet_name])

    def _save_similarity_report(
        self,
        similarity_df: pd.DataFrame,
        excluded_df: pd.DataFrame,
        context: ProcessingContext
    ) -> None:
        """
        Сохраняет отчёт о похожих названиях в Excel.

        Это предупреждение — не прерывает работу пайплайна.

        Когда сработали исключения, добавляется лист «Исключено по
        справочнику»: иначе применённое исключение было бы видно только в
        логе, а по правилам проекта ничего не применяется молча. Если после
        исключений не осталось ничего, WARNING не выдаётся — в этом и есть
        смысл справочника — но файл всё равно пишется, чтобы исключение
        осталось видимым.
        """
        SE = SimilarityExclusionConstants
        name_company = context.company
        period = context.period

        filename = f'WARNING_similar_contractors_{name_company}_{period}.xlsx'
        has_excluded = excluded_df is not None and not excluded_df.empty

        try:
            output_path = get_output_dir('warnings') / filename

            with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
                self._to_sheet(similarity_df, writer, SE.SHEET_REPORT)
                if has_excluded:
                    self._to_sheet(excluded_df, writer, SE.SHEET_EXCLUDED)

            if similarity_df.empty:
                logger.info(
                    "[OK] Все найденные похожие контрагенты исключены справочником "
                    "'{}' (исключено {} совпадений). Подробности см. в {}",
                    SE.SHEET_NAME,
                    len(excluded_df),
                    to_relative(output_path),
                )
            else:
                logger.warning(
                    "Найдено {} потенциальных совпадений контрагентов со своими "
                    "компаниями. Подробности см. в {}",
                    len(similarity_df),
                    to_relative(output_path),
                )

        except PermissionError as e:
            logger.error(
                "Не удалось сохранить отчёт о похожих контрагентах: {}. "
                "Закройте файл и повторите.",
                e,
            )
        except Exception as e:
            logger.error("Ошибка при сохранении отчёта: {}", e)