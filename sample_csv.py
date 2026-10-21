"""Extract reproducible random samples from both Lending Club CSV files.

Run: python sample_csv.py --rows 10000 --seed 42
Uses only the Python standard library. Sources are read-only; samples are
written to processed/ relative to this script, regardless of the working folder.
"""

import argparse
import csv
from pathlib import Path
import random


PROJECT_ROOT = Path(__file__).resolve().parent
SOURCES = {
    "accepted": PROJECT_ROOT / "data" / "accepted_2007_to_2018q4.csv"
    / "accepted_2007_to_2018Q4.csv",
    "rejected": PROJECT_ROOT / "data" / "rejected_2007_to_2018q4.csv"
    / "rejected_2007_to_2018Q4.csv",
}


def sample_csv(source_path: Path, output_path: Path, rows: int, seed: int) -> None:
    """Use reservoir sampling: uniform selection without replacement, O(rows) memory.

    Each eligible source record has the same probability of being selected.
    Duplicate values already present in the source remain eligible as separate
    records. Only blank records and Lending Club's funded-total footers are skipped.
    """
    if rows < 1:
        raise ValueError("rows must be at least 1")
    if source_path.resolve() == output_path.resolve():
        raise ValueError("The output must not overwrite the source CSV")

    rng = random.Random(seed)
    reservoir = []
    seen = skipped = 0
    print(f"Sampling {source_path.name} ...", flush=True)

    with source_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source, strict=True)
        header = next(reader, [])
        if not header or len(set(header)) != len(header):
            raise ValueError(f"{source_path.name}: missing or duplicate column names")

        for record_number, row in enumerate(reader, start=2):
            if not row or not any(row):
                skipped += 1
                continue
            if (
                header[0] == "id"
                and row[0].startswith("Total amount funded in policy code ")
                and not any(row[1:])
            ):
                skipped += 1
                continue
            if len(row) != len(header):
                raise ValueError(
                    f"{source_path.name}: record {record_number} has "
                    f"{len(row)} fields; expected {len(header)}"
                )

            seen += 1
            if seen <= rows:
                reservoir.append((record_number, row))
            else:
                position = rng.randrange(seen)
                if position < rows:
                    reservoir[position] = (record_number, row)
            if seen % 5_000_000 == 0:
                print(f"  Read {seen:,} data rows ...", flush=True)

    if seen < rows:
        raise ValueError(f"{source_path.name}: requested {rows:,} rows; found {seen:,}")

    # Keep sampled records in their original relative order; do not convert values.
    reservoir.sort(key=lambda item: item[0])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(row for _, row in reservoir)

    print(
        f"Saved {output_path.name}: {rows:,} rows, {len(header)} columns "
        f"(from {seen:,} data rows; skipped {skipped:,} blank/footer records).",
        flush=True,
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10_000, help="Rows per file (default: 10000)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    args = parser.parse_args(argv)
    if args.rows < 1:
        parser.error("--rows must be at least 1")

    try:
        for name, source_path in SOURCES.items():
            output_path = PROJECT_ROOT / "processed" / f"{name}_sample_{args.rows}.csv"
            sample_csv(source_path, output_path, args.rows, args.seed)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Sampling failed: {error}\n")


if __name__ == "__main__":
    main()
