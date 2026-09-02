# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:30:02 2026

@author: a.karabedyan
"""

# utils/file_utils.py
import pandas as pd
from pathlib import Path
from typing import List, Optional, Tuple
from loguru import logger

# ---------------------------------------------------------------------------
# Определение кодировки текстовых (TXT) выгрузок 1С
# ---------------------------------------------------------------------------

# Порядок важен: utf-8-sig проверяется первым — cp1251 почти всегда "декодирует"
# UTF-8-байты без ошибки (кракозябрами), поэтому строгую пробу UTF-8 надо делать раньше.
TXT_ENCODING_CANDIDATES = ('utf-8-sig', 'cp1251', 'cp866')

# Объём начала файла (байт), используемый для пробы декодирования
ENCODING_SAMPLE_SIZE = 5 * 1024 * 1024  # 5 МБ


def detect_txt_encoding(
    file_path: Path,
    sample_size: int = ENCODING_SAMPLE_SIZE,
) -> Tuple[str, str]:
    """Определяет кодировку текстового файла по его началу.

    Порядок определения:
      1. BOM (utf-8-sig / utf-16) — однозначная идентификация;
      2. строгая проба декодирования сэмпла по TXT_ENCODING_CANDIDATES;
      3. если ни одна не подошла — cp1251 с заменой неопределимых байтов
         (errors='replace') и предупреждением в лог.

    Args:
        file_path: Путь к текстовому файлу.
        sample_size: Сколько байт от начала файла использовать для пробы.

    Returns:
        Tuple[str, str]: пара (кодировка, режим ошибок) — пригодна для
        open(..., encoding=..., errors=...) и
        pd.read_csv(..., encoding=..., encoding_errors=...).
    """
    file_path = Path(file_path)

    with open(file_path, 'rb') as f:
        sample = f.read(sample_size)
    head = sample[:4]

    # 1. BOM (utf-8-sig заодно убирает BOM при последующем текстовом чтении)
    if head.startswith(b'\xef\xbb\xbf'):
        logger.debug("Кодировка {}: utf-8-sig (обнаружен BOM)", file_path.name)
        return 'utf-8-sig', 'strict'
    if head.startswith((b'\xff\xfe', b'\xfe\xff')):
        logger.debug("Кодировка {}: utf-16 (обнаружен BOM)", file_path.name)
        return 'utf-16', 'strict'

    # 2. Строгая проба по кандидатам
    for encoding in TXT_ENCODING_CANDIDATES:
        try:
            sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        logger.debug("Кодировка {}: {} (строгая проба сэмпла)", file_path.name, encoding)
        return encoding, 'strict'

    # 3. Последний рубеж — читаем с заменой неопределимых байтов
    logger.warning(
        "Кодировка {} не определена однозначно — читаем как cp1251 "
        "с заменой неопределимых байтов (errors='replace')",
        file_path.name,
    )
    return 'cp1251', 'replace'


def format_filename_vectorized(df: pd.DataFrame) -> list:
    """Векторизованное формирование имен файлов (работает в разы быстрее apply)"""
    return (
        df['Сокращенное Наименование компании'].astype(str) + '_' +
        df['регистр'].astype(str) + '_' +
        df['счет'].astype(str) + '_' +
        df['Период Отчетности'].astype(str) + '_.xlsx'
    ).tolist()

def find_missing_files(filenames: List[str], folder_path: str = 'INPUT_DATA') -> List[str]:
    """Возвращает список файлов из filenames, которых нет в указанной папке."""
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        logger.error("Папка '{}' не найдена или не является директорией", folder_path)
        return filenames.copy()
        
    existing_files = {f.name for f in folder.iterdir() if f.is_file()}
    return list(set(filenames) - existing_files)

def find_register_file(
    folder_path: Path,
    type_register: Optional[str] = None,
    account_number: Optional[str] = None,
    company_name: Optional[str] = None,
    period: Optional[str] = None
) -> Optional[Path]:
    """
    Находит единственный файл по критериям в указанной папке.
    Формат имени: CompanyName_typeRegister_accountNumber_period_.xlsx
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        raise FileNotFoundError(f"Папка '{folder_path}' не найдена")
        
    files = [f for f in folder.glob('*.xlsx') if not f.name.startswith('~$')]
    
    if not files:
        return None
        
    for file_path in files:
        name = file_path.stem.rstrip('_')
        parts = name.split('_')
        if len(parts) < 4:
            continue
            
        file_company = parts[0]
        file_type = parts[1]
        file_account = parts[2]
        file_period = parts[3]
        
        if company_name and file_company != company_name:
            continue
        if type_register and file_type != type_register:
            continue
        if account_number and file_account != account_number:
            continue
        if period and file_period != period:
            continue
            
        return file_path
        
    criteria = []
    if company_name: criteria.append(f"компания='{company_name}'")
    if type_register: criteria.append(f"тип='{type_register}'")
    if account_number: criteria.append(f"счет='{account_number}'")
    if period: criteria.append(f"период='{period}'")
    
    logger.warning("Файл не найден по критериям: {}", ', '.join(criteria))
    return None
