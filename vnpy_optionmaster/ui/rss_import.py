"""
Read the 先物OP約定一覧 (futures/option execution list) from マーケットスピード II
RSS via an already-open Excel workbook, and parse it into structured executions.

The user opens their own Excel workbook (with the マーケットスピード II RSS add-in
loaded and MS II logged in) containing a =RssFOPExecutionList(...) sheet. vnpy
attaches to that running Excel over COM (pywin32), locates the sheet by its
header row, reads the values, and parses them. vnpy does NOT create, open, save
or modify any file — it only reads what is already on screen.
"""

import os
import re
from dataclasses import dataclass


# Header titles of the 先物OP約定一覧 (used to locate the sheet + map columns).
FOP_HEADERS: list[str] = [
    "約定日", "銘柄コード", "銘柄名称", "市場名称", "セッション区分",
    "取引", "約定数量", "約定単価", "約定代金", "手数料", "税金", "受渡金額",
]

# Columns that must be present for a row to be recognised as the header row.
_REQUIRED_HEADERS = {"取引", "約定数量", "約定単価", "銘柄名称"}

# Placeholder RSS writes into empty list slots.
_PLACEHOLDER = "--------"


@dataclass
class RssExecution:
    date: str
    code: str
    name: str
    trade: str          # 取引: 買建 / 売建 / 転売 / 買戻 ...
    qty: float          # 約定数量
    price: float        # 約定単価


def _attach_running_excel():
    """Attach to the already-running Excel (never launch a new instance)."""
    import win32com.client
    try:
        return win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        pass
    try:
        return win32com.client.GetObject(Class="Excel.Application")
    except Exception:
        return None


def workbook_status(path: str) -> tuple[bool, bool]:
    """Return (excel_running, workbook_open) for the file at `path`.

    excel_running is False if no Excel instance is reachable.
    workbook_open is True if a workbook whose full path == `path` is open.
    """
    xl = _attach_running_excel()
    if xl is None:
        return (False, False)
    target = os.path.normcase(os.path.abspath(path))
    try:
        for wb in xl.Workbooks:
            try:
                if os.path.normcase(os.path.abspath(wb.FullName)) == target:
                    return (True, True)
            except Exception:
                continue
    except Exception:
        pass
    return (True, False)


def _write_template_com(ws, settle_flag: int, account_flag: int) -> None:
    """Write the header row (A2:L2) and the RssFOPExecutionList formula (A1)."""
    for i, h in enumerate(FOP_HEADERS):
        ws.Cells(2, 1 + i).Value = h
    formula = f"=RssFOPExecutionList($A$2:$L$2, {settle_flag}, {account_flag})"
    try:
        ws.Cells(1, 1).Formula2 = formula
    except Exception:
        ws.Cells(1, 1).Formula = formula


def create_rss_file(path: str, settle_flag: int = 0, account_flag: int = 0) -> str:
    """Create the rss_fop.xlsx template (headers + RssFOPExecutionList formula).

    Prefers the user's already-running Excel (the file is created and left OPEN
    there, so the RSS add-in is loaded) → returns "excel". Otherwise launches a
    throwaway Excel just to write the file to disk and closes it → returns
    "disk" (the user must open it themselves so the add-in loads).
    """
    import win32com.client

    abspath = os.path.abspath(path)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)

    xl = _attach_running_excel()
    opened_in_user_excel = xl is not None
    launched = False
    if xl is None:
        xl = win32com.client.DispatchEx("Excel.Application")
        launched = True

    try:
        wb = xl.Workbooks.Add()
        ws = wb.Worksheets(1)
        _write_template_com(ws, settle_flag, account_flag)
        wb.SaveAs(abspath, FileFormat=51)          # 51 = .xlsx
        if opened_in_user_excel:
            try:
                xl.Visible = True
            except Exception:
                pass
            return "excel"
        wb.Close(SaveChanges=False)                 # already saved
        return "disk"
    finally:
        if launched:
            try:
                xl.Quit()
            except Exception:
                pass


def read_fop_executions(max_rows: int = 2000, scan_cols: int = 30) -> list[RssExecution]:
    """Attach to the running Excel, find the open 先物OP約定一覧 sheet, and return
    the parsed executions.

    Raises RuntimeError if Excel is not running or the sheet can't be found.
    """
    xl = _attach_running_excel()
    if xl is None:
        raise RuntimeError(
            "実行中のExcelに接続できません。\n"
            "マーケットスピードII（RSS）とExcelを起動・ログインし、\n"
            "先物OP約定一覧（RssFOPExecutionList）のシートを開いてください。"
        )

    grid = _find_exec_grid(xl, max_rows, scan_cols)
    if grid is None:
        raise RuntimeError(
            "先物OP約定一覧のシートが見つかりません。\n"
            "開いているExcelに「取引 / 約定数量 / 約定単価 / 銘柄名称」の\n"
            "見出し行がある RssFOPExecutionList のシートを用意してください。"
        )
    return _parse_block(grid)


