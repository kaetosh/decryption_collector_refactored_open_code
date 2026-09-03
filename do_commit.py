#!/usr/bin/env python
# -*- coding: utf-8 -*-
import subprocess

files = [
    'data_processors/transaction_report.py',
    'data_processors/file_handler.py',
    'pipeline/steps/step_01a_list_registers.py',
    'pipeline/steps/step_02_flat_osv.py'
]

# Add files
result = subprocess.run(['git', 'add'] + files, capture_output=True, text=True)
print('git add returncode:', result.returncode)
if result.stderr:
    print('git add stderr:', result.stderr[:500])

# Commit
msg = '''fix: resolve Error 2 and related bugs in MЭЗ processing

- step_01a: filter OPU accounts by all-prefixes instead of 2-char prefixes
- transaction_report.py: soft warning for empty posting reports; remove premature dropna
- file_handler.py: handle empty processor result without not_correct_files
- step_02: cast to numeric and filter .notna() & (!= 0) for NaN balance rows
'''

result = subprocess.run(['git', 'commit', '-m', msg], capture_output=True, text=True)
print('git commit returncode:', result.returncode)
if result.stdout:
    print('git commit stdout:', result.stdout[:500])
if result.stderr:
    print('git commit stderr:', result.stderr[:500])
