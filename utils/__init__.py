# utils/__init__.py
from .dataframe_utils import (
    cast_columns_to_types,
    align_dtypes_to_reference,
    find_header_index,
    set_header_from_row,
    get_required_columns_df,
    build_composite_key,
)
from .column_utils import (
    find_target_column,
    resolve_account_level_column,
    describe_level_columns,
    process_account,
    normalize_account,
)
from .currency_utils import (
    needs_conversion,
    get_currency,
    get_rate_for_date,
    get_rate_for_date_with_info,
    get_earliest_rate,
    get_balance_rate,
    get_last_rate_date,
    get_rate_median,
    get_rate_deviation_limit,
    convert_series,
    refresh_rub_equivalent,
    add_ruble_amount_column,
)
from .file_utils import (
    detect_txt_encoding,
    find_missing_files,
    find_register_file,
    format_filename_vectorized,
    normalize_ragged_tab_rows,
)
from .reference_scope import (
    ScopeDiagnostics,
    resolve_company_view,
)

__all__ = [
    # dataframe_utils
    'cast_columns_to_types',
    'align_dtypes_to_reference',
    'find_header_index',
    'set_header_from_row',
    'get_required_columns_df',
    # column_utils
    'find_target_column',
    'resolve_account_level_column',
    'describe_level_columns',
    'process_account',
    'normalize_account',
    # file_utils
    'detect_txt_encoding',
    'find_missing_files',
    'find_register_file',
    'format_filename_vectorized',
    'normalize_ragged_tab_rows',
    # currency_utils
    'needs_conversion',
    'get_currency',
    'get_rate_for_date',
    'get_rate_for_date_with_info',
    'get_earliest_rate',
    'get_balance_rate',
    'get_last_rate_date',
    'get_rate_median',
    'get_rate_deviation_limit',
    'convert_series',
    'refresh_rub_equivalent',
    'add_ruble_amount_column',
    # reference_scope
    'resolve_company_view',
    'ScopeDiagnostics',
]