def _find_exec_grid(xl, max_rows: int, scan_cols: int):
    """Scan every open worksheet for the execution-list header row. Returns the
    value grid starting at that header row (header row first), or None."""
    try:
        workbooks = list(xl.Workbooks)
    except Exception:
        return None

    for wb in workbooks:
        try:
            worksheets = list(wb.Worksheets)
        except Exception:
            continue
        for ws in worksheets:
            try:
                grid = ws.Range(
                    ws.Cells(1, 1), ws.Cells(max_rows, scan_cols)
                ).Value
            except Exception:
                continue
            if not grid:
                continue
            for r, row in enumerate(grid):
                if not row:
                    continue
                texts = {str(c).strip() for c in row if c is not None}
                if _REQUIRED_HEADERS.issubset(texts):
                    return grid[r:]
    return None


def _parse_block(values) -> list[RssExecution]:
    """Turn a value grid (header row first) into RssExecution rows."""
    execs: list[RssExecution] = []
    if not values:
        return execs

    header = values[0]
    idx: dict[str, int] = {}
    for ci, h in enumerate(header):
        if h is not None and str(h).strip():
            idx[str(h).strip()] = ci

    def cell(row, name: str):
        ci = idx.get(name)
        if ci is None or ci >= len(row):
            return None
        return row[ci]

    for row in values[1:]:
        if row is None:
            continue
        code = cell(row, "銘柄コード")
        code_str = "" if code is None else str(code).strip()
        if code_str in ("", _PLACEHOLDER):
            continue
        trade = cell(row, "取引")
        trade_str = "" if trade is None else str(trade).strip()
        if trade_str in ("", _PLACEHOLDER):
            continue
        execs.append(RssExecution(
            date=str(cell(row, "約定日") or "").strip(),
            code=code_str,
            name=str(cell(row, "銘柄名称") or "").strip(),
            trade=trade_str,
            qty=_to_float(cell(row, "約定数量")),
            price=_to_float(cell(row, "約定単価")),
        ))
    return execs


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------
#  Instrument-name parsing (銘柄名称 → month / call-put / strike / mini)
# --------------------------------------------------------------------------
def parse_instrument(name: str, code: str = "") -> dict | None:
    """Parse a 先物OP 銘柄名称 into a structured instrument descriptor.

    Returns a dict with keys:
        kind    : "option" | "futures"
        cp      : "C" | "P" | None
        yymm    : e.g. "2609"
        strike  : int | None
        is_mini : bool
        is_micro: bool
        raw     : the original name
    or None if the month (and, for options, the strike) can't be determined.
    """
    s = str(name or "")

    is_mini = bool(re.search(r"ﾐﾆ|ミニ|mini", s, re.IGNORECASE))
    is_micro = bool(re.search(r"ﾏｲｸﾛ|マイクロ|micro", s, re.IGNORECASE))
    # Option if the name says so (先物OP names look like "日経225オプション 26-09 P 57000").
    is_option = bool(re.search(r"オプション|ｵﾌﾟｼｮﾝ|option", s, re.IGNORECASE))

    yymm = _parse_month(s)
    if yymm is None:
        return None

    if is_option:
        # Call/Put may be written as words (コール/プット/CALL/PUT) or as a
        # standalone single letter C / P (e.g. "26-09 P 57000").
        is_call = bool(re.search(r"コール|ｺ[ｰ－\-]?ﾙ|CALL", s, re.IGNORECASE)) or \
            bool(re.search(r"(?<![A-Za-z])C(?![A-Za-z])", s))
        is_put = bool(re.search(r"プット|ﾌﾟｯﾄ|PUT", s, re.IGNORECASE)) or \
            bool(re.search(r"(?<![A-Za-z])P(?![A-Za-z])", s))
        if not (is_call or is_put):
            return None
        strike = _parse_strike(s)
        if strike is None:
            return None
        return {
            "kind": "option",
            "cp": "C" if is_call else "P",
            "yymm": yymm,
            "strike": strike,
            "is_mini": is_mini,
            "is_micro": is_micro,
            "raw": s,
        }

    return {
        "kind": "futures",
        "cp": None,
        "yymm": yymm,
        "strike": None,
        "is_mini": is_mini,
        "is_micro": is_micro,
        "raw": s,
    }


def _parse_month(s: str) -> str | None:
    """Extract a YYMM month code from names like 2026/09, 2026年9月, 26/09, 2609."""
    m = re.search(r"(20\d{2})\D{0,3}?(\d{1,2})\s*月?", s)
    if m:
        yy = int(m.group(1)) % 100
        mm = int(m.group(2))
        if 1 <= mm <= 12:
            return f"{yy:02d}{mm:02d}"
    m = re.search(r"\b(\d{2})[/\-.](\d{1,2})\b", s)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12:
            return f"{yy:02d}{mm:02d}"
    m = re.search(r"\b(\d{2})(\d{2})\b", s)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12:
            return f"{yy:02d}{mm:02d}"
    return None


def _parse_strike(s: str) -> int | None:
    """Pick the option strike: the largest plausible price (Nikkei strikes are
    multiples of 125), ignoring year-like small numbers."""
    nums = [int(n) for n in re.findall(r"\d+", s)]
    cands = [n for n in nums if n >= 10000]
    if not cands:
        return None
    grid = [n for n in cands if n % 125 == 0]
    return max(grid or cands)
