from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML_NAME = "Findchips_Purchasing_Matcher.html"
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "*")


def clean(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def mpn_key(value):
    """Normalize an MPN only for exact-match comparison."""
    return re.sub(r"[^A-Z0-9]", "", clean(value).upper())


def int_value(value):
    """Mirror Power Query fxInteger: keep every digit and '-' after removing commas."""
    text = clean(value)
    if not text:
        return None
    digits = "".join(ch for ch in text.replace(",", "") if ch.isdigit() or ch == "-")
    if not digits or digits == "-":
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def stock_qty(value):
    """Parse the quantity shown in Findchips' Stock column.

    The visible Stock cell is the primary source.  This parser accepts the
    layouts seen on Findchips, for example::

        9,845
        Americas - 48,000
        Asia - 222,000 Limited Supply - Call TTI
        Global - 1,189
        0

    It intentionally reads only the first stock quantity and ignores later
    packaging / status numbers that can appear in the same rendered cell.
    """
    text = clean(value)
    if not text:
        return None

    # Normalise unicode dash variants sometimes emitted by rendered HTML.
    text = (
        text.replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
    )

    # Findchips may prefix the quantity with a region, e.g.
    # "Americas - 48000" / "Asia - 222000 Limited Supply".
    # Strip the prefix only when the left-hand side has no digits so an MPN or
    # another numeric token cannot accidentally be treated as a region.
    if " - " in text:
        left, right = text.split(" - ", 1)
        if left and not any(ch.isdigit() for ch in left):
            text = clean(right)

    # Prefer an explicitly labelled stock/availability quantity when present.
    labelled = re.search(
        r"(?i)\b(?:stock|inventory|available|availability|qty)\s*[:=]?\s*(\d[\d,]*)",
        text,
    )
    if labelled:
        return int(labelled.group(1).replace(",", ""))

    # Otherwise use the first integer token.  Comma-grouped values are kept as
    # one number, so "9,845 1 Bulk" correctly returns 9845, not 1.
    match = re.search(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?![\d.])", text)
    if not match:
        return None

    digits = match.group(1).replace(",", "")
    return int(digits) if digits else None


def region_from_stock(value):
    text = clean(value)
    return clean(text.split(" - ", 1)[0]) if " - " in text else ""


def distributor_name(value):
    text = clean(value)
    text = re.sub(r"\s+ECIA(?:\s*\([^)]*\))?.*$", "", text, flags=re.I)
    text = re.sub(r"\s+Authorized Distributor.*$", "", text, flags=re.I)
    text = re.sub(r"\s+Independent Distributor.*$", "", text, flags=re.I)
    return re.sub(r"\s*[•|]\s*$", "", text).strip()


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag = tag.lower()
        self.attrs = dict(attrs or [])
        self.children = []
        self.parent = parent

    def text(self):
        out = []
        stack = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                out.append(item)
            else:
                stack.extend(reversed(item.children))
        return clean(" ".join(out))

    def descendants(self, pred=None):
        found = []
        stack = list(reversed(self.children))
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                continue
            if pred is None or pred(item):
                found.append(item)
            stack.extend(reversed(item.children))
        return found

    def classes(self):
        return set(clean(self.attrs.get("class", "")).split())


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.lower() and tag.lower() not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def first_desc(node, pred):
    vals = node.descendants(pred)
    return vals[0] if vals else None


def direct_cells(row):
    return [x for x in row.children if isinstance(x, Node) and x.tag == "td"]


def node_by_class(node, class_name):
    return first_desc(node, lambda x: class_name in x.classes())


def data_title_value(node, candidates):
    wanted = {re.sub(r"[^a-z0-9]", "", x.lower()) for x in candidates}
    for el in node.descendants(lambda x: "data-title" in x.attrs):
        cur = re.sub(r"[^a-z0-9]", "", clean(el.attrs.get("data-title")).lower())
        if cur in wanted:
            return el.text()
    return ""


def _has_ancestor(node, predicate, stop=None):
    cur = node.parent
    while cur is not None and cur is not stop:
        if predicate(cur):
            return True
        cur = cur.parent
    return False


def _nearest_ancestor(node, predicate, stop=None):
    cur = node.parent
    while cur is not None and cur is not stop:
        if predicate(cur):
            return cur
        cur = cur.parent
    return None


def _is_hidden(node, stop=None):
    """Best-effort DOM visibility check without relying on CSS execution."""
    cur = node
    hidden_classes = {
        "hidden", "hide", "d-none", "is-hidden", "u-hidden", "ng-hide",
        "display-none", "displaynone", "visually-hidden"
    }
    while cur is not None:
        attrs = cur.attrs
        if "hidden" in attrs:
            return True
        if clean(attrs.get("aria-hidden", "")).lower() == "true":
            return True
        style = clean(attrs.get("style", "")).lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        classes = {c.lower() for c in cur.classes()}
        if classes & hidden_classes:
            return True
        if cur is stop:
            break
        cur = cur.parent
    return False


def _table_rows(table):
    """Return only TRs that belong to this table, excluding nested-table TRs."""
    rows = []
    for tr in table.descendants(lambda x: x.tag == "tr"):
        nearest_table = _nearest_ancestor(tr, lambda x: x.tag == "table")
        if nearest_table is table:
            rows.append(tr)
    return rows


def _is_offer_table(table):
    """Identify the visible Findchips offer table by its own header row."""
    for tr in _table_rows(table):
        cells = [x for x in tr.children if isinstance(x, Node) and x.tag in {"th", "td"}]
        if not cells:
            continue
        names = {re.sub(r"[^a-z0-9]", "", clean(c.text()).lower()) for c in cells}
        has_part = any(n in names for n in {"part", "partnumber", "partno", "partnum"})
        has_mfr = "manufacturer" in names
        has_stock = "stock" in names
        if has_part and has_mfr and has_stock:
            return True
    return False


def _row_has_offer_cells(tr):
    cells = direct_cells(tr)
    if len(cells) < 4:
        return False
    has_stock = any("td-stock" in td.classes() or clean(td.attrs.get("data-title", "")).lower() == "stock" for td in cells)
    has_part = any("td-part" in td.classes() or "part-number" in td.classes() for td in cells)
    return has_stock and (has_part or "data-mfrpartnumber" in tr.attrs)


def _matches_powerbi_row_selector(tr, block):
    """Mirror: tbody tr.row, tbody tr[data-mfrpartnumber], tr.row[data-mfrpartnumber]."""
    if tr.tag != "tr":
        return False
    classes = tr.classes()
    has_attr = "data-mfrpartnumber" in tr.attrs
    in_tbody = _has_ancestor(tr, lambda x: x.tag == "tbody", stop=block)
    return (in_tbody and "row" in classes) or (in_tbody and has_attr) or ("row" in classes and has_attr)


def _first_direct_td_matching(cells, predicate):
    for td in cells:
        if predicate(td):
            return td
    return None


def _first_anchor(node, allowed_classes=None):
    if node is None:
        return None
    for a in node.descendants(lambda x: x.tag == "a"):
        if allowed_classes is None or (a.classes() & allowed_classes):
            return a
    return None


def _parse_offer_row(tr, block_dist, input_mpn):
    """Apply the same field priority as fxParseDistributor in Power Query."""
    cells = direct_cells(tr)

    # MPN selector: td.td-part a, td.part-number a, td:first-child a
    part_cell = _first_direct_td_matching(
        cells,
        lambda td: "td-part" in td.classes() or "part-number" in td.classes(),
    )
    if part_cell is None and cells:
        part_cell = cells[0]
    mpn_visible = ""
    if part_cell is not None:
        a = _first_anchor(part_cell)
        mpn_visible = clean(a.text() if a else "")
    mpn_attr = clean(tr.attrs.get("data-mfrpartnumber", ""))
    mpn = mpn_attr or mpn_visible

    # Manufacturer selector: data-title='Manufacturer', nth-of-type(2), legacy classes.
    manufacturer_visible = ""
    mf = _first_direct_td_matching(
        cells,
        lambda td: clean(td.attrs.get("data-title", "")).lower() == "manufacturer",
    )
    if mf is None and len(cells) >= 2:
        mf = cells[1]
    if mf is None:
        mf = _first_direct_td_matching(
            cells,
            lambda td: "td-mfr" in td.classes() or "manufacturer" in td.classes(),
        )
    if mf is not None:
        manufacturer_visible = clean(mf.text())
    manufacturer_attr = clean(tr.attrs.get("data-mfr", ""))
    manufacturer = manufacturer_attr or manufacturer_visible

    desc_cell = _first_direct_td_matching(cells, lambda td: "td-desc" in td.classes())
    description = ""
    if desc_cell is not None:
        inner = node_by_class(desc_cell, "td-description")
        description = clean((inner or desc_cell).text())

    stock_cell = _first_direct_td_matching(
        cells,
        lambda td: (
            "td-stock" in td.classes()
            or clean(td.attrs.get("data-title", "")).lower() == "stock"
        ),
    )
    stock_display = clean(stock_cell.text() if stock_cell else "")

    # IMPORTANT: use the value displayed in the Stock column first.
    # Browser-rendered Findchips pages can contain a data-stock attribute that
    # is stale, templated, or differs from the number currently shown to the
    # user.  Power BI/browser output is expected to match the visible Stock
    # column, so attributes are fallbacks only.
    stock_visible = stock_qty(stock_display)

    stock_attr_candidates = [tr.attrs.get("data-stock", "")]
    if stock_cell is not None:
        stock_attr_candidates.extend([
            stock_cell.attrs.get("data-stock", ""),
            stock_cell.attrs.get("data-qty", ""),
            stock_cell.attrs.get("data-inventory", ""),
            stock_cell.attrs.get("data-available", ""),
        ])
        for node in stock_cell.descendants():
            stock_attr_candidates.extend([
                node.attrs.get("data-stock", ""),
                node.attrs.get("data-qty", ""),
                node.attrs.get("data-inventory", ""),
                node.attrs.get("data-available", ""),
            ])

    stock_attr = next(
        (qty for qty in (stock_qty(v) for v in stock_attr_candidates) if qty is not None),
        None,
    )
    stock = stock_visible if stock_visible is not None else stock_attr

    price_cell = _first_direct_td_matching(
        cells,
        lambda td: "td-price-range" in td.classes() or "td-price" in td.classes(),
    )

    buy_cell = _first_direct_td_matching(cells, lambda td: "td-buy" in td.classes())
    buy = ""
    if buy_cell is not None:
        a = _first_anchor(buy_cell, {"buy-button", "rfq-button"})
        if a is not None:
            buy = clean(a.text())

    row = {
        "MPN_XP": input_mpn,
        "Authorized Distributor": clean(block_dist),
        "MPN_Distributor": clean(mpn),
        "Manufacturer": clean(manufacturer),
        "Stock Qty": stock,
        "Region": region_from_stock(stock_display),
        "MOQ": int_value(data_title_value(desc_cell, ["Min Qty"])) if desc_cell else None,
        "Package Multiple": int_value(data_title_value(desc_cell, ["Package Mult."])) if desc_cell else None,
        "Lead Time": clean(data_title_value(desc_cell, ["Lead time"])) if desc_cell else "",
        "Date Code": clean(data_title_value(desc_cell, ["Date Code"])) if desc_cell else "",
        "Price Range": clean(price_cell.text() if price_cell else ""),
        "Description": description,
        "Buy": buy,
    }

    # Power BI RealRows: MPN <> null OR Stock Qty <> null OR Description <> null.
    if row["MPN_Distributor"] or row["Stock Qty"] is not None or row["Description"]:
        return row
    return None


def _block_distributor(block):
    """Extract one distributor name from the nearest distributor-results block."""
    dist_node = node_by_class(block, "distributor-name")
    if dist_node is None:
        header = node_by_class(block, "distributor-header")
        if header is not None:
            dist_node = (
                first_desc(header, lambda x: x.tag == "h2")
                or first_desc(header, lambda x: x.tag == "h3")
            )
    if dist_node is None:
        dist_node = first_desc(block, lambda x: x.tag in {"h2", "h3"})
    return distributor_name(dist_node.text() if dist_node else "")


def parse_findchips_html(html, input_mpn):
    """Extract every Findchips Authorized Distributor offer row.

    The important difference from the earlier implementation is that rows are
    scanned globally and then attached to their *nearest* distributor-results
    block.  We do not discard a row because it is collapsed/hidden, and we do
    not keep only the first/top-level distributor block.  This mirrors the
    Power BI Html.Table behaviour much more closely and prevents 28 offer rows
    from collapsing to only a handful of distributors.
    """
    parser = TreeParser()
    parser.feed(html)

    blocks = parser.root.descendants(
        lambda x: x.tag == "div" and "distributor-results" in x.classes()
    )
    if not blocks:
        return []

    # Cache each block's authorization state/name because the same block may
    # contain many offer rows.
    block_info = {}
    authorized_names = []
    for block in blocks:
        txt = block.text()
        is_authorized = "authorized distributor" in txt.lower()
        name = _block_distributor(block)
        block_info[id(block)] = (is_authorized, name)
        if is_authorized and name:
            authorized_names.append(name)

    def name_is_authorized(name):
        d = clean(name).upper()
        if not d:
            return False
        for known in authorized_names:
            k = clean(known).upper()
            if d == k or d in k or k in d:
                return True
        return False

    # Scan each TR once.  A TR can sit under nested distributor-results DIVs;
    # the nearest ancestor is the actual distributor block for that offer.
    all_rows = parser.root.descendants(lambda x: x.tag == "tr")
    output = []
    seen_nodes = set()

    for tr in all_rows:
        nearest_block = _nearest_ancestor(
            tr,
            lambda x: x.tag == "div" and "distributor-results" in x.classes(),
        )
        if nearest_block is None:
            continue
        if id(tr) in seen_nodes:
            continue

        # Exact Power BI row-selector semantics, evaluated relative to the
        # nearest distributor block.
        if not _matches_powerbi_row_selector(tr, nearest_block):
            continue

        is_authorized, block_dist = block_info.get(
            id(nearest_block),
            (False, ""),
        )
        row_dist = clean(tr.attrs.get("data-distributor_name", ""))

        # Main branch: the nearest block explicitly says Authorized Distributor.
        # Fallback: row-level distributor name matches an authorized block name.
        if not is_authorized:
            if not name_is_authorized(row_dist):
                continue
            block_dist = row_dist

        # IMPORTANT: do not filter with _is_hidden(tr).  Findchips can keep
        # additional offers in collapsed rows and Power BI still reads them.
        row = _parse_offer_row(tr, block_dist or row_dist, input_mpn)
        if row is not None:
            output.append(row)
            seen_nodes.add(id(tr))

    return output


def find_browser():
    env = os.environ.get("FINDCHIPS_BROWSER")
    candidates = [env] if env else []
    for name in ("msedge", "chrome", "chrome.exe", "msedge.exe", "chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            candidates.append(path)
    if sys.platform.startswith("win"):
        pf = [os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")]
        for base in filter(None, pf):
            candidates += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


BROWSER = find_browser()


def _playwright_dom(url):
    """Render Findchips completely on a slower cloud worker.

    Findchips progressively/lazily exposes distributor sections.  A cloud
    browser can reach the bottom before late distributor XHR/DOM work finishes,
    leaving only the first handful of offer rows.  This implementation performs
    repeated top-to-bottom passes and requires the DOM row/distributor counts to
    remain stable before capture.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError("Playwright is not installed") from exc

    row_selector = (
        "div.distributor-results tbody tr.row,"
        "div.distributor-results tbody tr[data-mfrpartnumber],"
        "div.distributor-results tr.row[data-mfrpartnumber]"
    )

    scroll_wait_ms = int(os.environ.get("FINDCHIPS_SCROLL_WAIT_MS", "900"))
    bottom_wait_ms = int(os.environ.get("FINDCHIPS_BOTTOM_WAIT_MS", "1800"))
    settle_wait_ms = int(os.environ.get("FINDCHIPS_SETTLE_WAIT_MS", "5000"))
    max_passes = int(os.environ.get("FINDCHIPS_MAX_SCROLL_PASSES", "4"))

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": [
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if not sys.platform.startswith("win"):
            launch_kwargs["args"].append("--no-sandbox")
        if BROWSER:
            launch_kwargs["executable_path"] = BROWSER

        browser = p.chromium.launch(**launch_kwargs)
        try:
            # Singapore/APAC browser context.  Render is also deployed in the
            # Singapore region in render.yaml so both outbound IP and browser
            # context are much closer to the Vietnam/local behaviour.
            context = browser.new_context(
                viewport={"width": 1920, "height": 1200},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/153.0.0.0 Safari/537.36"
                ),
                locale="en-SG",
                timezone_id="Asia/Singapore",
                geolocation={"latitude": 1.3521, "longitude": 103.8198},
                permissions=["geolocation"],
                extra_http_headers={
                    "Accept-Language": "en-SG,en;q=0.9,en-US;q=0.8"
                },
            )

            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("div.distributor-results", timeout=45000)

            # Do not begin scrolling as soon as the first distributor appears.
            # Cloud Chromium is slower than a desktop browser and Findchips can
            # still be bootstrapping distributor requests at this point.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(3500)

            state_js = f"""
                () => ({{
                    y: window.scrollY,
                    inner: window.innerHeight,
                    height: Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight
                    ),
                    blocks: document.querySelectorAll(
                        'div.distributor-results'
                    ).length,
                    rows: document.querySelectorAll(
                        {row_selector!r}
                    ).length
                }})
            """

            def get_state():
                return page.evaluate(state_js)

            best_rows = 0
            best_blocks = 0
            no_growth_passes = 0

            for pass_no in range(max_passes):
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(800)

                stable_bottom_ticks = 0
                previous_bottom_signature = None

                for _ in range(160):
                    state = get_state()
                    best_rows = max(best_rows, int(state["rows"]))
                    best_blocks = max(best_blocks, int(state["blocks"]))

                    at_bottom = (
                        state["y"] + state["inner"] >= state["height"] - 12
                    )

                    if not at_bottom:
                        page.evaluate(
                            """
                            () => window.scrollBy(
                                0,
                                Math.max(
                                    650,
                                    Math.floor(window.innerHeight * 0.72)
                                )
                            )
                            """
                        )
                        page.wait_for_timeout(scroll_wait_ms)
                        continue

                    # At the bottom: keep firing scroll events and waiting.
                    # Findchips can append a new distributor block several
                    # seconds after the viewport first reaches the bottom.
                    page.evaluate(
                        """
                        () => {
                            window.dispatchEvent(new Event('scroll'));
                            window.scrollTo(
                                0,
                                Math.max(
                                    document.body.scrollHeight,
                                    document.documentElement.scrollHeight
                                )
                            );
                        }
                        """
                    )
                    page.wait_for_timeout(bottom_wait_ms)

                    after = get_state()
                    signature = (
                        int(after["height"]),
                        int(after["blocks"]),
                        int(after["rows"]),
                    )

                    if signature == previous_bottom_signature:
                        stable_bottom_ticks += 1
                    else:
                        stable_bottom_ticks = 0
                        previous_bottom_signature = signature

                    best_rows = max(best_rows, int(after["rows"]))
                    best_blocks = max(best_blocks, int(after["blocks"]))

                    # Require a long stable period, not the old ~2.5 seconds.
                    if stable_bottom_ticks >= 5:
                        break

                # Give late XHR/DOM work time to append more distributor groups.
                page.wait_for_timeout(settle_wait_ms)
                after_pass = get_state()

                grew = (
                    int(after_pass["rows"]) > best_rows
                    or int(after_pass["blocks"]) > best_blocks
                )
                best_rows = max(best_rows, int(after_pass["rows"]))
                best_blocks = max(best_blocks, int(after_pass["blocks"]))

                if grew:
                    no_growth_passes = 0
                else:
                    no_growth_passes += 1

                print(
                    f"[browser] pass={pass_no + 1} "
                    f"blocks={after_pass['blocks']} "
                    f"rows={after_pass['rows']} "
                    f"height={after_pass['height']}",
                    flush=True,
                )

                # Two complete passes with no growth is a much stronger signal
                # than just reaching the bottom once.
                if pass_no >= 1 and no_growth_passes >= 2:
                    break

            # Touch every currently known distributor block once. This triggers
            # any IntersectionObserver attached to individual blocks.
            try:
                blocks = page.locator("div.distributor-results")
                count = blocks.count()
                for i in range(count):
                    blocks.nth(i).scroll_into_view_if_needed(timeout=3000)
                    page.wait_for_timeout(180)
            except Exception:
                pass

            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(settle_wait_ms)

            final_state = get_state()
            print(
                f"[browser] final blocks={final_state['blocks']} "
                f"rows={final_state['rows']}",
                flush=True,
            )

            html = page.content()
            if "<html" not in html.lower():
                raise RuntimeError("Playwright returned no HTML")
            return html
        finally:
            browser.close()


def _dump_dom(url):
    """Dependency-free browser fallback when Playwright is unavailable."""
    if not BROWSER:
        raise RuntimeError("Chrome/Edge was not found. Install Chrome or Edge, or set FINDCHIPS_BROWSER.")
    with tempfile.TemporaryDirectory(prefix="findchips-browser-") as profile:
        cmd = [
            BROWSER,
            "--headless=new",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-blink-features=AutomationControlled",
            "--window-size=1920,30000",
            "--run-all-compositor-stages-before-draw",
            f"--user-data-dir={profile}",
            "--virtual-time-budget=35000",
            "--dump-dom",
            url,
        ]
        if not sys.platform.startswith("win"):
            cmd.insert(2, "--no-sandbox")
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=55,
        )
        html = proc.stdout
        if "<html" not in html.lower():
            err = clean(proc.stderr)[-500:]
            raise RuntimeError("Browser returned no HTML" + (f": {err}" if err else ""))
        return html


def browser_dom(url):
    errors = []
    for renderer in (_playwright_dom, _dump_dom):
        try:
            return renderer(url)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors[-2:]))


def urllib_dom(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _candidate_score(rows):
    """Prefer the source that contains the most complete offer table."""
    distributors = {
        clean(r.get("Authorized Distributor", "")).upper()
        for r in rows
        if clean(r.get("Authorized Distributor", ""))
    }
    stock_count = sum(r.get("Stock Qty") is not None for r in rows)
    mpn_count = sum(bool(clean(r.get("MPN_Distributor", ""))) for r in rows)
    # Row count dominates; the other values break ties in favour of the more
    # complete DOM rather than an incomplete first-screen capture.
    return (len(rows), len(distributors), stock_count, mpn_count)


def fetch_mpn(mpn):
    url = "https://www.findchips.com/search/" + urllib.parse.quote(mpn, safe="")
    errors = []
    candidates = []

    # Do not return after the first non-empty result.  The browser can be
    # partially lazy-loaded while the direct response can sometimes contain
    # more rows (or vice versa).  Parse both and choose the fuller table.
    for source_name, fetcher in (("browser", browser_dom), ("direct", urllib_dom)):
        try:
            html = fetcher(url)
            rows = parse_findchips_html(html, mpn)
            if rows:
                dist_count = len({
                    clean(r.get("Authorized Distributor", "")).upper()
                    for r in rows
                    if clean(r.get("Authorized Distributor", ""))
                })
                print(
                    f"[fetch] {mpn} source={source_name} "
                    f"rows={len(rows)} distributors={dist_count}",
                    flush=True,
                )
                candidates.append((rows, source_name))
            else:
                errors.append(f"{source_name}: no Authorized Distributor rows found")
        except Exception as exc:
            errors.append(f"{source_name}: {exc}")

    if candidates:
        rows, source_name = max(candidates, key=lambda item: _candidate_score(item[0]))
        print(
            f"[fetch] {mpn} selected={source_name} rows={len(rows)}",
            flush=True,
        )
        return rows, None

    return [], "; ".join(errors[-3:])


class Handler(BaseHTTPRequestHandler):
    server_version = "FindchipsLocal/4.0-PBI-FullRows"

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self.send_json({"app": "findchips-offers", "version": "4.0-pbi-fullrows", "browser": bool(BROWSER)})
            return
        if path in ("/", "/index.html", f"/{HTML_NAME}"):
            file = ROOT / HTML_NAME
            if not file.exists():
                self.send_error(404, f"{HTML_NAME} not found")
                return
            raw = file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_error(404)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/search":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise ValueError("Request is too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            mpns = payload.get("mpns") or []
            if not isinstance(mpns, list) or not mpns:
                raise ValueError("mpns must be a non-empty array")
            if len(mpns) > 10:
                raise ValueError("Send at most 10 MPN per API request")
            results = []
            for value in mpns:
                mpn = clean(value)
                if not mpn:
                    continue
                rows, error = fetch_mpn(mpn)
                item = {"inputMPN": mpn, "rows": rows}
                if error:
                    item["error"] = error
                results.append(item)
            self.send_json({"results": results})
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)


def main():
    if not (ROOT / HTML_NAME).exists():
        print(f"Missing {HTML_NAME} in {ROOT}")
        raise SystemExit(1)
    if not BROWSER:
        print("Warning: Chrome/Edge not found. The app will try direct HTTP as fallback.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"Findchips Power BI Matcher: {url}")
    print("Press Ctrl+C to stop.")
    no_open = os.environ.get(
        "FINDCHIPS_NO_OPEN",
        "1" if HOST in {"0.0.0.0", "::"} else "0",
    )
    if no_open != "1":
        browser_host = "127.0.0.1" if HOST in {"0.0.0.0", "::"} else HOST
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{browser_host}:{PORT}/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
