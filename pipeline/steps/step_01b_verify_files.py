"""
Шаг 1b: Проверка наличия всех необходимых файлов выгрузок.
"""
from loguru import logger
import pandas as pd
from pipeline.base import Step, ProcessingContext
from pipeline.errors import MissingFilesError
from pipeline.step_config import LeaseConstants
from config.defaults import DEFAULTS
from config.settings import (
    ACCOUNT_CARDS_DIR,
    REQUIRED_OSV_DIRS,
    SPECIAL_REPORTS_DIR
)
from utils import find_missing_files


class Step1bVerifyFilesStep(Step):
    """
    Шаг 1б: Проверка списка необходимых регистров для выгрузки.
    
    Проверяет наличие:
    - ОСВ для баланса (в accounts_osv и accounts_osv_lease)
    - Отчеты по проводкам для ОПУ (в transaction_report)
    - Спецотчеты для баланса (в special_reports)
    """

    def __init__(self):
        super().__init__(
            name="Шаг 1б: Проверка списка выгрузок",
            description="Отсутствие необходимых регистров на основе общей ОСВ вызовет ошибку"
        )

    def _process(self, context: ProcessingContext) -> ProcessingContext:
        # =========================================================================
        # ПРОВЕРКА ОСВ ДЛЯ БАЛАНСА
        # =========================================================================
        balance_filenames = context.data.get('expected_filenames', [])
        
        missing_balance = []
        missing_by_dir = {}
        
        for dir_config in REQUIRED_OSV_DIRS:
            dir_path = dir_config['path']
            dir_name = dir_path.name
            account_filter = dir_config.get('account_filter')
            exclude_accounts = dir_config.get('exclude_accounts')
            
            # Фильтруем файлы для этой папки
            dir_filenames = self._filter_filenames_for_dir(
                balance_filenames,
                account_filter=account_filter,
                exclude_accounts=exclude_accounts
            )
            
            if not dir_filenames:
                logger.debug("Нет ожидаемых ОСВ для папки {}", dir_name)
                continue
            
            # Проверяем наличие файлов
            missing = find_missing_files(dir_filenames, dir_path)
            
            if missing:
                missing_balance.extend(missing)
                missing_by_dir[dir_name] = missing
                logger.warning(
                    "В папке {} отсутствует {} файл(ов) ОСВ",
                    dir_name,
                    len(missing),
                )
            else:
                logger.debug(
                    "[OK] Все {} ОСВ найдены в {}", len(dir_filenames), dir_name
                )
        
        
        # =========================================================================
        # ПРОВЕРКА СПЕЦОТЧЕТОВ, точно нужны:
            # если период год, то анализ по 84 обязателен, чтобы разбить НРП на накопленную на начало года и текущего периода
            # ведомость амортизации арендованных ОС в разбивке по арендодателям, чтобы исключить ВГО
            # остальные - спецотчеты для УПП выгрузок, если их нет, скрипт заполняет значениями по умолчанию
        # =========================================================================
        # =========================================================================
        # Ведомость амортизации выгружается из 1С только по арендованным
        # объектам — у компании без аренды файла не существует. Поэтому она
        # условно обязательна (п. «Обязательность ведомости амортизации»):
        # необходимость решает общая ОСВ, авторитетная проверка — шаг 10.
        obligatory_list = []
        if context.type_period == 'год':
            obligatory_list.append(
                f'{context.company}_анализ_84_{context.period}_.xlsx'
            )

        vedamor_filename = f'{context.company}_ведамор_0102_{context.period}_.xlsx'
        if self._is_lease_present(context):
            obligatory_list.append(vedamor_filename)
        else:
            logger.info(
                "Остатков по счетам арендованных ОС (01.03/02.03) в общей ОСВ нет — "
                "ведомость амортизации не обязательна, файл '{}' не проверяется",
                vedamor_filename,
            )
        context.data['special_reports_obligatory_list'] = obligatory_list
        
        special_reports_filenames = context.data.get('special_reports_obligatory_list', [])
        
        missing_special_reports = []
        
        if special_reports_filenames:
            missing_special_reports = find_missing_files(special_reports_filenames, SPECIAL_REPORTS_DIR)
            
            if missing_special_reports:
                missing_by_dir['special_reports'] = missing_special_reports
                logger.warning(
                    "В папке special_reports отсутствует обязательные спецотчеты: {} шт.",
                    len(missing_special_reports),
                )
            else:
                logger.debug(
                    "[OK] Все {} спецотчеты в special_reports",
                    len(special_reports_filenames),
                )
        
        # =========================================================================
        # ПРОВЕРКА ОТЧЕТОВ ПО ПРОВОДКАМ ДЛЯ ОПУ
        # =========================================================================
        card_filenames = context.data.get('expected_card_filenames', [])
        
        missing_cards = []
        
        if card_filenames:
            missing_cards = find_missing_files(card_filenames, ACCOUNT_CARDS_DIR)
            
            if missing_cards:
                missing_by_dir['transaction_report'] = missing_cards
                logger.warning(
                    "В папке transaction_report отсутствует отчеты по проводкам: {} шт.",
                    len(missing_cards),
                )
            else:
                logger.debug(
                    "[OK] Все {} отчеты по проводкам найдены в account_cards",
                    len(card_filenames),
                )
        
        # =========================================================================
        # ОБЪЕДИНЕНИЕ ОШИБОК И ВЫБРОС ИСКЛЮЧЕНИЯ
        # =========================================================================
        all_missing = missing_balance + missing_cards + missing_special_reports
        
        if all_missing:
            # Формируем problem_data для Excel
            problem_data = self._build_missing_files_report(
                missing_by_dir,
                context.company,
                context.period
            )
            
            raise MissingFilesError(
                            message=f"Отсутствуют {len(all_missing)} обязательных выгрузок из 1С",
                            missing_files=all_missing,
                            problem_data=problem_data,
                            reference_name="Папки с выгрузками",
                            expected_dir=", ".join(missing_by_dir.keys()),  # ← ИСПРАВЛЕНО: только папки с проблемами
                            total_missing=len(all_missing),
                            missing_by_dir={k: len(v) for k, v in missing_by_dir.items()},
                        )
        
        logger.info("[OK] Все обязательные файлы выгрузок найдены")
        return context

    def _is_lease_present(self, context: ProcessingContext) -> bool:
        """
        Есть ли у компании арендованные ОС — по остаткам 01.03/02.03
        в общей ОСВ.

        Ведомость амортизации выгружается из 1С только по арендованным
        объектам (п. «Обязательность ведомости амортизации»), поэтому
        требовать её безусловно нельзя: у компании без аренды файла не
        существует. Здесь — быстрый предпросмотр на шаге 1б, чтобы не
        останавливать конвейер раньше времени; авторитетная проверка
        остаётся в шаге 10 (`_detect_leased_os` по сводной ОСВ).

        Порог — `tolerance_leased_os`, тот же допуск, что у сверки ОСВ
        с ведомостью: копеечный остаток от округления выгрузки не требует
        выгрузки и не должен останавливать прогон.

        Консервативно возвращает True (требуем файл), если данные для
        решения недоступны: точнее решить не можем, а лишний запрос файла
        лучше пропущенного стопа.

        Returns:
            bool: True — ведомость амортизации обязательна.
        """
        df = getattr(context, 'common_osv_df', None)
        if df is None or df.empty:
            return True

        required_cols = {'Счет', 'Дебет_конец', 'Кредит_конец'}
        if not required_cols.issubset(df.columns):
            logger.debug(
                "Не найдены колонки {} в общей ОСВ — ведомость амортизации "
                "считается обязательной (консервативное допущение)",
                sorted(required_cols - set(df.columns)),
            )
            return True

        # Префиксное сравнение: в общей ОСВ возможна детализация ниже
        # синтетического уровня ('01.03.1') — не хотим пропустить остаток.
        codes = df['Счет'].astype(str)
        mask = codes.str.startswith(tuple(LeaseConstants.ACCOUNTS_01_03))

        # Сальдо в общей ОСВ — в рублях (Дебет_конец - Кредит_конец),
        # допуск — в тыс.ед. (как в колонке «сальдо, тыс.ед.» сводной ОСВ).
        balance = (
            pd.to_numeric(df['Дебет_конец'], errors='coerce').fillna(0)
            - pd.to_numeric(df['Кредит_конец'], errors='coerce').fillna(0)
        )
        total_th = float(balance[mask].sum()) / 1000.0

        tolerance = (getattr(context, 'tolerance_params', None) or {}).get(
            'tolerance_leased_os',
            DEFAULTS['tolerance_leased_os'],
        )
        has_lease = bool(mask.any()) and abs(total_th) > tolerance

        logger.debug(
            "Арендованные ОС по общей ОСВ: счета {} — {} строк, "
            "сальдо={:.2f} тыс.ед., допуск={} -> ведомость {}",
            '/'.join(LeaseConstants.ACCOUNTS_01_03),
            int(mask.sum()),
            total_th,
            tolerance,
            'нужна' if has_lease else 'не нужна',
        )
        return has_lease

    def _filter_filenames_for_dir(
        self,
        filenames: list,
        account_filter: list = None,
        exclude_accounts: list = None
    ) -> list:
        """Фильтрует список файлов для конкретной папки."""
        if account_filter is None and exclude_accounts is None:
            return filenames
        
        filtered = []
        
        for filename in filenames:
            parts = filename.replace('.xlsx', '').split('_')
            if len(parts) < 3:
                continue
            
            account = parts[2]
            
            if account_filter and account not in account_filter:
                continue
            
            if exclude_accounts and account in exclude_accounts:
                continue
            
            filtered.append(filename)
        
        return filtered

    def _build_missing_files_report(
        self,
        missing_by_dir: dict,
        company_name: str,
        period: str
    ) -> pd.DataFrame:
        """Формирует DataFrame с информацией об отсутствующих файлах."""
        rows = []
        
        for dir_name, missing_files in missing_by_dir.items():
            for filename in missing_files:
                parts = filename.replace('.xlsx', '').split('_')
                
                if len(parts) >= 4:
                    rows.append({
                        'папка': dir_name,
                        'имя_файла': filename,
                        'компания': parts[0],
                        'регистр': parts[1],
                        'счет': parts[2],
                        'период': parts[3],
                        'статус': 'ОТСУТСТВУЕТ'
                    })
                else:
                    rows.append({
                        'папка': dir_name,
                        'имя_файла': filename,
                        'компания': '',
                        'регистр': '',
                        'счет': '',
                        'период': '',
                        'статус': 'ОТСУТСТВУЕТ'
                    })
        
        return pd.DataFrame(rows)
