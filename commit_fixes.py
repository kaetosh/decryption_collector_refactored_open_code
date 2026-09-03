import subprocess
import sys

files = [
    'data_processors/transaction_report.py',
    'data_processors/file_handler.py',
    'pipeline/steps/step_01a_list_registers.py',
    'pipeline/steps/step_02_flat_osv.py',
    'TASKS.md'
]

with open(r'C:\Users\a.karabedyan\git_commit_log.txt', 'w', encoding='utf-8') as log:
    result = subprocess.run(['git', 'add'] + files, capture_output=True, text=True)
    log.write(f'git add returncode: {result.returncode}\n')
    log.write(f'git add stderr: {result.stderr}\n')
    log.write(f'git add stdout: {result.stdout}\n')
    
    if result.returncode == 0:
        commit_msg = """fix: three bugs fixed during MEP testing

1. step_01a: filter by all prefixes of exact OSV accounts (all lengths),
   not just 2 chars. Excludes 90.07 from exports if no turnover.

2. transaction_report: soft warning + skip for empty posting reports
   instead of ValueError. file_handler: empty DataFrame without adding to not_correct_files.

3. step_02: pd.to_numeric + .notna() & (!= 0) to filter empty rows.
   764 rows with non-numeric values caused NaN, balance broke by -3.17M.

4. transaction_report: removed dropna(axis=1, how='all') from _load_txt_file,
   which deleted empty 'Subconto Kt' column for 90.08 before required_cols check.

TASKS.md: 'Error 2' marked as completed."""
        
        result = subprocess.run(['git', 'commit', '-m', commit_msg], capture_output=True, text=True)
        log.write(f'git commit returncode: {result.returncode}\n')
        log.write(f'git commit stderr: {result.stderr}\n')
        log.write(f'git commit stdout: {result.stdout}\n')
    else:
        log.write('git add failed, skipping commit\n')

print('Done')