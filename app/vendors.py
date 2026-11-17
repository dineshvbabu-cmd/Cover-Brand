"""Pull vendor names / contacts / logos out of Excel, CSV, PDF, email or pasted text."""
import email
import io
import re
from email import policy
from pathlib import Path

import pandas as pd

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{6,}\d)")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s,;|]+", re.I)
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

FIELDS = [  # checked in this order; "contact no" must hit phone before contact
    ("email", ["email", "e-mail", "mail", "email id", "email address", "mail id"]),
    ("phone", ["phone", "mobile", "tel", "telephone", "contact no", "contact number", "phone no",
               "cell", "whatsapp", "mob"]),
    ("website", ["website", "web", "url", "site", "www", "web site"]),
    ("contact", ["contact person", "contact name", "attn", "attention", "person", "contact", "pic",
                 "representative"]),
    ("name", ["vendor", "vendor name", "company", "company name", "supplier", "supplier name", "name",
              "party", "party name", "client", "customer", "firm", "organisation", "organization",
              "maker", "manufacturer", "agent", "dealer"]),
]


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _field_for(header) -> str | None:
    h = _norm(header)
    if not h:
        return None
    for f, syn in FIELDS:
        if h in syn:
            return f
    for f, syn in FIELDS:
        if any(re.search(rf"\b{re.escape(s)}\b", h) for s in syn):
            return f
    return None


