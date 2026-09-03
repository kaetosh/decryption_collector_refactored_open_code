#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Удаление дублирующегося метода _process_dataframe_optimized из osv_account.py"""

filepath = r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code\data_processors\osv_account.py'

with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Ищем начало дублирующегося метода (строка 359, индекс 358)
# Ищем конец дублирующегося метода (строка 464, индекс 463)
# Ищем начало следующего метода _shift_level_columns_vectorized

start_idx = None
end_idx = None

for i, line in enumerate(lines):
    if '    def _process_dataframe_optimized(self, df: pd.DataFrame) -> pd.DataFrame:' in line:
        if start_idx is None:
            start_idx = i
    if '    def _shift_level_columns_vectorized(self, df: pd.DataFrame, account_for_table: str) -> pd.DataFrame:' in line:
        if end_idx is None:
            end_idx = i
            break

print(f"Found _process_dataframe_optimized at line {start_idx + 1}")
print(f"Found _shift_level_columns_vectorized at line {end_idx + 1}")

if start_idx is not None and end_idx is not None:
    # Удаляем строки от start_idx до end_idx (не включая end_idx)
    new_lines = lines[:start_idx] + lines[end_idx:]
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print(f"Removed {end_idx - start_idx} lines. File updated.")
else:
    print("Could not find the duplicate method.")