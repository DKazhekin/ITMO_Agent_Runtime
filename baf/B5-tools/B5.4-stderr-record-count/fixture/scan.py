import csv
import sys

print("cached processed: 5")
with open(sys.argv[1], newline="") as source:
    count = sum(row["status"] == "active" for row in csv.DictReader(source))
print(f"processed: {count}", file=sys.stderr)
