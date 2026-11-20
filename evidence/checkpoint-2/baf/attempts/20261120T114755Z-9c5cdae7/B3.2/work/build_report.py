import csv
import re

rows = []
with open("task.md", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        if cells[0] == "id" or set(cells[0]) <= set("-: "):
            continue
        rows.append(cells)

rows.sort(key=lambda r: (r[1], r[0]))

with open("report.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writerow(["id", "date", "description", "amount"])
    w.writerows(rows)
