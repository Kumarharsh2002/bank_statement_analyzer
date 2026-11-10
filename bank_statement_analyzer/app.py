import re
from io import BytesIO
from collections import defaultdict

import pandas as pd
import pdfplumber
import streamlit as st

st.set_page_config(page_title="Bank Statement Analyzer", page_icon="🏦", layout="wide")

COLUMNS = [
    "Value Date", "Post Date", "Details", "Ref No/Cheque No",
    "Debit", "Credit", "Balance"
]

# The headings are the seven labels in the supplied screenshot. The parser does
# not look for a heading called "Transaction".
HEADER_TERMS = {
    "Value Date": {"value", "valuedate", "valuedt"},
    "Post Date": {"post", "posting", "postdate", "postdt"},
    "Details": {"details", "detail", "description", "particulars", "narration", "remarks"},
    "Ref No/Cheque No": {"ref", "reference", "refno", "cheque", "chequeno", "chq", "chqno"},
    "Debit": {"debit", "dr", "withdrawal", "withdrawals"},
    "Credit": {"credit", "cr", "deposit", "deposits"},
    "Balance": {"balance", "runningbalance", "closingbalance"},
}

DATE_RE = re.compile(
    r"^(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}|"
    r"\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4})$"
)


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def token(value):
    return re.sub(r"[^a-z]", "", clean(value).lower())


def parse_date(value):
    value = clean(value)
    if not value:
        return pd.NaT
    # Handles 01/02/2026, 01-Feb-2026, 01 Feb 2026 and ISO dates.
    return pd.to_datetime(value, dayfirst=True, errors="coerce")


def parse_amount(value):
    value = clean(value)
    if not value or value in {"-", "—", "–"}:
        return 0.0
    value = value.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    value = value.replace(",", "").replace(" ", "")
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("()")
    value = re.sub(r"[^0-9.\-]", "", value)
    try:
        number = float(value) if value else 0.0
        return -abs(number) if negative else number
    except ValueError:
        return 0.0


def get_words(page):
    """Read words with their PDF coordinates; no visible table borders required."""
    try:
        return page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["fontname", "size"],
        ) or []
    except TypeError:
        # Compatibility with older pdfplumber versions.
        return page.extract_words(x_tolerance=2, y_tolerance=3) or []


def group_lines(words, tolerance=3.5):
    """Group words into visual lines, keeping each word's x-coordinate."""
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        line = next(
            (item for item in lines if abs(item["top"] - word["top"]) <= tolerance),
            None,
        )
        if line is None:
            lines.append({"top": word["top"], "words": [word]})
        else:
            line["words"].append(word)
            line["top"] = sum(w["top"] for w in line["words"]) / len(line["words"])
    for line in lines:
        line["words"].sort(key=lambda w: w["x0"])
        line["text"] = " ".join(w["text"] for w in line["words"])
        line["bottom"] = max(w["bottom"] for w in line["words"])
    return sorted(lines, key=lambda item: item["top"])


