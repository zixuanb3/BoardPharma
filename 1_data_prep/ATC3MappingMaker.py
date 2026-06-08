"""
Purpose:
Build ATC peer-mapping tables from SSR product data. Each row links one
firm-product record to other firms in the same ATC cell and time cell.

Process:
- Load SSR product records with year, quarter, product, atc3, and BoardName.
- Apply ATC granularity based on atc_level.
- Define matching cells by level:
    year -> year + atc3, quarter -> year + quarter + atc3.
- Expand matches within each cell, remove self-pairs, and export mapping tables.

Input:
- InterimData/boardex_ssr_price_sample.csv

Output:
- data/atc3mapping/atc*mapping_year.csv
- data/atc3mapping/atc*mapping_quarter.csv
"""

import pandas as pd
import sys
from pathlib import Path


# ========================== USER CONFIG ==========================
# levels:
# - "year" or "quarter".
# - year matches in year + atc3 cells.
# - quarter matches in year + quarter + atc3 cells.
# - Quarter matching is stricter, so peer sets are usually smaller and more time-specific.
#
# atc_levels:
# - "atc1" keeps "Device" if it starts with "Device", otherwise keeps only the first character.
# - "atc2" truncates the last character and coarsens therapeutic categories.
# - "atc3" keeps original ATC3.

RUN_CONFIG = {
    "levels": ["year"],
    "atc_levels": ["atc2","atc3"],
}
# ===============================================================


def process_data(level='year', atc_level='atc3'):
    """
    Build peer mappings under one level and one ATC granularity.
    """

    # Load source rows for peer-cell construction.
    df = pd.read_csv(interim_data_path / "boardex_ssr_price_sample.csv")
    
    # atc_level controls therapeutic aggregation before matching.
    if atc_level == 'atc1':
        df['atc3'] = df['atc3'].astype(str).apply(lambda x: 'Device' if x.startswith('Device') else x[0])
    elif atc_level == 'atc2':
        df['atc3'] = df['atc3'].astype(str).str[:-1]
    elif atc_level != 'atc3':
        raise ValueError("atc_level must be 'atc1', 'atc2', or 'atc3'")
    
    # level defines time cell used for matching and uniqueness checks.
    if level == 'year':
        columns = ['year', 'product', 'atc3', 'BoardName']
        groupby_cols = ['year', 'atc3']
        uniqueness_cols = ['year', 'product', 'BoardName']
    elif level == 'quarter':
        columns = ['year', 'quarter', 'product', 'atc3', 'BoardName']
        groupby_cols = ['year', 'quarter', 'atc3']
        uniqueness_cols = ['year', 'quarter', 'product', 'BoardName']
    else:
        raise ValueError("level must be 'year' or 'quarter'")
    
    df_level = df[columns].copy()
    
    # Year mode removes source duplicates before pair expansion.
    if level == 'year':
        df_level = df_level.drop_duplicates().reset_index(drop=True)
    
    # Enforce one product has only one atc3 before matching.
    duplicates = df_level[df_level.duplicated(subset=uniqueness_cols, keep=False)]
    if len(duplicates) > 0:
        print(f"\nERROR: {', '.join(uniqueness_cols)} are NOT unique!")
        print(f"Found {len(duplicates)} duplicate entries:")
        print(duplicates.sort_values(uniqueness_cols).head(20))
        sys.exit(1)
    
    print(f"Uniqueness check passed: {', '.join(uniqueness_cols)} are all unique")
    
    # Build peer lookup table at the matching-cell level.
    peers_table = df_level[groupby_cols + ['BoardName']].copy()
    peers_table = peers_table.drop_duplicates().reset_index(drop=True)
    peers_table.columns = groupby_cols + ['BoardNamePair']
    
    # Expand to all within-cell firm pairs.
    result_df = df_level.merge(peers_table, on=groupby_cols, how='inner')
    
    # Remove self-matches.
    result_df = result_df[result_df['BoardName'] != result_df['BoardNamePair']].reset_index(drop=True)
    
    # Keep output schema by level.
    if level == 'year':
        output_cols = ['year', 'product', 'atc3', 'BoardName', 'BoardNamePair']
    else:
        output_cols = ['year', 'quarter', 'product', 'atc3', 'BoardName', 'BoardNamePair']
    result_df = result_df[output_cols]

    # Sort for deterministic output.
    sort_cols = [col for col in output_cols if col != 'product']
    result_df = result_df.sort_values(sort_cols).reset_index(drop=True)
    return result_df


def main():
    """
    Run configured level x atc_level combinations and export mapping files.
    """
    
    def ensure_list(v):
        if isinstance(v, str):
            return [v]
        return list(v)

    levels = ensure_list(RUN_CONFIG["levels"])
    atc_levels = ensure_list(RUN_CONFIG["atc_levels"])

    output_dir = workspace_root / "data" / "atc3mapping"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput directory: {output_dir}")
    
    for atc_level in atc_levels:
        for level in levels:
            result_df = process_data(level=level, atc_level=atc_level)
            output_path = output_dir / f"{atc_level}mapping_{level}.csv"
            result_df.to_csv(output_path, index=False)
            print(f"{level.capitalize()}-level data saved to: {output_path}")

if __name__ == "__main__":
    # Resolve project paths for data I/O.
    script_dir = Path(__file__).resolve().parent
    workspace_root = script_dir.parent.parent
    
    interim_data_path = workspace_root / "InterimData"
    
    main()
