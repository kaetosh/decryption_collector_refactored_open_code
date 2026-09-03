#!/usr/bin/env python
# -*- coding: utf-8 -*-
filepath = r'C:\Users\a.karabedyan\Documents\PythonProject\decryption_collector_refactored_open_code\data_processors\osv_account.py'
with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()
new_lines = []
for i, line in enumerate(lines):
    if i == 273:
        new_lines.append('        df = self._process_header(df, header_row_idx, rename_columns=True)\n')
    elif 273 < i < 296:
        continue
    else:
        new_lines.append(line)
with open(filepath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done')