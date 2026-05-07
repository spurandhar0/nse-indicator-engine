"""
NSE Indicator Consolidator
Reads all CSVs from indicator_data/DD-Mon-YYYY/ and writes
one Excel file per day to consolidated_excel/DD-Mon-YYYY.xlsx
Each CSV becomes a separate sheet (named after the CSV file).
"""

import os
import sys
import glob
import pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
today = datetime.now(IST).strftime("%d-%b-%Y")  # e.g. 07-May-2026

# Allow override via argument
if len(sys.argv) > 1:
    today = sys.argv[1]

source_dir = f"indicator_data/{today}"
output_dir = "consolidated_excel"
output_file = f"{output_dir}/{today}.xlsx"

print(f"=== NSE Indicator Consolidator ===")
print(f"Date       : {today}")
print(f"Source dir : {source_dir}")
print(f"Output     : {output_file}")

if not os.path.isdir(source_dir):
    print(f"ERROR: Source directory not found: {source_dir}")
    sys.exit(1)

os.makedirs(output_dir, exist_ok=True)

csv_files = sorted(glob.glob(f"{source_dir}/*.csv"))
if not csv_files:
    print("ERROR: No CSV files found!")
    sys.exit(1)

print(f"\nFound {len(csv_files)} CSV files. Building Excel...")

# Sheet name mapping: filename (no ext) → clean sheet name (max 31 chars)
def sheet_name(filepath):
    name = os.path.splitext(os.path.basename(filepath))[0]
    # Excel sheet names max 31 chars
    return name[:31]

with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
    summary_rows = []

    for csv_path in csv_files:
        sname = sheet_name(csv_path)
        print(f"  → Sheet: {sname:<35}", end="")
        try:
            df = pd.read_csv(csv_path, low_memory=False)
            # Truncate to 1,000,000 rows (Excel limit)
            if len(df) > 1_000_000:
                df = df.iloc[:1_000_000]
                print(f"({len(df)} rows, TRUNCATED) ", end="")
            df.to_excel(writer, sheet_name=sname, index=False)
            print(f"{len(df)} rows ✅")
            summary_rows.append({"Sheet": sname, "Rows": len(df), "Columns": len(df.columns), "Status": "OK"})
        except Exception as e:
            print(f"ERROR: {e}")
            summary_rows.append({"Sheet": sname, "Rows": 0, "Columns": 0, "Status": str(e)})

    # Add a Summary sheet as the first sheet
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_excel(writer, sheet_name="SUMMARY", index=False)
    # Move SUMMARY to first position
    wb = writer.book
    wb.move_sheet("SUMMARY", offset=-len(wb.sheetnames))

print(f"\n✅ Excel written: {output_file}")
file_size = os.path.getsize(output_file) / (1024 * 1024)
print(f"   File size: {file_size:.2f} MB")
