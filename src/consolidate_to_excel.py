"""
NSE Indicator Consolidator — ALL DATES
Reads all CSVs from every indicator_data/DD-Mon-YYYY/ folder
and writes one Excel file per date into consolidated_excel/
Skips dates that already have an Excel file (unless --force is passed).
"""

import os
import sys
import glob
import pandas as pd
from pathlib import Path

INDICATOR_DIR = Path("indicator_data")
OUTPUT_DIR    = Path("consolidated_excel")
OUTPUT_DIR.mkdir(exist_ok=True)

force = "--force" in sys.argv

date_folders = sorted([d for d in INDICATOR_DIR.iterdir() if d.is_dir()])
print(f"Found {len(date_folders)} date folders.")

for date_folder in date_folders:
    date_label = date_folder.name          # e.g. 06-May-2026
    out_file   = OUTPUT_DIR / f"{date_label}.xlsx"

    if out_file.exists() and not force:
        print(f"  SKIP {date_label} (already exists)")
        continue

    csv_files = sorted(date_folder.glob("*.csv"))
    if not csv_files:
        print(f"  SKIP {date_label} (no CSVs)")
        continue

    print(f"  Processing {date_label} — {len(csv_files)} CSVs...", end=" ", flush=True)

    summary_rows = []
    with pd.ExcelWriter(out_file, engine="openpyxl") as writer:
        for csv_path in csv_files:
            sheet_name = csv_path.stem[:31]   # Excel sheet name max 31 chars
            try:
                df = pd.read_csv(csv_path)
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                summary_rows.append({"Sheet": sheet_name, "Rows": len(df), "Columns": len(df.columns), "Status": "OK"})
            except Exception as e:
                summary_rows.append({"Sheet": sheet_name, "Rows": 0, "Columns": 0, "Status": str(e)})

        # Write summary as first sheet
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_excel(writer, sheet_name="SUMMARY", index=False)
        # Move SUMMARY to first position
        wb = writer.book
        wb.move_sheet("SUMMARY", offset=-len(wb.sheetnames)+1)

    print(f"✅  → {out_file.name}")

print("\nAll done!")
