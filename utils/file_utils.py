# -*- coding: utf-8 -*-
"""
Created on Mon Jun 22 09:30:02 2026

@author: a.karabedyan
"""

# utils/file_utils.py
import pandas as pd
from pathlib import Path
from typing import List, Optional
from loguru import logger

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
        logger.error(f"Папка '{folder_path}' не найдена или не является директорией")
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
    
    logger.warning(f"Файл не найден по критериям: {', '.join(criteria)}")
    return None