# Project data source

Use `data/` as this project's data source. It is a Windows directory junction
pointing to `C:\Users\kenny\Documents\lending-club-data`; files are not copied.
Resolve `data/` relative to the project root, not an arbitrary working directory.

Available inputs:

- `data/accepted_2007_to_2018Q4.csv.gz`
- `data/rejected_2007_to_2018Q4.csv.gz`
- `data/accepted_2007_to_2018q4.csv/accepted_2007_to_2018Q4.csv`
- `data/rejected_2007_to_2018q4.csv/rejected_2007_to_2018Q4.csv`
- `data/LCDataDictionary.xlsx`

Treat source data as read-only unless the user explicitly requests changes.
Writes through the junction change the original files. Save generated outputs
in `outputs/` and derived datasets in `processed/` inside this project.
Ignore temporary Excel lock files whose names begin with `~$`.
The CSV files are large; use streaming, selected columns, or chunked reads.

The junction is local to this computer and excluded from Git. If the project
is moved to another computer, recreate `data/` pointing to that computer's
data folder.
