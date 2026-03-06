from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import requests, re, time, logging, os, json, shutil, subprocess

app = Flask(__name__, static_folder=".")
CORS(app, resources={r"/*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE      = "https://election.ratopati.com"
CACHE_TTL = 30
results_cache = {}

NP_DIGITS = {'०':'0','१':'1','२':'2','३':'3','४':'4',
             '५':'5','६':'6','७':'7','८':'8','९':'9'}

def nepali_to_int(s: str) -> int:
    cleaned = ''.join(NP_DIGITS.get(c, c) for c in str(s))
    cleaned = re.sub(r'[^\d]', '', cleaned)
    return int(cleaned) if cleaned else 0

def is_num_str(s: str) -> bool:
    s = s.strip()
    if not s: return False
    nc = sum(1 for c in s if c in NP_DIGITS or c.isdigit() or c == ',')
    return nc >= max(len(s) * 0.75, 1)

def abs_url(src: str):
    if not src: return None
    src = src.strip()
    if src.startswith("data:"): return None
    if src.startswith("http"):  return src
    if src.startswith("//"):    return "https:" + src
    if src.startswith("/"):     return BASE + src
    return BASE + "/" + src

def is_fresh(e):
    return e.get("data") is not None and time.time() < e.get("expires_at", 0)


# ─────────────────────────────────────────────────────────────────────────────
# Selenium setup
# ─────────────────────────────────────────────────────────────────────────────

def _find_binary(*candidates):
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    seen = set()
    for c in candidates:
        if not c: continue
        name = os.path.basename(c)
        if name in seen: continue
        seen.add(name)
        found = shutil.which(name)
        if found: return found
    return None

def _chrome_version(binary: str):
    try:
        out = subprocess.check_output([binary,"--version"], stderr=subprocess.DEVNULL, timeout=10).decode().strip()
        m = re.search(r"[\d]+\.[\d]+\.[\d]+\.[\d]+", out)
        if m: return m.group(0)
        m = re.search(r"[\d]+\.[\d]+\.[\d]+", out)
        if m: return m.group(0)
    except: pass
    return None

def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    chromium = _find_binary(
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/run/current-system/sw/bin/chromium", "/snap/bin/chromium",
    )
    chromedriver = _find_binary(
        "/usr/bin/chromedriver", "/usr/lib/chromium-browser/chromedriver",
        "/usr/local/bin/chromedriver", "/run/current-system/sw/bin/chromedriver",
    )
    if chromium:
        opts.binary_location = chromium
    if chromedriver:
        return webdriver.Chrome(service=Service(chromedriver), options=opts)
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.core.os_manager import ChromeType
        wdm_type = ChromeType.CHROMIUM if chromium else ChromeType.GOOGLE
        return webdriver.Chrome(service=Service(ChromeDriverManager(chrome_type=wdm_type).install()), options=opts)
    except Exception as e:
        raise RuntimeError(f"ChromeDriver not found: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# JS DOM extractor — runs inside the real browser, sees the real rendered DOM
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_JS = r"""
(function() {
  var NP = {'०':'0','१':'1','२':'2','३':'3','४':'4','५':'5','६':'6','७':'7','८':'8','९':'9'};
  function toInt(s) {
    return parseInt((s||'').split('').map(function(c){return NP[c]||c;}).join('').replace(/[^0-9]/g,'') || '0', 10) || 0;
  }

  // ── Step 1: find the lead-table section ───────────────────────────────
  var section = document.querySelector(
    '.section-lead-table, section.section-lead-table, ' +
    '[class*="lead-table"], [class*="leadTable"], ' +
    '[class*="party-result"], [class*="partyResult"], ' +
    '[class*="seat-count"], [class*="seatCount"]'
  );

  if (!section) {
    // Wider scan: find ANY element that has 3+ party logo imgs
    var allEls = Array.from(document.querySelectorAll('section, div'));
    for (var i = 0; i < allEls.length; i++) {
      var imgs = allEls[i].querySelectorAll('img');
      if (imgs.length >= 3 && allEls[i].innerText && /[0-9]/.test(allEls[i].innerText)) {
        section = allEls[i];
        break;
      }
    }
  }
  if (!section) return {found: false, error: 'section not found', parties: []};

  // ── Step 2: discover column order from headers ────────────────────────
  var headers = Array.from(section.querySelectorAll('th, [class*="head"] > *, [class*="header"] > *'))
    .map(function(h){ return (h.innerText||'').trim().toLowerCase(); });

  // Common Nepali / English labels
  var WON_KEYS     = ['won','विजयी','जितेको','विजय','win','jite'];
  var LEADING_KEYS = ['lead','अग्रणी','अग्रता','leading','aagrani','ahead'];

  var wonCol     = -1;
  var leadingCol = -1;
  headers.forEach(function(h,i){
    if (WON_KEYS.some(function(k){return h.indexOf(k)>=0;}))     wonCol     = i;
    if (LEADING_KEYS.some(function(k){return h.indexOf(k)>=0;})) leadingCol = i;
  });

  // ── Step 3: find repeating row/card elements ─────────────────────────
  // Walk the direct children of section; if they each have an img, treat as rows.
  // Otherwise, go one level deeper.
  function getRows(parent) {
    var direct = Array.from(parent.children);
    var withImg = direct.filter(function(el){ return el.querySelector('img'); });
    if (withImg.length >= 2) return withImg;
    // Try one level deeper
    for (var i=0;i<direct.length;i++) {
      var sub = Array.from(direct[i].children).filter(function(el){ return el.querySelector('img'); });
      if (sub.length >= 2) return sub;
    }
    // Fallback: all <tr> rows
    var rows = Array.from(section.querySelectorAll('tr')).filter(function(r){ return r.querySelector('img'); });
    if (rows.length >= 1) return rows;
    return [];
  }

  var rows = getRows(section);

  // ── Step 4: extract data from each row ───────────────────────────────
  var parties = [];
  rows.forEach(function(row, rowIdx) {
    var img = row.querySelector('img');
    if (!img) return;
    var logo = img.src || img.getAttribute('src') || '';
    if (!logo || logo.indexOf('data:') === 0) return;

    // ── Party name ────────────────────────────────────────────────────
    // Try specific selectors first
    var nameEl = row.querySelector(
      '[class*="name"],[class*="title"],[class*="partyName"],[class*="party-name"],' +
      '[class*="party_name"],[class*="text"]'
    );
    var name = nameEl ? (nameEl.innerText||'').trim() : '';

    // If that gives a number or nothing, fall back to all non-numeric text
    if (!name || /^[0-9\s,]+$/.test(name)) {
      // Collect all text nodes, skip purely numeric ones
      var walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      var parts = [];
      while (walker.nextNode()) {
        var t = (walker.currentNode.textContent||'').trim();
        // Replace Nepali digits, then check if it's purely numeric
        var ascii = t.split('').map(function(c){return NP[c]||c;}).join('');
        if (!ascii || /^[0-9,\s]+$/.test(ascii)) continue;
        // Skip very short noise
        if (ascii.length < 2) continue;
        parts.push(t);
      }
      name = parts.join(' ').trim();
    }
    if (!name) return;

    // ── Numbers: collect ALL numeric text nodes in DOM order ──────────
    var allNums = [];
    var numWalker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
    while (numWalker.nextNode()) {
      var t2 = (numWalker.currentNode.textContent||'').trim();
      // Nepali or ASCII digits only (with optional comma)
      if (/^[0-9,०-९]+$/.test(t2) && t2.replace(/,/g,'').length >= 1) {
        allNums.push(toInt(t2));
      }
    }

    // ── Won / Leading ─────────────────────────────────────────────────
    var won = 0, leading = 0;

    // Strategy 1: class-name labels
    var wonEl = row.querySelector(
      '[class*="won"],[class*="vijay"],[class*="Win"],[class*="win"]:not([class*="window"]):not([class*="wink"])'
    );
    var leadEl = row.querySelector(
      '[class*="lead"]:not([class*="leading-space"]):not([class*="leader"]),' +
      '[class*="ahead"],[class*="aagrani"]'
    );
    if (wonEl)  won     = toInt(wonEl.innerText);
    if (leadEl) leading = toInt(leadEl.innerText);

    // Strategy 2: table column order from headers
    if (won === 0 && leading === 0 && wonCol >= 0 && allNums.length > wonCol - 1) {
      won     = allNums[wonCol - 1]     || 0;
      leading = allNums[leadingCol - 1] || 0;
    }

    // Strategy 3: positional (first num = won, second = leading)
    if (won === 0 && leading === 0 && allNums.length >= 1) {
      won     = allNums[0] || 0;
      leading = allNums[1] || 0;
    }

    parties.push({
      name:    name.substring(0,100),
      logo:    logo,
      won:     won,
      leading: leading,
      total:   won + leading,
      _nums:   allNums.slice(0,6),
    });
  });

  // Sort by total desc
  parties.sort(function(a,b){ return b.total - a.total; });

  return {
    found: true,
    section_class: section.className,
    row_count: rows.length,
    header_labels: headers,
    parties: parties,
  };
})();
"""

# Lightweight debug JS — dumps real class names without scraping
_DEBUG_JS = r"""
(function(){
  var sel = '.section-lead-table,[class*="lead-table"],[class*="leadTable"],[class*="party-result"]';
  var section = document.querySelector(sel);
  if (!section) {
    var tops = Array.from(document.querySelectorAll('section, main > div, #__next > div')).slice(0,30);
    return {
      found: false,
      top_sections: tops.map(function(t){return {tag:t.tagName, cls:t.className, txt:(t.innerText||'').slice(0,100)};})
    };
  }
  return {
    found: true,
    section_class: section.className,
    outer_html_preview: section.outerHTML.slice(0,6000),
    text_preview: section.innerText.slice(0,1500),
    all_child_classes: Array.from(section.querySelectorAll('*')).slice(0,80)
      .map(function(e){return {tag:e.tagName, cls:e.className, txt:(e.innerText||'').slice(0,50)};})
  };
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# scrape_party_seats
# ─────────────────────────────────────────────────────────────────────────────

def scrape_party_seats() -> dict:
    logging.info(f"scrape_party_seats → {BASE}")
    driver = make_driver()
    try:
        driver.get(BASE)

        # Wait for body to have meaningful content — don't require the exact section
        try:
            WebDriverWait(driver, 30).until(
                lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 500
            )
        except Exception:
            logging.warning("body-text wait timed out")

        # Extra wait for JS-rendered content
        time.sleep(4)

        html = driver.page_source
        logging.info(f"  Homepage HTML length: {len(html):,} chars")

        # ── Try JS extractor first ─────────────────────────────────────
        parties = []
        try:
            result = driver.execute_script(_EXTRACT_JS)
            if result and isinstance(result, dict):
                logging.info(
                    f"  JS result: found={result.get('found')}, "
                    f"rows={result.get('row_count')}, "
                    f"parties={len(result.get('parties', []))}, "
                    f"class={str(result.get('section_class','?'))[:80]}"
                )
                parties = result.get("parties") or []
            else:
                logging.warning(f"  execute_script returned: {repr(result)}")
        except Exception as js_err:
            logging.warning(f"  JS extractor threw: {js_err}")

        # ── BeautifulSoup fallback ─────────────────────────────────────
        if not parties:
            logging.info("  Falling back to BeautifulSoup parser")
            parties = _bs_parse_party_seats(html)

        logging.info(f"  Final party count: {len(parties)}")

        return {
            "scraped_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
            "majority":    138,
            "total_seats": 165,
            "parties":     parties,
        }
    finally:
        driver.quit()


def _bs_parse_party_seats(html: str) -> list:
    """
    BeautifulSoup fallback.  Scans every element that contains an <img> and
    at least one number, groups them into party rows, and extracts
    name / logo / won / leading.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── 1. Try to find the lead-table section ─────────────────────────
    section = None
    for tag in soup.find_all(True):
        cls = " ".join(tag.get("class") or [])
        if any(kw in cls for kw in ("lead-table", "leadTable", "section-lead", "party-result", "seat-count")):
            section = tag
            logging.info(f"  BS: found section by class → {cls[:80]}")
            break

    # fallback: search for a <table> that has party logo images
    if not section:
        for tbl in soup.find_all("table"):
            if len(tbl.find_all("img")) >= 3:
                section = tbl
                logging.info("  BS: using table with 3+ imgs as section")
                break

    # last resort: scan the whole page
    if not section:
        section = soup
        logging.warning("  BS: scanning full page")

    # ── 2. Collect candidate row elements ─────────────────────────────
    # Find the smallest elements that each have exactly one img and some numbers
    seen_ids = set()
    rows = []

    for el in section.find_all(True):
        eid = id(el)
        if eid in seen_ids:
            continue
        imgs = el.find_all("img", src=True)
        if len(imgs) != 1:
            continue
        txt = el.get_text()
        if not re.search(r'[0-9०-९]', txt):
            continue
        # Skip wrappers that contain other candidate rows
        inner_imgs = sum(1 for c in el.find_all(True) if c.find_all("img", src=True))
        if inner_imgs > 1:
            continue
        rows.append(el)
        for desc in el.find_all(True):
            seen_ids.add(id(desc))
        seen_ids.add(eid)

    logging.info(f"  BS: candidate rows found = {len(rows)}")

    if not rows:
        return []

    # ── 3. Extract from each row ───────────────────────────────────────
    parties = []
    for row in rows:
        img = row.find("img", src=True)
        if not img:
            continue
        logo = abs_url(img.get("src", ""))
        if not logo:
            continue

        # Party name: all non-numeric text in the row
        raw_text = row.get_text(separator=" ", strip=True)
        name_tokens = []
        for tok in raw_text.split():
            clean = ''.join(NP_DIGITS.get(c, c) for c in tok)
            clean = re.sub(r'[^\d]', '', clean)
            if not clean:          # not a number token
                name_tokens.append(tok)
        name = " ".join(name_tokens).strip()
        # Remove noise
        name = re.sub(r'\s+', ' ', name).strip()
        if not name or len(name) < 2:
            continue

        # All numbers in DOM order
        nums = [nepali_to_int(m) for m in re.findall(r'[0-9,०-९]+', raw_text)]
        nums = [n for n in nums if n >= 0]  # keep zeros too

        won     = nums[0] if len(nums) > 0 else 0
        leading = nums[1] if len(nums) > 1 else 0

        # Try to detect won/leading from child element classes
        for child in row.find_all(True):
            ccls = " ".join(child.get("class") or []).lower()
            ctxt = child.get_text(strip=True)
            cval = nepali_to_int(ctxt) if ctxt else 0
            if any(k in ccls for k in ("won", "vijay", "win")) and "window" not in ccls:
                won = cval
            elif any(k in ccls for k in ("lead", "ahead", "aagrani")):
                leading = cval

        parties.append({
            "name":    name[:100],
            "logo":    logo,
            "won":     won,
            "leading": leading,
            "total":   won + leading,
        })

    parties.sort(key=lambda p: p["total"], reverse=True)
    return parties


# ─────────────────────────────────────────────────────────────────────────────
# VOTER STATS
# ─────────────────────────────────────────────────────────────────────────────

def parse_total_voters(body: str, html: str) -> int:
    labels = ["जम्मा मतदाता", "कुल मतदाता"]
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        for lbl in labels:
            if lbl in line:
                for j in range(1, 4):
                    if i + j < len(lines):
                        nxt = lines[i + j]
                        if is_num_str(nxt):
                            v = nepali_to_int(nxt)
                            if v > 1000: return v
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=True):
        if any(lbl in node for lbl in labels):
            parent = node.parent
            for _ in range(5):
                if parent is None: break
                sib = parent.find_next_sibling()
                if sib:
                    t = sib.get_text(strip=True)
                    if is_num_str(t):
                        v = nepali_to_int(t)
                        if v > 1000: return v
                parent = parent.parent
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CORE CONSTITUENCY PARSER (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

_NOISE = re.compile(
    r'^©|ratopati|copyright|सम्पत्ति हुन्|न्युज नेटवर्क|'
    r'election\.ratopati\.com|निर्वाचन.*उम्मेदवारहरु|^logo$|'
    r'जम्मा मतदाता|पुरुष मतदाता|महिला मतदाता|मतदाता विवरण',
    re.I
)

def _container_lines(container_text: str) -> list:
    out = []
    for raw in container_text.splitlines():
        l = raw.strip()
        if not l: continue
        if _NOISE.search(l): continue
        if is_num_str(l) and nepali_to_int(l) < 50 and len(l) <= 4:
            continue
        out.append(l)
    return out

def _parse_container_text(lines: list) -> list:
    candidates = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if is_num_str(line): i += 1; continue
        name = line; i += 1
        if i < n and lines[i] == name: i += 1
        if i >= n: break
        if is_num_str(lines[i]):
            party = "स्वतन्त्र"
        else:
            party = lines[i]; i += 1
            if i < n and lines[i] == party: i += 1
        votes_raw = "—"
        if i < n and is_num_str(lines[i]):
            votes_raw = lines[i]; i += 1
        winner = False
        if i < n and lines[i].lower() == "win-tick":
            winner = True; i += 1
        vi = nepali_to_int(votes_raw) if votes_raw != "—" else 0
        candidates.append({"candidate_name":name,"party":party,"votes":votes_raw,"votes_int":vi,"winner":winner,"photo":None,"party_logo":None})
    return candidates

def _find_card(anchor):
    node = anchor
    while node.parent is not None:
        parent = node.parent
        if len(parent.find_all("a", href=re.compile(r"/candidate/"))) > 1:
            return node
        node = parent
    return node

def _enrich_photos(candidates: list, container_soup) -> list:
    if not candidates: return candidates
    name_map = {c["candidate_name"]: c for c in candidates}
    anchors = container_soup.find_all("a", href=re.compile(r"/candidate/"))
    seen_hrefs = set()
    for anchor in anchors:
        href = anchor.get("href","")
        if href in seen_hrefs: continue
        seen_hrefs.add(href)
        anchor_name = anchor.get_text(strip=True)
        cand = name_map.get(anchor_name)
        if not cand:
            for cn in name_map:
                if cn in anchor_name or anchor_name in cn:
                    cand = name_map[cn]; break
        if not cand: continue
        card = _find_card(anchor)
        if not cand["photo"]:
            for img in card.find_all("img", src=True):
                src = img.get("src","")
                if any(k in src.lower() for k in ["party","symbol","flag","logo","icon","placeholder","default","blank","avatar"]): continue
                try:
                    w = int(img.get("width","0") or "0")
                    if 0 < w < 25: continue
                except: pass
                cand["photo"] = abs_url(src); break
        if not cand["party_logo"]:
            party_link = card.find("a", href=re.compile(r"/party/"))
            if party_link:
                img = party_link.find("img", src=True)
                if not img and party_link.parent:
                    img = party_link.parent.find("img", src=True)
                if img:
                    cand["party_logo"] = abs_url(img.get("src",""))
    return candidates

def parse_results_from_html(html: str, container_index: int = 1) -> list:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all("div", class_=lambda c: c and "result-container" in c and "col6" in c)
    if not containers:
        containers = soup.find_all("div", class_=lambda c: c and "result-container" in c)
    if not containers: return []
    idx = min(container_index, len(containers) - 1)
    container = containers[idx]
    lines = _container_lines(container.get_text(separator="\n"))
    candidates = _parse_container_text(lines)
    candidates = _enrich_photos(candidates, container)
    winners = [c for c in candidates if c["winner"]]
    others  = sorted([c for c in candidates if not c["winner"]], key=lambda c: c["votes_int"], reverse=True)
    return winners + others


# ─────────────────────────────────────────────────────────────────────────────
# Constituency scrape (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def scrape(slug: str) -> dict:
    url = f"{BASE}/constituency/{slug}"
    driver = make_driver()
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(lambda d: bool(d.find_elements(By.CSS_SELECTOR,"div.result-container.col6, div.result-container")))
        except: logging.warning("result-container wait timed out")
        time.sleep(2)
        html = driver.page_source
        body_text = driver.find_element(By.TAG_NAME,"body").text
        total_voters = parse_total_voters(body_text, html)
        candidates = parse_results_from_html(html, 0) or parse_results_from_html(html, 1)
        if not candidates:
            candidates = [{"candidate_name":"डाटा उपलब्ध छैन","party":"—","votes":"—","votes_int":0,"winner":False,"photo":None,"party_logo":None}]
        return {"constituency_slug":slug,"url":url,"year":"2082","scraped_at":time.strftime("%Y-%m-%d %H:%M:%S"),"total_voters":total_voters,"candidates":candidates}
    finally:
        driver.quit()


# ─────────────────────────────────────────────────────────────────────────────
# HOT-SEATS / TRENDING CANDIDATES
# ─────────────────────────────────────────────────────────────────────────────

_HOT_JS = r"""
(function(){
  var NP={'०':'0','१':'1','२':'2','३':'3','४':'4','५':'5','६':'6','७':'7','८':'8','९':'9'};
  function toInt(s){return parseInt((s||'').split('').map(function(c){return NP[c]||c;}).join('').replace(/[^0-9]/g,'') || '0',10)||0;}

  // Collect every card-like element: has a candidate photo AND a name AND a number
  var allEls = Array.from(document.querySelectorAll('*'));
  var seen = new Set();
  var cards = [];

  for(var i=0;i<allEls.length;i++){
    var el = allEls[i];
    if(seen.has(el)) continue;
    // Must contain an img
    var imgs = el.querySelectorAll('img');
    if(imgs.length < 1) continue;
    var txt = (el.innerText||'').trim();
    // Must have at least one number
    if(!/[0-9०-९]/.test(txt)) continue;
    // Skip if it's a massive wrapper
    if(el.querySelectorAll('img').length > 4) continue;
    if(txt.length > 500) continue;
    cards.push(el);
    el.querySelectorAll('*').forEach(function(c){seen.add(c);});
    seen.add(el);
  }

  var candidates = [];
  cards.forEach(function(card){
    var imgs = Array.from(card.querySelectorAll('img[src]'));
    var candidateImg = null;
    var partyImg = null;

    // Heuristic: larger/first img = candidate photo, img inside a small element = party logo
    imgs.forEach(function(img){
      var src = img.src||'';
      // skip data URIs
      if(src.indexOf('data:')===0) return;
      var cls = (img.className||'') + ' ' + (img.closest('a')?img.closest('a').className:'');
      if(/party|logo|symbol|flag/i.test(src+cls)){
        if(!partyImg) partyImg=img;
      } else {
        if(!candidateImg) candidateImg=img;
      }
    });
    // fallback: first img = candidate
    if(!candidateImg && imgs.length>0) candidateImg=imgs[0];
    if(!candidateImg) return;

    var photo = candidateImg.src;

    // Party logo: second img, or img inside element with party-related class/href
    if(!partyImg && imgs.length>1) partyImg=imgs[1];
    var partyLogo = partyImg ? partyImg.src : '';
    if(partyLogo===photo) partyLogo='';

    // Candidate name: look for named element, else non-numeric text tokens
    var nameEl = card.querySelector('[class*="name"],[class*="title"],[class*="cand"],[class*="person"],[class*="candidate"]');
    var name = nameEl ? (nameEl.innerText||'').trim() : '';
    if(!name || /^[0-9\s,]+$/.test(name)){
      var walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
      var parts=[];
      while(walker.nextNode()){
        var t=(walker.currentNode.textContent||'').trim();
        var ascii=t.split('').map(function(c){return NP[c]||c;}).join('');
        if(!ascii || /^[0-9,\s%]+$/.test(ascii) || ascii.length<2) continue;
        parts.push(t);
      }
      name=parts.join(' ').trim();
    }
    if(!name) return;

    // Party name
    var partyEl = card.querySelector('[class*="party"],[class*="dol"],[class*="group"]');
    var party = partyEl ? (partyEl.innerText||'').trim() : '';
    // Remove name from party if duplicated
    if(party===name) party='';

    // Constituency / area
    var areaEl = card.querySelector('[class*="area"],[class*="const"],[class*="region"],[class*="district"],[class*="seat"]');
    var area = areaEl ? (areaEl.innerText||'').trim() : '';

    // Votes / count
    var walker2 = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    var nums=[];
    while(walker2.nextNode()){
      var t2=(walker2.currentNode.textContent||'').trim();
      if(/^[0-9,०-९]+$/.test(t2) && t2.replace(/,/g,'').length>=1)
        nums.push(toInt(t2));
    }
    var votes = nums.length>0 ? Math.max.apply(null,nums) : 0;

    // Link to candidate page
    var link = '';
    var anchor = card.closest('a[href]') || card.querySelector('a[href*="candidate"],a[href*="person"]');
    if(anchor) link = anchor.href;

    // Status: won / leading
    var status = '';
    var statusEl = card.querySelector('[class*="win"],[class*="lead"],[class*="vijay"],[class*="status"],[class*="badge"]');
    if(statusEl) status = (statusEl.innerText||'').trim();

    candidates.push({
      name: name.substring(0,80),
      photo: photo,
      party: party.substring(0,60),
      party_logo: partyLogo,
      area: area.substring(0,80),
      votes: votes,
      status: status.substring(0,30),
      link: link,
    });
  });

  return {
    url: window.location.href,
    count: candidates.length,
    candidates: candidates,
  };
})();
"""


def scrape_hot_seats() -> dict:
    """Scrape election.ratopati.com/hot-seats for trending candidates."""
    url = f"{BASE}/hot-seats"
    logging.info(f"scrape_hot_seats → {url}")
    driver = make_driver()
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 300
            )
        except Exception:
            logging.warning("hot-seats body wait timed out")
        time.sleep(4)  # let JS render cards

        html = driver.page_source
        logging.info(f"  hot-seats HTML length: {len(html):,}")

        candidates = []
        # Try JS extractor
        try:
            result = driver.execute_script(_HOT_JS)
            if result and isinstance(result, dict):
                candidates = result.get("candidates") or []
                logging.info(f"  JS extractor: {len(candidates)} candidates")
            else:
                logging.warning(f"  JS returned: {repr(result)}")
        except Exception as e:
            logging.warning(f"  JS error: {e}")

        # BS4 fallback
        if not candidates:
            logging.info("  Falling back to BS4 for hot-seats")
            candidates = _bs_parse_hot_seats(html)

        logging.info(f"  Final hot-seats count: {len(candidates)}")
        return {
            "scraped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_url": url,
            "candidates": candidates,
        }
    finally:
        driver.quit()


def _bs_parse_hot_seats(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen_ids = set()

    for el in soup.find_all(True):
        eid = id(el)
        if eid in seen_ids:
            continue
        imgs = el.find_all("img", src=True)
        if len(imgs) < 1:
            continue
        txt = el.get_text()
        if not re.search(r'[0-9०-९]', txt):
            continue
        if len(el.find_all(True)) > 25:
            continue
        if len(txt) > 400:
            continue

        # deduplicate children
        for desc in el.find_all(True):
            seen_ids.add(id(desc))
        seen_ids.add(eid)

        img = imgs[0]
        photo = abs_url(img.get("src", ""))
        if not photo:
            continue

        raw_text = el.get_text(separator=" ", strip=True)
        name_tokens = []
        for tok in raw_text.split():
            c = ''.join(NP_DIGITS.get(ch, ch) for ch in tok)
            c = re.sub(r'[^\d]', '', c)
            if not c:
                name_tokens.append(tok)
        name = " ".join(name_tokens).strip()
        if not name or len(name) < 2:
            continue

        nums = [nepali_to_int(m) for m in re.findall(r'[0-9,०-९]+', raw_text)]
        votes = max(nums) if nums else 0

        # try to get party logo from second img
        party_logo = abs_url(imgs[1].get("src", "")) if len(imgs) > 1 else None
        if party_logo == photo:
            party_logo = None

        candidates.append({
            "name": name[:80],
            "photo": photo,
            "party": "",
            "party_logo": party_logo,
            "area": "",
            "votes": votes,
            "status": "",
            "link": "",
        })

    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/party-seats")
def party_seats():
    entry = results_cache.get("__party_seats__", {})
    if is_fresh(entry):
        return jsonify(entry["data"])
    try:
        data = scrape_party_seats()
        results_cache["__party_seats__"] = {"data": data, "expires_at": time.time() + CACHE_TTL}
        return jsonify(data)
    except Exception as e:
        logging.error(f"party-seats failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



@app.route("/hot-seats")
def hot_seats():
    entry = results_cache.get("__hot_seats__", {})
    if is_fresh(entry):
        return jsonify(entry["data"])
    try:
        data = scrape_hot_seats()
        results_cache["__hot_seats__"] = {"data": data, "expires_at": time.time() + CACHE_TTL}
        return jsonify(data)
    except Exception as e:
        logging.error(f"hot-seats failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/debug-lead-table")
def debug_lead_table():
    """Dumps DOM + all class names to diagnose section-lead-table structure."""
    driver = make_driver()
    try:
        driver.get(BASE)
        try:
            WebDriverWait(driver, 30).until(lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 500)
        except Exception:
            pass
        time.sleep(4)
        js_result = None
        try:
            js_result = driver.execute_script(_DEBUG_JS)
        except Exception as e:
            js_result = {"js_error": str(e)}
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        all_classes = sorted({c for tag in soup.find_all(True) for c in (tag.get("class") or [])})
        party_related = [c for c in all_classes if any(k in c.lower() for k in ["party","lead","seat","result","table","win","vote","count","row","item"])]
        interesting = []
        for el in soup.find_all(True):
            imgs = el.find_all("img", src=True)
            if not imgs: continue
            txt = el.get_text()
            if not re.search(r"[0-9u0966-u096f]", txt): continue
            if len(el.find_all(True)) > 30: continue
            interesting.append({"tag": el.name, "classes": el.get("class", []), "imgs": [i.get("src","")[:80] for i in imgs[:3]], "text": txt.strip()[:120], "html": str(el)[:400]})
        return jsonify({"js_result": js_result, "html_length": len(html), "party_related_classes": party_related[:80], "elements_with_img_and_number": interesting[:20]})
    finally:
        driver.quit()

@app.route("/results/<path:slug>")
def get_results(slug: str):
    slug = slug.strip("/").lower()
    entry = results_cache.get(slug, {})
    if is_fresh(entry):
        return jsonify(entry["data"])
    try:
        data = scrape(slug)
        results_cache[slug] = {"data": data, "expires_at": time.time() + CACHE_TTL}
        return jsonify(data)
    except Exception as e:
        logging.error(f"Failed {slug}: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/debug-voters/<path:slug>")
def debug_voters(slug: str):
    slug = slug.strip("/").lower()
    driver = make_driver()
    try:
        driver.get(f"{BASE}/constituency/{slug}")
        try: WebDriverWait(driver,30).until(lambda d:len(d.find_element(By.TAG_NAME,"body").text)>300)
        except: pass
        time.sleep(2)
        html=driver.page_source; body_text=driver.find_element(By.TAG_NAME,"body").text
        soup=BeautifulSoup(html,"html.parser")
        keywords=["जम्मा","पुरुष","महिला","मतदाता","voter"]
        body_lines=body_text.splitlines(); context_lines=[]
        for i,line in enumerate(body_lines):
            if any(k.lower() in line.lower() for k in keywords):
                context_lines.append({"line_index":i,"context":body_lines[max(0,i-2):min(len(body_lines),i+5)]})
        matching_elements=[]
        for el in soup.find_all(True):
            own_text=el.get_text(separator=" ",strip=True)
            if any(k in own_text for k in ["जम्मा मतदाता","पुरुष मतदाता","महिला मतदाता"]):
                if len(own_text)<500:
                    matching_elements.append({"tag":el.name,"classes":el.get("class",[]),"html":str(el)[:600],"text":own_text[:200]})
        return jsonify({"url":f"{BASE}/constituency/{slug}","body_line_count":len(body_lines),"keyword_contexts":context_lines[:20],"matching_elements":matching_elements[:15]})
    finally: driver.quit()

@app.route("/debug/<path:slug>")
def debug_html(slug: str):
    slug=slug.strip("/").lower()
    driver=make_driver()
    try:
        driver.get(f"{BASE}/constituency/{slug}")
        try: WebDriverWait(driver,30).until(lambda d:bool(d.find_elements(By.CSS_SELECTOR,"div.result-container.col6, div.result-container")))
        except: pass
        time.sleep(2)
        html=driver.page_source; soup=BeautifulSoup(html,"html.parser")
        containers=soup.find_all("div",class_=lambda c:c and "result-container" in c and "col6" in c)
        if not containers: containers=soup.find_all("div",class_=lambda c:c and "result-container" in c)
        out=[]
        for i,c in enumerate(containers):
            anchors=c.find_all("a",href=re.compile(r"/candidate/"))[:6]
            samples=[str(_find_card(a))[:2000] for a in anchors]
            out.append({"index":i,"classes":c.get("class",[]),"text_preview":c.get_text(separator="|",strip=True)[:400],"candidate_anchor_count":len(c.find_all("a",href=re.compile(r"/candidate/"))),"img_count":len(c.find_all("img")),"all_img_srcs":[img.get("src","") for img in c.find_all("img",src=True)][:20],"first_candidate_sample_html":samples[:2]})
        return jsonify({"url":f"{BASE}/constituency/{slug}","container_count":len(containers),"containers":out})
    finally: driver.quit()

@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    results_cache.clear()
    return jsonify({"status":"cleared"})

@app.route("/health")
def health():
    chromium=_find_binary("/usr/bin/chromium","/usr/bin/chromium-browser","/usr/bin/google-chrome","/snap/bin/chromium","/run/current-system/sw/bin/chromium") or "not found"
    chromedriver=_find_binary("/usr/bin/chromedriver","/usr/lib/chromium-browser/chromedriver","/usr/local/bin/chromedriver","/run/current-system/sw/bin/chromedriver") or "not found"
    return jsonify({"status":"ok","source":BASE,"chromium":chromium,"chromedriver":chromedriver,"chrome_version":_chrome_version(chromium) if chromium!="not found" else None,"endpoints":{"GET /party-seats":"Party seat counts (scraped from homepage section-lead-table)","GET /debug-lead-table":"Dump real DOM of lead-table section","GET /results/<slug>":"Constituency results","POST /cache/clear":"Flush cache"}})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