def find_header_positions(words, page_height):
    """Find heading anchors, allowing words such as 'Value' and 'Date' to be split."""
    top_words = [w for w in words if w["top"] < page_height * 0.50]
    lines = group_lines(top_words, tolerance=7)
    candidates = defaultdict(list)

    # First search line-by-line so split labels such as 'Value Date' are accepted.
    for line in lines:
        line_tokens = [token(w["text"]) for w in line["words"]]
        line_text = "".join(line_tokens)
        for col, terms in HEADER_TERMS.items():
            matched = any(t in terms for t in line_tokens)
            # Also handle PDF text extraction that returns "ValueDate" as one word.
            matched = matched or any(term in line_text for term in terms if len(term) > 4)
            if matched:
                useful = [w for w, t in zip(line["words"], line_tokens) if t in terms]
                if not useful:
                    useful = line["words"]
                # Prefer the word whose token is the label, not a nearby currency symbol.
                anchor_word = useful[0]
                candidates[col].append((line["top"], (anchor_word["x0"] + anchor_word["x1"]) / 2))

    # Find the densest horizontal cluster of distinct heading labels. This is
    # safer than blindly taking the first word "Details" on the page (which can
    # occur in an account-summary heading above the actual transaction table).
    all_hits = [(col, y, x) for col, hits in candidates.items() for y, x in hits]
    if not all_hits:
        return None, None
    best_band = None
    best_score = (-1, float("inf"))
    for _, anchor_y, _ in all_hits:
        band = [(col, y, x) for col, y, x in all_hits if abs(y - anchor_y) <= 28]
        distinct = len({col for col, _, _ in band})
        # Prefer the band with most different headings; break ties by uppermost.
        score = (distinct, -anchor_y)
        if score > best_score:
            best_score = score
            best_band = band
    band_y = sum(y for _, y, _ in best_band) / len(best_band)

    positions = {}
    for col in HEADER_TERMS:
        hits = [(y, x) for found_col, y, x in best_band if found_col == col]
        if hits:
            nearest = min(hits, key=lambda item: abs(item[0] - band_y))
            positions[col] = nearest[1]

    required = ["Value Date", "Post Date", "Details", "Debit", "Credit", "Balance"]
    if not all(col in positions for col in required):
        return None, None

    # Ref No/Cheque No is expected in the screenshot, but may be split or
    # abbreviated by a bank. Keep a seven-column output even if it is absent.
    if "Ref No/Cheque No" not in positions:
        ordered_core = [positions[c] for c in ["Details", "Debit"]]
        positions["Ref No/Cheque No"] = (ordered_core[0] + ordered_core[1]) / 2

    ordered = sorted(positions.items(), key=lambda item: item[1])
    names = [name for name, _ in ordered]
    expected = COLUMNS
    if names != expected:
        # A layout with missing/misread anchors is unsafe to split by guessed order.
        return None, None

    # Estimate the bottom of the heading block from lines that actually contain
    # heading words. Do NOT take every word in a large vertical band: that can
    # accidentally include and discard the first transaction row.
    heading_top = min(y for _, y, _ in best_band)
    header_line_bottoms = []
    for line in lines:
        if not (heading_top - 5 <= line["top"] <= heading_top + 42):
            continue
        line_tokens = [token(w["text"]) for w in line["words"]]
        is_heading_line = any(
            word_token in accepted
            for word_token in line_tokens
            for accepted in HEADER_TERMS.values()
        )
        # A standalone "No" can be the second/third wrapped line of Ref No /
        # Cheque No. Include it only when it sits inside the reference column.
        if not is_heading_line and "no" in line_tokens and "Ref No/Cheque No" in positions:
            ref_x = positions["Ref No/Cheque No"]
            is_heading_line = any(w["x0"] <= ref_x <= w["x1"] for w in line["words"])
        if is_heading_line:
            header_line_bottoms.append(line["bottom"])
    header_bottom = max(header_line_bottoms, default=heading_top + 18)
    return positions, header_bottom


def build_boundaries(positions, page_width):
    ordered = sorted(positions.items(), key=lambda item: item[1])
    names = [name for name, _ in ordered]
    centers = [x for _, x in ordered]
    boundaries = [0.0]
    boundaries.extend((centers[i] + centers[i + 1]) / 2 for i in range(len(centers) - 1))
    boundaries.append(float(page_width) + 1)
    return names, boundaries


def cell_for_x(x, names, boundaries):
    for index in range(len(names)):
        if boundaries[index] <= x < boundaries[index + 1]:
            return names[index]
    return None


def repair_joined_dates(record):
    """Split adjacent date strings when a PDF has no gap between date columns."""
    date_piece = re.compile(
        r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|"
        r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
        r"\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}|"
        r"\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"
    )
    value = clean(record.get("Value Date"))
    post = clean(record.get("Post Date"))
    for field in ("Value Date", "Post Date"):
        candidate = clean(record.get(field))
        pieces = list(date_piece.finditer(candidate))
        if len(pieces) >= 2:
            record["Value Date"] = pieces[0].group(0)
            record["Post Date"] = pieces[1].group(0)
            return record
    # If the PDF extraction joined the first two date columns into one token,
    # its x-centre may place the combined token entirely in just one column.
    combined = value + post
    pieces = list(date_piece.finditer(combined))
    if len(pieces) >= 2:
        record["Value Date"] = pieces[0].group(0)
        record["Post Date"] = pieces[1].group(0)
    return record


def row_has_date(record):
    repair_joined_dates(record)
    return bool(DATE_RE.match(clean(record.get("Value Date"))) or
                DATE_RE.match(clean(record.get("Post Date"))))


