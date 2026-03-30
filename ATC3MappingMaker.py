"""
ATC3 Mapping Maker
==================
Creates mapping tables for companies with same ATC3 drugs by year and quarter.

Tasks:
1. Read boardex_ssr_price_sample.csv and extract year, quarter, product, atc3, BoardName
2. Create year-level mapping: for each company-product-atc3-year combination, 
   find all other companies in the same year with the same atc3 product
3. Create quarter-level mapping: same logic but within each quarter
4. Output to data/atc3mapping/atc3mapping_year(quarter)_level.csv

ATC Level:
- atc_level=1: Use atc3 as is
- atc_level=2: Remove last character from atc3 strings (e.g., 'A01A' -> 'A01')
"""

import pandas as pd
import sys
from pathlib import Path


def process_data(level='year', atc_level=1):
    """
    Process ATC3 mapping data at specified level (year or quarter).
    
    Parameters:
    -----------
    level : str
        Either 'year' or 'quarter'
    atc_level : int
        1: Use atc3 as is
        2: Remove last character from atc3 strings
    
    Returns:
    --------
    result_df : pd.DataFrame
        Mapping table with peer companies
    """

    # Read data
    df = pd.read_csv(interim_data_path / "boardex_ssr_price_sample.csv")
    
    # Process ATC3 column based on atc_level
    if atc_level == 2:
        # Remove last character from atc3 strings
        df['atc3'] = df['atc3'].astype(str).str[:-1]
    elif atc_level != 1:
        raise ValueError("atc_level must be 1 or 2")
    
    # Select columns based on level
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
    
    # For year level, remove duplicates; for quarter level, data should be unique already
    if level == 'year':
        df_level = df_level.drop_duplicates().reset_index(drop=True)
    
    # Check uniqueness of base columns
    duplicates = df_level[df_level.duplicated(subset=uniqueness_cols, keep=False)]
    if len(duplicates) > 0:
        print(f"\nERROR: {', '.join(uniqueness_cols)} are NOT unique!")
        print(f"Found {len(duplicates)} duplicate entries:")
        print(duplicates.sort_values(uniqueness_cols).head(20))
        sys.exit(1)
    
    print(f"Uniqueness check passed: {', '.join(uniqueness_cols)} are all unique")
    
    # Expand rows using merge
    # Create a mapping table with unique (groupby_cols, BoardName) combinations
    peers_table = df_level[groupby_cols + ['BoardName']].copy()
    peers_table = peers_table.drop_duplicates().reset_index(drop=True)  # Remove duplicates
    peers_table.columns = groupby_cols + ['BoardNamePair']
    
    # Merge to create all combinations
    result_df = df_level.merge(peers_table, on=groupby_cols, how='inner')
    
    # Remove self-pairs (BoardName == BoardNamePair)
    result_df = result_df[result_df['BoardName'] != result_df['BoardNamePair']].reset_index(drop=True)
    
    # Keep only required columns and enforce output schema.
    if level == 'year':
        output_cols = ['year', 'product', 'atc3', 'BoardName', 'BoardNamePair']
    else:
        output_cols = ['year', 'quarter', 'product', 'atc3', 'BoardName', 'BoardNamePair']
    result_df = result_df[output_cols]

    # Sort for consistency
    sort_cols = [col for col in output_cols if col != 'product']
    result_df = result_df.sort_values(sort_cols).reset_index(drop=True)
    return result_df


def main():
    """
    Main execution function.
    """
    
    # Create output directory if it doesn't exist
    output_dir = workspace_root / "data" / "atc3mapping"
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nOutput directory: {output_dir}")
    
    # Process both atc_level 1 and 2
    for atc_level in [2]: # 1
        level_suffix = "" if atc_level == 1 else "_level2"

        # Process year-level data
        df_year = process_data(level='year', atc_level=atc_level)
        year_output_path = output_dir / f"atc3mapping_year_level{level_suffix}.csv"
        df_year.to_csv(year_output_path, index=False)
        print(f"Year-level data saved to: {year_output_path}")
        
        """
        # Process quarter-level data
        df_quarter = process_data(level='quarter', atc_level=atc_level)
        quarter_output_path = output_dir / f"atc3mapping_quarter_level{level_suffix}.csv"
        df_quarter.to_csv(quarter_output_path, index=False)
        print(f"Quarter-level data saved to: {quarter_output_path}")
        """

if __name__ == "__main__":
    # Set up paths
    # Get the directory of this script (codes directory)
    script_dir = Path(__file__).resolve().parent
    workspace_root = script_dir.parent  # Go up one level to BoardPharma root
    
    interim_data_path = workspace_root / "InterimData"
    
    csv_path = interim_data_path / "boardex_ssr_price_sample.csv"
    
    # Run main processing
    main()