def _clean(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return "" if s.lower() in ("nan", "none") else s


def _vendor(name="", contact="", email_="", phone="", website="", source=""):
    return {"name": name.strip(), "contact": contact.strip(), "email": email_.strip(),
            "phone": phone.strip(), "website": website.strip(), "source": source}


def from_dataframe(df: pd.DataFrame, source: str) -> list[dict]:
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return []
    rows = df.astype(object).values.tolist()
    # find a header row within the first 15 rows
    hdr_idx, mapping = None, {}
    for i, row in enumerate(rows[:15]):
        m = {}
        for j, cell in enumerate(row):
            f = _field_for(_clean(cell))
            if f and f not in m.values():
                m[j] = f
        if "name" in m.values() and len(m) >= 1 and sum(1 for c in row if _clean(c)) >= 1:
            hdr_idx, mapping = i, m
            break
    body = rows[hdr_idx + 1:] if hdr_idx is not None else rows
    if hdr_idx is None:  # guess columns from content
        cols = list(zip(*body)) if body else []
        for j, col in enumerate(cols):
            vals = [_clean(c) for c in col if _clean(c)]
            if not vals:
                continue
            if sum(bool(EMAIL_RE.search(v)) for v in vals) > len(vals) / 2:
                mapping.setdefault(j, "email")
            elif sum(bool(PHONE_RE.fullmatch(v)) for v in vals) > len(vals) / 2:
                mapping.setdefault(j, "phone")
            elif sum(bool(URL_RE.search(v)) for v in vals) > len(vals) / 2:
                mapping.setdefault(j, "website")
        for j, col in enumerate(cols):
            if j not in mapping and any(_clean(c) for c in col):
                mapping[j] = "name"
                break
    out = []
    for row in body:
        v = _vendor(source=source)
        for j, f in mapping.items():
            if j < len(row) and not v[f]:
                v[f] = _clean(row[j])
        if v["name"] and not v["name"].replace(".", "").isdigit():
            out.append(v)
    return out


def from_text(text: str, source: str, freeform: bool) -> list[dict]:
    """Each line: 'Name, Contact, email, phone, web' in any order/delimiter.
    freeform=True (PDF/email body): only keep lines that carry an email or phone."""
    out, prev = [], ""
    for raw in text.splitlines():
        line = raw.strip().strip("•*-–").strip()
        if not line:
            prev = ""
            continue
        emails = EMAIL_RE.findall(line)
        rest = EMAIL_RE.sub(" ", line)
        urls = URL_RE.findall(rest)
        rest = URL_RE.sub(" ", rest)
        phones = [p.strip() for p in PHONE_RE.findall(rest) if sum(c.isdigit() for c in p) >= 7]
        for p in phones:
            rest = rest.replace(p, " ")
        rest = re.sub(r"\b(email|e-mail|tel|phone|mob|mobile|web|website|contact)\s*[:.]", " ", rest, flags=re.I)
        tokens = [t.strip(" :") for t in re.split(r"[,\t|;]+|\s{2,}", rest) if t.strip(" :")]
        tokens = [t for t in tokens if re.search(r"[A-Za-z]", t)]
        has_contact = bool(emails or phones)
        if freeform and not has_contact:
            prev = line if len(line) < 80 else ""
            continue
        name = tokens[0] if tokens else (prev if freeform else "")
        contact = tokens[1] if len(tokens) > 1 else ""
        if not name and emails:
            name = _company_from_email(emails[0])
        if name:
            out.append(_vendor(name, contact, emails[0] if emails else "", phones[0] if phones else "",
                               urls[0] if urls else "", source))
        prev = ""
    return out


def _company_from_email(addr: str) -> str:
    dom = addr.split("@")[-1].split(".")[0]
    if dom.lower() in {"gmail", "yahoo", "outlook", "hotmail", "live", "icloud", "rediffmail", "aol"}:
        return addr.split("@")[0].replace(".", " ").replace("_", " ").title()
    return re.sub(r"[-_]+", " ", dom).title()


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser").get_text("\n")


def _html_tables(html: str, source: str) -> list[dict]:
    try:
        tables = pd.read_html(io.StringIO(html), header=None)
    except Exception:
        return []
    out = []
    for t in tables:
        out += from_dataframe(t, source)
    return out


def from_pdf(data: bytes, source: str) -> list[dict]:
    import pdfplumber
    out, text = [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for t in page.extract_tables() or []:
                out += from_dataframe(pd.DataFrame(t), source)
            text.append(page.extract_text() or "")
    if not out:
        out = from_text("\n".join(text), source, freeform=True)
    return out


def _email_parts(data: bytes, filename: str):
    """Return (sender_name, sender_addr, body_text, body_html, [(fname, bytes)])."""
    if filename.lower().endswith(".msg"):
        import extract_msg
        m = extract_msg.Message(io.BytesIO(data))
        sender = m.sender or ""
        html = m.htmlBody.decode("utf-8", "ignore") if isinstance(m.htmlBody, bytes) else (m.htmlBody or "")
        atts = [(a.longFilename or a.shortFilename or "file", a.data) for a in m.attachments
                if isinstance(getattr(a, "data", None), bytes)]
        body = m.body or ""
    else:
        m = email.message_from_bytes(data, policy=policy.default)
        sender = str(m.get("From", ""))
        body, html, atts = "", "", []
        for part in m.walk():
            if part.is_multipart():
                continue
            fn = part.get_filename()
            ctype = part.get_content_type()
            if fn or part.get_content_disposition() in ("attachment", "inline") and not ctype.startswith("text/"):
                payload = part.get_payload(decode=True)
                if payload:
                    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}.get(ctype, "")
                    atts.append((fn or f"inline{len(atts)}{ext}", payload))
            elif ctype == "text/plain" and not body:
                body = part.get_content()
            elif ctype == "text/html" and not html:
                html = part.get_content()
    name, addr = email.utils.parseaddr(sender)
    return name, addr, body, html, atts


def parse_file(filename: str, data: bytes) -> tuple[list[dict], list[tuple[str, bytes]]]:
    """Returns (vendors, logo_images)."""
    ext = Path(filename).suffix.lower()
    src = filename
    if ext in (".xlsx", ".xlsm", ".xls"):
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None)
        return [v for df in sheets.values() for v in from_dataframe(df, src)], []
    if ext in (".csv", ".tsv"):
        df = pd.read_csv(io.BytesIO(data), header=None, sep=None, engine="python")
        return from_dataframe(df, src), []
    if ext == ".txt":
        return from_text(data.decode("utf-8", "ignore"), src, freeform=False), []
    if ext == ".pdf":
        return from_pdf(data, src), []
    if ext in IMG_EXT:
        return [], [(filename, data)]
    if ext in (".eml", ".msg"):
        sname, saddr, body, html, atts = _email_parts(data, filename)
        vendors, logos = [], []
        for fn, blob in atts:
            fext = Path(fn).suffix.lower()
            if fext in IMG_EXT:
                if len(blob) > 2500:  # skip tracking pixels / tiny icons
                    logos.append((fn, blob))
            elif fext in (".xlsx", ".xls", ".csv", ".pdf", ".txt", ".eml", ".msg"):
                v, lg = parse_file(fn, blob)
                vendors += v
                logos += lg
        if html:
            vendors += _html_tables(html, src)
        text = body or (_html_to_text(html) if html else "")
        if not vendors:
            body_v = from_text(text, src, freeform=True)
            if len(body_v) > 1:
                vendors += body_v
            elif saddr:  # the email is *from* the vendor: sender + signature
                bv = body_v[0] if body_v else {}
                phone = bv.get("phone") or next((p for p in PHONE_RE.findall(text) if sum(c.isdigit() for c in p) >= 7), "")
                url = bv.get("website") or next(iter(URL_RE.findall(text)), "")
                name = bv.get("name") or _company_from_email(saddr)
                if sname and _norm(name) == _norm(sname):  # picked the person's name, use the domain
                    name = _company_from_email(saddr)
                vendors.append(_vendor(name, bv.get("contact") or sname, bv.get("email") or saddr,
                                       phone.strip(), url, src))
            else:
                vendors += body_v
        # single vendor from an email: its attached image is their logo
        if len(vendors) == 1 and logos:
            biggest = max(logos, key=lambda x: len(x[1]))
            vendors[0]["_logo_file"] = biggest[0]
        return vendors, logos
    raise ValueError(f"Unsupported file type: {ext}")


def dedupe(existing: list[dict], new: list[dict]) -> list[dict]:
    seen = {_norm(v["name"]) for v in existing}
    out = []
    for v in new:
        k = _norm(v["name"])
        if k and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def match_logo(filename: str, vendors: list[dict]):
    from rapidfuzz import fuzz, process
    stem = _norm(Path(filename).stem.replace("logo", ""))
    if not stem or not vendors:
        return None
    names = [_norm(v["name"]) for v in vendors]
    best = process.extractOne(stem, names, scorer=fuzz.token_set_ratio)
    if best and best[1] >= 70:
        return vendors[best[2]]
    return None