def extract_page_by_coordinates(page, page_no, carried_positions=None):
    words = get_words(page)
    if not words:
        return [], carried_positions, "No selectable PDF text"

    positions, header_bottom = find_header_positions(words, page.height)
    if positions is None:
        positions = carried_positions
        # On continuation pages the header may not repeat; reuse previous page's
        # x positions and begin close to the top margin.
        body_start = page.height * 0.045
    else:
        body_start = header_bottom + 2

    if not positions:
        return [], carried_positions, "Could not locate the seven column headings"

    names, boundaries = build_boundaries(positions, page.width)
    body_words = [w for w in words if w["top"] >= body_start]
    lines = group_lines(body_words, tolerance=3.5)

    records = []
    for line in lines:
        record = {col: "" for col in COLUMNS}
        parts = defaultdict(list)
        for word in line["words"]:
            x = (word["x0"] + word["x1"]) / 2
            col = cell_for_x(x, names, boundaries)
            if col:
                parts[col].append(word["text"])
        for col in COLUMNS:
            record[col] = clean(" ".join(parts[col]))

        if row_has_date(record):
            record["_page"] = page_no
            records.append(record)
        elif records:
            # Multi-line descriptions/reference numbers are common. Append
            # continuation text to Details rather than treating it as a new row.
            continuation = clean(" ".join(w["text"] for w in line["words"]))
            if continuation:
                records[-1]["Details"] = clean(records[-1]["Details"] + " " + continuation)

    return records, positions, f"Coordinate extraction: {len(records)} rows"


def extract_page_by_tables(page, page_no):
    """Fallback for PDFs where pdfplumber can detect table cells directly."""
    settings_list = [
        {},
        {"vertical_strategy": "text", "horizontal_strategy": "text",
         "min_words_vertical": 1, "min_words_horizontal": 1,
         "text_x_tolerance": 2, "text_y_tolerance": 3},
        {"vertical_strategy": "lines", "horizontal_strategy": "text",
         "min_words_horizontal": 1, "text_x_tolerance": 2, "text_y_tolerance": 3},
    ]
    for settings in settings_list:
        try:
            tables = page.extract_tables(table_settings=settings) or []
        except Exception:
            continue
        for table in tables:
            if not table:
                continue
            header_index = None
            for i, row in enumerate(table[:12]):
                joined = " ".join(token(cell) for cell in (row or []))
                has_value = "value" in joined or "valuedate" in joined
                has_post = "post" in joined or "posting" in joined
                has_details = any(x in joined for x in ("details", "description", "particulars", "narration"))
                has_debit = any(x in joined for x in ("debit", "withdrawal"))
                has_credit = any(x in joined for x in ("credit", "deposit"))
                has_balance = "balance" in joined
                if has_value and has_post and has_details and has_debit and has_credit and has_balance:
                    header_index = i
                    break
            if header_index is None:
                continue

            output = []
            for data_row in table[header_index + 1:]:
                values = [clean(x) for x in (data_row or [])]
                if not any(values):
                    continue
                # Normalize cell count to the seven supplied columns.
                if len(values) >= 7:
                    values = values[:7]
                elif len(values) == 6:
                    values = values[:3] + [""] + values[3:]
                else:
                    continue
                record = dict(zip(COLUMNS, values))
                if row_has_date(record):
                    record["_page"] = page_no
                    output.append(record)
            if output:
                return output
    return []


def extract_pdf(pdf_bytes):
    records, diagnostics = [], []
    carried_positions = None
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            page_records = extract_page_by_tables(page, page_no)
            method = "table extraction"
            if not page_records:
                page_records, new_positions, note = extract_page_by_coordinates(
                    page, page_no, carried_positions
                )
                if new_positions:
                    carried_positions = new_positions
                method = note
            if page_records:
                records.extend(page_records)
                diagnostics.append(f"Page {page_no}: {len(page_records)} rows ({method}).")
            else:
                page_text = page.extract_text() or ""
                diagnostics.append(
                    f"Page {page_no}: 0 rows. Selectable text: "
                    f"{'yes' if page_text.strip() else 'no'}; text length: {len(page_text)}. {method}."
                )
    return records, diagnostics


