"""Excel (.xlsx) template and reader of the layer vaccination schedule.

Only file handling lives here: building the template and reading the uploaded sheet into rows keyed
by column name. Validating the values and replacing the schedule is done in ``layer_health`` with
the same rules as the single-item endpoints.

The sheet follows the ``layer_vaccinations`` columns: one row per vaccination, a header row with
the column names (any order; the optional columns may be left out).
"""

import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from app.core.errors import AppError
from app.db.models import LAYER_HEALTH_METHODS

SHEET = "Vaccinations"
INSTRUCTIONS_SHEET = "Instructions"
COLUMNS = ("age_week", "vaccine_name", "target_disease", "method", "dosage", "notes")
REQUIRED_COLUMNS = ("age_week", "vaccine_name", "method")
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILE_NAME = "layer-vaccination-schedule.xlsx"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 500
# Rows looked at (including blank ones) before giving up: a sheet is never that long.
MAX_SCANNED_ROWS = 5000
# Rows of the template that get the drop-down and number checks.
TEMPLATE_ROWS = 500

COLUMN_HELP = {
    "age_week": "Required. Age of the hens in whole weeks (0-200). The vaccine can be given on any day of that age week.",
    "vaccine_name": "Required. Vaccine name (up to 255 characters). The same vaccine only once per age week.",
    "target_disease": "Optional. Diseases it protects against, e.g. ND, IB.",
    "method": "Required. How it is given: one of the methods below (pick it from the drop-down).",
    "dosage": "Optional. Free text, e.g. 1 tetes/ekor or 0,5 ml/ekor.",
    "notes": "Optional. Instructions for the ABK (up to 3000 characters).",
}
METHOD_HELP = {
    "EYE_DROP": "Eye drop (tetes mata)",
    "NASAL_DROP": "Nose drop (tetes hidung)",
    "ORAL": "By mouth (tetes mulut / cekok)",
    "DRINKING_WATER": "In the drinking water (air minum)",
    "FEED": "In the feed (pakan)",
    "SPRAY": "Spray",
    "INTRAMUSCULAR": "Injection into the muscle (suntik otot / IM)",
    "SUBCUTANEOUS": "Injection under the skin (suntik bawah kulit / SC)",
    "WING_WEB": "Wing web stab (tusuk sayap)",
    "OTHER": "Other: explain it in notes",
}


def template(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """The .xlsx template, prefilled with ``rows`` (values by column name)."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = SHEET
    sheet.append(list(COLUMNS))
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
    for row in rows:
        sheet.append([row.get(column) for column in COLUMNS])
    sheet.freeze_panes = "A2"
    for letter, width in zip("ABCDEF", (10, 30, 24, 18, 24, 50), strict=True):
        sheet.column_dimensions[letter].width = width
    last = TEMPLATE_ROWS + 1
    ages = DataValidation(
        type="whole",
        operator="between",
        formula1="0",
        formula2="200",
        showErrorMessage=True,
        errorTitle="age_week",
        error="A whole number of weeks from 0 to 200.",
    )
    methods = DataValidation(
        type="list",
        formula1='"' + ",".join(LAYER_HEALTH_METHODS) + '"',
        showErrorMessage=True,
        errorTitle="method",
        error="Pick a method from the list.",
    )
    sheet.add_data_validation(ages)
    sheet.add_data_validation(methods)
    ages.add(f"A2:A{last}")
    methods.add(f"D2:D{last}")

    help_sheet = workbook.create_sheet(INSTRUCTIONS_SHEET)
    wrap = Alignment(wrap_text=True, vertical="top")
    lines: list[list[str]] = [
        ["Layer vaccination schedule"],
        [
            "Fill in the Vaccinations sheet, one row per vaccination, and upload the file. "
            "Uploading REPLACES THE WHOLE SCHEDULE: a vaccination not in the file is no longer valid "
            "(it stops being reminded). Keep the header row as it is. Blank rows are ignored."
        ],
        [],
        ["Column", "Description"],
        *([column, COLUMN_HELP[column]] for column in COLUMNS),
        [],
        ["Method", "Meaning"],
        *([method, METHOD_HELP[method]] for method in LAYER_HEALTH_METHODS),
    ]
    for line in lines:
        help_sheet.append(line)
    for row in help_sheet.iter_rows():
        for cell in row:
            cell.alignment = wrap
    for number in (1, 4, 5 + len(COLUMNS) + 1):
        for cell in help_sheet[number]:
            cell.font = Font(bold=True)
    help_sheet.column_dimensions["A"].width = 18
    help_sheet.column_dimensions["B"].width = 100
    # Title and introduction span both columns (merged cells do not grow with wrapped text).
    help_sheet.merge_cells("A1:B1")
    help_sheet.merge_cells("A2:B2")
    help_sheet.row_dimensions[2].height = 48

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


@dataclass(frozen=True)
class SheetRow:
    number: int  # row number in Excel (the header is row 1)
    values: dict[str, Any]


def file_error(detail: str) -> AppError:
    return AppError(422, "INVALID_FILE", "The file cannot be imported.", errors={"file": [detail]})


def header_name(value: object) -> str:
    return str(value).strip().lower().replace(" ", "_") if value is not None else ""


def read_rows(content: bytes) -> list[SheetRow]:
    """The non-blank rows of the schedule sheet (``Vaccinations``, else the first sheet), keyed by
    column. Raises 422 INVALID_FILE when the file is not a readable .xlsx or the header row is wrong."""
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # zip, XML, or workbook errors: all mean "not an .xlsx we can read"
        raise file_error("Upload an Excel .xlsx file (download the template).") from exc
    try:
        sheet = workbook[SHEET] if SHEET in workbook.sheetnames else workbook.worksheets[0]
        lines = sheet.iter_rows(max_col=len(COLUMNS) + 10, values_only=True)
        header = [header_name(value) for value in next(lines, ())]
        while header and not header[-1]:
            header.pop()
        unknown = [name or "(empty)" for name in header if name not in COLUMNS]
        if unknown:
            raise file_error(f"Unknown columns: {', '.join(unknown)}. Use the template's header row.")
        if len(set(header)) != len(header):
            raise file_error("A column appears twice in the header row.")
        missing = [name for name in REQUIRED_COLUMNS if name not in header]
        if missing:
            raise file_error(f"Missing columns: {', '.join(missing)}.")
        rows: list[SheetRow] = []
        for number, line in enumerate(lines, start=2):
            if number > MAX_SCANNED_ROWS:
                raise file_error(f"The sheet is too long (more than {MAX_SCANNED_ROWS} rows).")
            values = {
                name: cell.strip() if isinstance(cell, str) else cell for name, cell in zip(header, line, strict=False)
            }
            if all(value is None or value == "" for value in values.values()):
                continue
            if any(cell not in (None, "") for cell in line[len(header) :]):
                raise file_error(f"Row {number} has values outside the template's columns.")
            rows.append(SheetRow(number, values))
            if len(rows) > MAX_ROWS:
                raise file_error(f"At most {MAX_ROWS} vaccinations can be imported at once.")
    finally:
        workbook.close()
    if not rows:
        raise file_error("The file has no vaccination. An empty schedule cannot be imported.")
    return rows
