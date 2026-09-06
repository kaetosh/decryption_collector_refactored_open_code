"""
Mixin с преобразованием формата для Шага 14.

Методы:
    _reshape_to_long_format: melt в длинный формат (90.01/90.02)
    _enrich_with_mappings: обогащение данными из справочников (УФР, ВидСвязиКА)
    _validate_mapping_completeness: проверка полноты маппинга
    _validate_group_ka_values: проверка допустимых значений группа_ка
    _calculate_connection_type: расчёт вид_связи
    _merge_with_reassessment: объединение основного результата с переоценкой
"""
import numpy as np
import pandas as pd
from loguru import logger

from pipeline.errors import MissingMappingError


class Step14TransformMixin:
    """Миксин преобразования формата для Шага 14."""

    def _reshape_to_long_format(self, df: pd.DataFrame) -> pd.DataFrame:
        """Преобразует DataFrame в длинный формат (melt)."""
        logger.debug("Преобразование в длинный формат")

        df['выручка_без_ндс_тыс_ед'] = -df['выручка_без_ндс_тыс_ед']
        df['выручка_без_ндс_тыс_руб'] = -df['выручка_без_ндс_тыс_руб']

        df_long = df.melt(
            id_vars=['контрагент', 'ном_группа'],
            value_vars=['выручка_без_ндс_тыс_ед', 'себестоимость_тыс_ед'],
            var_name='счет',
            value_name='оборот, тыс.ед.'
        )

        account_mapping = {
            'выручка_без_ндс_тыс_ед': '90.01',
            'себестоимость_тыс_ед': '90.02'
        }

        df_long['счет'] = df_long['счет'].map(account_mapping).astype('string')

        account_mapping_rub = {
            'выручка_без_ндс_тыс_руб': '90.01',
            'себестоимость_тыс_руб': '90.02'
        }
        df_long_rub = df.melt(
            id_vars=['контрагент', 'ном_группа'],
            value_vars=['выручка_без_ндс_тыс_руб', 'себестоимость_тыс_руб'],
            var_name='счет',
            value_name='оборот, тыс.руб.'
        )
        df_long_rub['счет'] = df_long_rub['счет'].map(account_mapping_rub).astype('string')
        df_long['оборот, тыс.руб.'] = df_long_rub['оборот, тыс.руб.'].to_numpy()

        df_long = df_long[df_long['оборот, тыс.ед.'] != 0].reset_index(drop=True)

        logger.debug("После melt: {} строк", len(df_long))

        return df_long

    def _enrich_with_mappings(
        self,
        df_result: pd.DataFrame,
        mapping_opu_df: pd.DataFrame,
        directory_ufr_df: pd.DataFrame,
        group_companies_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Обогащает DataFrame данными из справочников."""
        logger.debug("Обогащение данными из справочников")

        mapping_account = (
            mapping_opu_df
            .drop_duplicates(subset='счет')
            .set_index('счет')['доход_расход']
        )
        df_result['доход_расход'] = df_result['счет'].map(mapping_account).astype('string')

        directory_ufr_df = directory_ufr_df.copy()
        directory_ufr_df.loc[:,'_key'] = (
            directory_ufr_df.loc[:, 'счет'].astype(str) + '_' +
            directory_ufr_df.loc[:, 'ном_группа_1с'].astype(str)
        )
        df_result.loc[:, '_key'] = (
            df_result.loc[:, 'счет'].astype(str) + '_' +
            df_result.loc[:, 'ном_группа'].astype(str)
        )

        mapping_revenue = (
            directory_ufr_df
            .drop_duplicates(subset='_key')
            .set_index('_key')['строка_уфр']
        )
        df_result['вид_дохода_расхода'] = df_result['_key'].map(mapping_revenue).astype('string')

        df_result.drop(columns='_key', inplace=True)
        directory_ufr_df.drop(columns='_key', inplace=True)

        mapping_segment = (
            directory_ufr_df
            .drop_duplicates(subset='ном_группа_1с')
            .set_index('ном_группа_1с')['сегмент']
        )
        df_result['сегмент'] = df_result['ном_группа'].map(mapping_segment).astype('string')

        self._validate_mapping_completeness(
            df_result, mapping_revenue, mapping_segment
        )

        group_unique = group_companies_df.drop_duplicates(subset='ВариантыНазвания')

        mapping_group = group_unique.set_index('ВариантыНазвания')['ВидСвязиКА'].astype('string')
        mapping_segment_ka = group_unique.set_index('ВариантыНазвания')['сегмент'].astype('string')

        df_result['группа_ка'] = df_result['контрагент'].map(mapping_group).fillna('3 лица').astype('string')
        df_result['сегмент_ка'] = df_result['контрагент'].map(mapping_segment_ka).fillna('3 лица').astype('string')

        self._validate_group_ka_values(df_result)

        df_result['вид_связи'] = self._calculate_connection_type(df_result)

        logger.debug(
            "Обогащение завершено: {}",
            df_result['вид_связи'].value_counts().to_dict(),
        )

        return df_result

    def _validate_mapping_completeness(
        self,
        df_result: pd.DataFrame,
        mapping_revenue: pd.Series,
        mapping_segment: pd.Series,
    ) -> None:
        """Проверяет полноту маппинга вид_дохода_расхода и сегмент."""
        unmapped_mask = df_result['вид_дохода_расхода'].isna() | df_result['сегмент'].isna()

        if unmapped_mask.any():
            problem_groups = df_result.loc[unmapped_mask, 'ном_группа'].unique()

            problem_data = pd.DataFrame({
                'ном_группа_без_маппинга': problem_groups,
                'строка_уфр_в_справочнике': [
                    mapping_revenue.get(g, 'ОТСУТСТВУЕТ') for g in problem_groups
                ],
                'сегмент_в_справочнике': [
                    mapping_segment.get(g, 'ОТСУТСТВУЕТ') for g in problem_groups
                ],
            })

            raise MissingMappingError(
                message=(
                    f"В справочнике УФР отсутствуют записи для "
                    f"{len(problem_groups)} ном_групп из отчёта по проводкам"
                ),
                problem_data=problem_data,
                reference_name="Справочник строк УФР (directory_ufr)",
            )

    def _validate_group_ka_values(self, df_result: pd.DataFrame) -> None:
        """Проверяет наличие неожиданных значений в группа_ка."""
        expected_groups = {'3 лица', 'Прочие ГАП', 'ГСК'}
        actual_groups = set(df_result['группа_ка'].unique())
        unexpected_groups = actual_groups - expected_groups

        if unexpected_groups:
            logger.warning(
                "[!] В столбце 'группа_ка' обнаружены неожиданные значения: {}. "
                "Ожидались только: {}",
                unexpected_groups,
                expected_groups,
            )

    def _calculate_connection_type(self, df_result: pd.DataFrame) -> pd.Series:
        """Рассчитывает вид_связи на основе группа_ка и сегмент_ка."""
        conditions = [
            df_result['группа_ка'] == '3 лица',
            df_result['группа_ка'] == 'Прочие ГАП',
            (df_result['группа_ка'] == 'ГСК') & (df_result['сегмент_ка'] == df_result['сегмент']),
            (df_result['группа_ка'] == 'ГСК') & (df_result['сегмент_ка'] != df_result['сегмент']),
        ]
        choices = [
            '3 лица',
            'Прочие ГАП',
            'ГСК внутрисегмент.',
            'ГСК межсегмент.',
        ]
        result = np.select(conditions, choices, default='не_указано')
        return pd.Series(result, index=df_result.index, dtype='string')

    def _merge_with_reassessment(
        self,
        df_result: pd.DataFrame,
        df9002_16: pd.DataFrame,
    ) -> pd.DataFrame:
        """Объединяет основной результат с переоценкой."""
        logger.debug("Объединение с переоценкой")

        if df9002_16.empty:
            logger.debug("Переоценка отсутствует, объединение не требуется")
            return df_result

        df9002_16_aligned = df9002_16.reindex(
            columns=df_result.columns,
            fill_value="не_указано"
        )

        df_final = pd.concat([df_result, df9002_16_aligned], ignore_index=True)

        text_cols = [
            'счет', 'контрагент', 'ном_группа', 'доход_расход',
            'вид_дохода_расхода', 'сегмент', 'группа_ка', 'сегмент_ка', 'вид_связи'
        ]
        for col in text_cols:
            if col in df_final.columns:
                df_final[col] = df_final[col].astype('string')

        logger.debug(
            "Объединение завершено: {} + {} = {} строк",
            len(df_result),
            len(df9002_16),
            len(df_final),
        )

        return df_final