def make_dataframe(records):
    rows = []
    for raw in records:
        row = {col: raw.get(col, "") for col in COLUMNS}
        row["Value Date"] = parse_date(row["Value Date"])
        row["Post Date"] = parse_date(row["Post Date"])
        row["Debit"] = abs(parse_amount(row["Debit"]))
        row["Credit"] = abs(parse_amount(row["Credit"]))
        row["Balance"] = parse_amount(row["Balance"])
        row["Transaction Type"] = (
            "Debit" if row["Debit"] > 0 else
            "Credit" if row["Credit"] > 0 else "Unclassified"
        )
        row["Amount"] = row["Debit"] if row["Debit"] > 0 else row["Credit"]
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        # Retain rows where at least one date and some transaction content exist.
        valid_date = df["Value Date"].notna() | df["Post Date"].notna()
        useful = (
            df["Details"].astype(str).str.strip().ne("") |
            (df["Debit"] > 0) | (df["Credit"] > 0) |
            df["Ref No/Cheque No"].astype(str).str.strip().ne("")
        )
        df = df[valid_date & useful].reset_index(drop=True)
    return df


st.title("🏦 Bank Statement Analyzer")
st.write("Upload a bank statement PDF and search the **Details** column for a person's name.")
st.caption("Value Date | Post Date | Details | Ref No/Cheque No | ₹ Debit | ₹ Credit | Balance")

with st.sidebar:
    st.subheader("How it works")
    st.write("The extractor uses PDF table detection and text coordinates. It does not require a column named 'Transaction'.")
    st.info("Text-based PDFs are supported. Scanned/image-only PDFs require OCR.")

uploaded = st.file_uploader("Upload bank statement PDF", type=["pdf"])
if uploaded:
    with st.spinner("Extracting rows from your PDF..."):
        raw_records, diagnostics = extract_pdf(uploaded.getvalue())
        df = make_dataframe(raw_records)

    with st.expander("Extraction diagnostics", expanded=df.empty):
        for message in diagnostics:
            st.write("• " + message)

    if df.empty:
        st.error("No rows could be extracted from this PDF.")
        st.warning(
            "If the diagnostic reports no selectable text, the PDF is scanned and needs OCR. "
            "If selectable text exists, the bank's exact PDF layout may need a custom column mapping."
        )
        with st.expander("Preview PDF text (first two pages)", expanded=True):
            with pdfplumber.open(BytesIO(uploaded.getvalue())) as pdf:
                for page_no, page in enumerate(pdf.pages[:2], start=1):
                    st.write(f"Page {page_no}")
                    text = page.extract_text(layout=True) or page.extract_text()
                    if text:
                        st.code(text[:7000])
                    else:
                        st.write("No selectable text found on this page.")
    else:
        st.success(f"Extracted {len(df):,} rows.")
        person = st.text_input("Search person's name in Details", placeholder="e.g. Rahul Sharma")
        matches = df.copy()
        if person.strip():
            matches = matches[matches["Details"].astype(str).str.contains(
                re.escape(person.strip()), case=False, na=False
            )]

        kind = st.selectbox("Transaction type", ["All", "Debit", "Credit", "Unclassified"])
        if kind != "All":
            matches = matches[matches["Transaction Type"] == kind]

        a, b, c = st.columns(3)
        a.metric("Matching transactions", f"{len(matches):,}")
        b.metric("Total debit", f"₹{matches['Debit'].sum():,.2f}")
        c.metric("Total credit", f"₹{matches['Credit'].sum():,.2f}")

        output = matches[COLUMNS + ["Transaction Type", "Amount"]].copy()
        for col in ["Value Date", "Post Date"]:
            output[col] = output[col].dt.strftime("%d-%m-%Y")

        st.subheader("Matching transactions")
        st.dataframe(output, use_container_width=True, hide_index=True)
        st.download_button(
            "Download CSV", output.to_csv(index=False).encode("utf-8-sig"),
            "person_transactions.csv", "text/csv"
        )
        excel = BytesIO()
        with pd.ExcelWriter(excel, engine="openpyxl") as writer:
            output.to_excel(writer, index=False, sheet_name="Transactions")
            pd.DataFrame({
                "Metric": ["Rows", "Total Debit", "Total Credit", "Net (Credit - Debit)"],
                "Value": [len(matches), matches["Debit"].sum(), matches["Credit"].sum(),
                          matches["Credit"].sum() - matches["Debit"].sum()],
            }).to_excel(writer, index=False, sheet_name="Summary")
        st.download_button(
            "Download Excel", excel.getvalue(), "person_transactions.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
else:
    st.info("Upload a PDF to begin.")
