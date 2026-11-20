"""Read normal OOXML workbook data without recalculating formulas."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
from zipfile import ZipFile

SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def workbook_cells(path: Path) -> tuple[list[str], dict]:
    with ZipFile(path) as book:
        entries = book.infolist()
        filenames = {entry.filename for entry in entries}
        if len(entries) > 100 or len(filenames) != len(entries) or sum(entry.file_size for entry in entries) > 4_194_304:
            raise ValueError("Unexpected or oversized workbook archive")

        def xml(name: str) -> ET.Element:
            return ET.fromstring(book.read(name))

        def strings(node: ET.Element) -> str:
            return "".join(child.text or "" for child in node.iter(SHEET + "t"))

        shared = []
        if "xl/sharedStrings.xml" in filenames:
            shared = [strings(item) for item in xml("xl/sharedStrings.xml")]
        links = {item.attrib["Id"]: item.attrib["Target"]
                 for item in xml("xl/_rels/workbook.xml.rels").findall(PACKAGE + "Relationship")}
        sheet_names = []
        cells = {}
        sheets = xml("xl/workbook.xml").findall(f"{SHEET}sheets/{SHEET}sheet")
        for sheet in sheets:
            name = sheet.attrib["name"]
            if name in sheet_names:
                raise ValueError("Duplicate workbook sheet")
            sheet_names.append(name)
            target = links[sheet.attrib[REL + "id"]]
            part = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            for cell in xml(part).findall(f"{SHEET}sheetData/{SHEET}row/{SHEET}c"):
                reference = (name, cell.attrib["r"])
                formula = cell.find(SHEET + "f")
                raw = cell.findtext(SHEET + "v")
                kind = cell.attrib.get("t", "n")
                if formula is not None:
                    if formula.attrib or not formula.text:
                        raise ValueError(f"Expected a normal formula in {reference[0]}!{reference[1]}")
                    value = ("formula", "=" + formula.text)
                elif kind == "inlineStr":
                    value = ("text", strings(cell))
                elif kind == "s":
                    value = ("text", shared[int(raw)])
                elif raw is None:
                    continue
                elif kind == "str":
                    value = ("text", raw)
                elif kind == "n":
                    value = ("number", Decimal(raw))
                else:
                    value = (kind, raw)
                if reference in cells:
                    raise ValueError("Duplicate workbook cell")
                cells[reference] = value
    return sheet_names, cells


def check_workbook(original: Path, edited: Path, specification: dict) -> None:
    original_names, original_cells = workbook_cells(original)
    names, cells = workbook_cells(edited)
    destination = (specification["sheet"], specification["destination"])
    actual_formula = cells.pop(destination, None)
    original_cells.pop(destination, None)
    if actual_formula != ("formula", specification["expected"]):
        raise ValueError(f"Incorrect formula or cell type in {destination[0]}!{destination[1]}: {actual_formula!r}")
    if original_names != names or original_cells != cells:
        raise ValueError("Source formula, another cell, or sheet order changed")
