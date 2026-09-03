import subprocess

# Add all modified files
result = subprocess.run(['git', 'add', '-A'], capture_output=True, text=True)
print(f'git add -A returncode: {result.returncode}')
if result.stderr:
    print(f'stderr: {result.stderr[:500]}')

# Commit
msg = '''fix: resolve Error 2 and related bugs in MЭЗ processing

- step_01a: filter OPU accounts by all-prefixes instead of 2-char prefixes
- transaction_report.py: soft warning for empty posting reports; remove premature dropna
- file_handler.py: handle empty processor result without not_correct_files
- step_02: cast to numeric and filter .notna() & (!= 0) for NaN balance rows
- osv_account.py: handle single-line OSV files (e.g. 05, 07, 09) without Level/Курсив columns
- logger_config.py: fix log file path handling
- executors.py: add debug logging for step metrics
- cli/main.py: minor argument parsing improvements
'''

result = subprocess.run(['git', 'commit', '-m', msg], capture_output=True, text=True)
print(f'git commit returncode: {result.returncode}')
if result.stdout:
    print(f'stdout: {result.stdout[:500]}')
if result.stderr:
    print(f'stderr: {result.stderr[:500]}')
