from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from bs4 import BeautifulSoup
import requests, re, time, logging, os, json, shutil, subprocess

app = Flask(__name__, static_folder=".")
CORS(app, resources={r"/*": {"origins": "*"}})
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE      = "https://election.ratopati.com"
CACHE_TTL = 30
results_cache = {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Referer": BASE,
}

NP_DIGITS = {'०':'0','१':'1','२':'2','३':'3','४':'4',
             '५':'5','६':'6','७':'7','८':'8','९':'9'}

def nepali_to_int(s: str) -> int:
    cleaned = ''.join(NP_DIGITS.get(c, c) for c in str(s))
    cleaned = re.sub(r'[^\d]', '', cleaned)
    return int(cleaned) if cleaned else 0

def fmt_num(n: int) -> str:
    return f"{n:,}" if n else "—"

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
# Selenium — robustly finds Chromium wherever it's installed
# ─────────────────────────────────────────────────────────────────────────────

def _find_binary(*candidates):
    """Return the first existing binary path, or None.
    Checks absolute paths first, then falls back to PATH (shutil.which).
    """
    # 1. Check absolute paths directly
    for c in candidates:
        if c and os.path.isfile(c):
            logging.info(f"  Found binary (abs): {c}")
            return c

    # 2. Try PATH lookup on the basename of every candidate
    #    e.g. "/run/current-system/sw/bin/chromium" → which("chromium")
    seen = set()
    for c in candidates:
        if not c:
            continue
        name = os.path.basename(c)
        if name in seen:
            continue
        seen.add(name)
        found = shutil.which(name)
        if found:
            logging.info(f"  Found binary (PATH): {found}  (looked up '{name}')")
            return found

    return None


def _chrome_version(binary: str) -> str | None:
    """Extract major.minor.build.patch version string from a Chrome/Chromium binary."""
    try:
        out = subprocess.check_output(
            [binary, "--version"], stderr=subprocess.DEVNULL, timeout=10
        ).decode().strip()
        # e.g. "Chromium 124.0.6367.82" or "Google Chrome 124.0.6367.82"
        m = re.search(r"[\d]+\.[\d]+\.[\d]+\.[\d]+", out)
        if m:
            return m.group(0)
        m = re.search(r"[\d]+\.[\d]+\.[\d]+", out)
        if m:
            return m.group(0)
    except Exception as e:
        logging.warning(f"Could not get Chrome version from {binary}: {e}")
    return None


def _arch_platform() -> str:
    """Return the chrome-for-testing platform string for this machine."""
    import platform
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "linux-arm64"
    return "linux64"


def _chromedriver_url(version: str) -> str:
    """Return the correct storage.googleapis.com download URL for chromedriver."""
    plat = _arch_platform()
    return (
        f"https://storage.googleapis.com/chrome-for-testing-public"
        f"/{version}/{plat}/chromedriver-{plat}.zip"
    )


def make_driver():
    import platform as _platform
    logging.info(f"make_driver: arch={_platform.machine()} platform={_platform.platform()[:60]}")
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    # ── 1. Find Chromium / Chrome binary ────────────────────────────────────
    chromium = _find_binary(
        # Nixpacks / Railway NixOS paths
        "/run/current-system/sw/bin/chromium",
        "/run/current-system/sw/bin/chromium-browser",
        # Nix profile paths
        "/nix/var/nix/profiles/default/bin/chromium",
        "/nix/var/nix/profiles/default/bin/chromium-browser",
        # Debian/Ubuntu system install
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        # Snap
        "/snap/bin/chromium",
    )

    # ── 2. Find ChromeDriver binary ──────────────────────────────────────────
    chromedriver = _find_binary(
        "/run/current-system/sw/bin/chromedriver",
        "/nix/var/nix/profiles/default/bin/chromedriver",
        # Ubuntu apt chromium-driver installs here:
        "/usr/lib/chromium-browser/chromedriver",
        "/usr/bin/chromedriver",
        "/usr/local/bin/chromedriver",
    )

    if chromium and chromedriver:
        logging.info(f"System Chromium: {chromium}")
        logging.info(f"System ChromeDriver: {chromedriver}")
        opts.binary_location = chromium
        return webdriver.Chrome(service=Service(chromedriver), options=opts)

    if chromium and not chromedriver:
        # Binary found but no matching driver — use webdriver-manager with
        # the CORRECT version string so it downloads from the right URL.
        logging.info(f"Chromium found at {chromium}, locating matching ChromeDriver…")
        opts.binary_location = chromium
        version = _chrome_version(chromium)
        logging.info(f"Detected Chrome version: {version}")
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            driver_path = ChromeDriverManager(
                chrome_type=ChromeType.CHROMIUM,
                driver_version=version,
            ).install()
            return webdriver.Chrome(service=Service(driver_path), options=opts)
        except Exception as e:
            logging.warning(f"webdriver-manager failed ({e}), trying manual download…")
            driver_path = _download_chromedriver_manual(version)
            return webdriver.Chrome(service=Service(driver_path), options=opts)

    # ── 3. Log PATH contents to help diagnose missing binaries ─────────────
    try:
        scan = subprocess.check_output(
            r"find /run /nix /usr/bin /usr/local/bin /snap/bin "
            r"-maxdepth 6 \( -name 'chrom*' -o -name 'google-chrome*' \) 2>/dev/null | head -20",
            shell=True, timeout=10
        ).decode().strip()
        logging.warning(f"No Chromium found. Filesystem scan:\n{scan or '(nothing found)'}")
    except Exception:
        pass

    # ── 4. Try webdriver-manager (works on local dev if installed) ──────────
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        logging.info("Falling back to ChromeDriverManager…")
        driver_path = ChromeDriverManager().install()
        return webdriver.Chrome(service=Service(driver_path), options=opts)
    except Exception as e:
        logging.warning(f"ChromeDriverManager unavailable: {e}")

    # ── 5. Last resort: download chrome-for-testing directly ──────────────
    logging.info("Attempting manual chrome-for-testing download…")
    driver_path = _download_chromedriver_manual(None)
    chrome_path = _download_chrome_headless(None)
    if chrome_path:
        opts.binary_location = chrome_path
    return webdriver.Chrome(service=Service(driver_path), options=opts)


def _download_chromedriver_manual(version: str | None) -> str:
    """
    Last-resort: download chromedriver from storage.googleapis.com
    using the correct URL format (not the old googlechromelabs path).
    """
    import zipfile, io

    if not version:
        # Fetch latest stable version
        r = requests.get(
            "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json",
            timeout=15,
        )
        r.raise_for_status()
        version = r.json()["channels"]["Stable"]["version"]
        logging.info(f"Latest stable ChromeDriver version: {version}")

    url = _chromedriver_url(version)
    logging.info(f"Downloading ChromeDriver from: {url}")
    r = requests.get(url, timeout=60)
    r.raise_for_status()

    dest = "/tmp/chromedriver"
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if name.endswith("chromedriver") and "__MACOSX" not in name:
                data = z.read(name)
                with open(dest, "wb") as f:
                    f.write(data)
                os.chmod(dest, 0o755)
                logging.info(f"ChromeDriver extracted to {dest}")
                return dest


    raise RuntimeError(f"chromedriver binary not found in zip from {url}")


def _download_chrome_headless(version: str | None) -> str | None:
    """Download chrome-headless-shell from storage.googleapis.com."""
    import zipfile, io

    dest = "/tmp/chrome-headless-shell"
    if os.path.isfile(dest):
        return dest

    try:
        if not version:
            r = requests.get(
                "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json",
                timeout=15,
            )
            r.raise_for_status()
            version = r.json()["channels"]["Stable"]["version"]

        plat = _arch_platform()
        url = (
            f"https://storage.googleapis.com/chrome-for-testing-public"
            f"/{version}/{plat}/chrome-headless-shell-{plat}.zip"
        )
        logging.info(f"Downloading chrome-headless-shell from: {url}")
        r = requests.get(url, timeout=120)
        r.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for name in z.namelist():
                if name.endswith("chrome-headless-shell") and "__MACOSX" not in name:
                    data = z.read(name)
                    with open(dest, "wb") as f:
                        f.write(data)
                    os.chmod(dest, 0o755)
                    logging.info(f"chrome-headless-shell extracted to {dest}")
                    return dest
    except Exception as e:
        logging.warning(f"Could not download chrome-headless-shell: {e}")
    return None


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
                            if v > 1000:
                                logging.info(f"Total voters (body text): {v}")
                                return v
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
                        if v > 1000:
                            logging.info(f"Total voters (DOM): {v}")
                            return v
                parent = parent.parent
    logging.info("Total voters: not found")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# CORE PARSER
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
        if is_num_str(line):
            i += 1
            continue
        name = line
        i += 1
        if i < n and lines[i] == name:
            i += 1
        if i >= n:
            break
        if is_num_str(lines[i]):
            party = "स्वतन्त्र"
        else:
            party = lines[i]
            i += 1
            if i < n and lines[i] == party:
                i += 1
        votes_raw = "—"
        if i < n and is_num_str(lines[i]):
            votes_raw = lines[i]
            i += 1
        winner = False
        if i < n and lines[i].lower() == "win-tick":
            winner = True
            i += 1
        vi = nepali_to_int(votes_raw) if votes_raw != "—" else 0
        candidates.append({
            "candidate_name": name,
            "party":          party,
            "votes":          votes_raw,
            "votes_int":      vi,
            "winner":         winner,
            "photo":          None,
            "party_logo":     None,
        })
    logging.info(f"Text parser → {len(candidates)} candidates")
    return candidates

def _find_card(anchor):
    node = anchor
    while node.parent is not None:
        parent = node.parent
        cand_links = parent.find_all("a", href=re.compile(r"/candidate/"))
        if len(cand_links) > 1:
            return node
        node = parent
    return node

def _enrich_photos(candidates: list, container_soup) -> list:
    if not candidates:
        return candidates
    name_map = {c["candidate_name"]: c for c in candidates}
    anchors = container_soup.find_all("a", href=re.compile(r"/candidate/"))
    logging.info(f"  /candidate/ anchors in container: {len(anchors)}")
    seen_hrefs = set()
    for anchor in anchors:
        href = anchor.get("href", "")
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        anchor_name = anchor.get_text(strip=True)
        cand = name_map.get(anchor_name)
        if not cand:
            for cn in name_map:
                if cn in anchor_name or anchor_name in cn:
                    cand = name_map[cn]
                    break
        if not cand:
            continue
        card = _find_card(anchor)
        if not cand["photo"]:
            for img in card.find_all("img", src=True):
                src = img.get("src", "")
                if any(k in src.lower() for k in
                       ["party", "symbol", "flag", "logo", "icon",
                        "placeholder", "default", "blank", "avatar"]):
                    continue
                try:
                    w = int(img.get("width", "0") or "0")
                    if 0 < w < 25:
                        continue
                except (ValueError, TypeError):
                    pass
                cand["photo"] = abs_url(src)
                break
        if not cand["party_logo"]:
            party_link = card.find("a", href=re.compile(r"/party/"))
            if party_link:
                img = party_link.find("img", src=True)
                if not img and party_link.parent:
                    img = party_link.parent.find("img", src=True)
                if img:
                    cand["party_logo"] = abs_url(img.get("src", ""))
    logging.info(f"  Photos assigned: {sum(1 for c in candidates if c['photo'])}/{len(candidates)}")
    logging.info(f"  Logos assigned:  {sum(1 for c in candidates if c['party_logo'])}/{len(candidates)}")
    return candidates

def parse_results_from_html(html: str, container_index: int = 1) -> list:
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all(
        "div",
        class_=lambda c: c and "result-container" in c and "col6" in c
    )
    logging.info(f"result-container.col6 found: {len(containers)}")
    if not containers:
        containers = soup.find_all(
            "div",
            class_=lambda c: c and "result-container" in c
        )
        logging.info(f"Fallback result-container found: {len(containers)}")
    if not containers:
        logging.error("No result-container found at all")
        return []
    idx = min(container_index, len(containers) - 1)
    container = containers[idx]
    logging.info(f"Using container[{idx}] of {len(containers)}")
    container_text = container.get_text(separator="\n")
    lines = _container_lines(container_text)
    logging.info(f"Container lines after cleaning: {len(lines)}")
    for li, l in enumerate(lines[:30]):
        logging.info(f"  [{li:02d}] {l}")
    candidates = _parse_container_text(lines)
    candidates = _enrich_photos(candidates, container)
    winners = [c for c in candidates if c["winner"]]
    others  = sorted(
        [c for c in candidates if not c["winner"]],
        key=lambda c: c["votes_int"],
        reverse=True
    )
    return winners + others


# ─────────────────────────────────────────────────────────────────────────────
# Main scrape
# ─────────────────────────────────────────────────────────────────────────────

def scrape(slug: str) -> dict:
    url = f"{BASE}/constituency/{slug}"
    logging.info(f"Scraping: {url}")
    driver = make_driver()
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: bool(d.find_elements(
                    By.CSS_SELECTOR,
                    "div.result-container.col6, div.result-container"
                ))
            )
        except Exception:
            logging.warning("result-container wait timed out")
        time.sleep(2)
        html      = driver.page_source
        body_text = driver.find_element(By.TAG_NAME, "body").text
        logging.info(f"HTML {len(html):,} chars  Body {len(body_text):,} chars")
        total_voters = parse_total_voters(body_text, html)
        candidates = parse_results_from_html(html, container_index=0)
        if not candidates:
            logging.warning("container[0] returned nothing — trying container[1]")
            candidates = parse_results_from_html(html, container_index=1)
        if not candidates:
            logging.error(f"No candidates at all.\nBody snippet:\n{body_text[:600]}")
            candidates = [{
                "candidate_name": "डाटा उपलब्ध छैन",
                "party": "—", "votes": "—", "votes_int": 0,
                "winner": False, "photo": None, "party_logo": None,
            }]
        return {
            "constituency_slug": slug,
            "url":               url,
            "year":              "2082",
            "scraped_at":        time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_voters":      total_voters,
            "candidates":        candidates,
        }
    finally:
        driver.quit()


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the frontend."""
    return send_from_directory(".", "index.html")

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
    url  = f"{BASE}/constituency/{slug}"
    driver = make_driver()
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: len(d.find_element(By.TAG_NAME, "body").text) > 300
            )
        except Exception:
            pass
        time.sleep(2)
        html      = driver.page_source
        body_text = driver.find_element(By.TAG_NAME, "body").text
        soup = BeautifulSoup(html, "html.parser")
        keywords = ["जम्मा", "पुरुष", "महिला", "मतदाता", "voter"]
        body_lines = body_text.splitlines()
        context_lines = []
        for i, line in enumerate(body_lines):
            if any(k.lower() in line.lower() for k in keywords):
                start = max(0, i-2)
                end   = min(len(body_lines), i+5)
                context_lines.append({"line_index": i, "context": body_lines[start:end]})
        matching_elements = []
        for el in soup.find_all(True):
            own_text = el.get_text(separator=" ", strip=True)
            if any(k in own_text for k in ["जम्मा मतदाता","पुरुष मतदाता","महिला मतदाता"]):
                if len(own_text) < 500:
                    matching_elements.append({
                        "tag": el.name, "classes": el.get("class", []),
                        "html": str(el)[:600], "text": own_text[:200],
                    })
        return jsonify({
            "url": url, "body_line_count": len(body_lines),
            "keyword_contexts": context_lines[:20],
            "matching_elements": matching_elements[:15],
        })
    finally:
        driver.quit()

@app.route("/debug/<path:slug>")
def debug_html(slug: str):
    slug = slug.strip("/").lower()
    url  = f"{BASE}/constituency/{slug}"
    driver = make_driver()
    try:
        driver.get(url)
        try:
            WebDriverWait(driver, 30).until(
                lambda d: bool(d.find_elements(
                    By.CSS_SELECTOR, "div.result-container.col6, div.result-container"
                ))
            )
        except Exception:
            pass
        time.sleep(2)
        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.find_all(
            "div", class_=lambda c: c and "result-container" in c and "col6" in c
        )
        if not containers:
            containers = soup.find_all(
                "div", class_=lambda c: c and "result-container" in c
            )
        out = []
        for i, c in enumerate(containers):
            anchors = c.find_all("a", href=re.compile(r"/candidate/"))[:6]
            samples = []
            for a in anchors:
                node = a
                for _ in range(5):
                    if node.parent: node = node.parent
                samples.append(str(node)[:2000])
            out.append({
                "index": i, "classes": c.get("class", []),
                "text_preview": c.get_text(separator="|", strip=True)[:400],
                "candidate_anchor_count": len(c.find_all("a", href=re.compile(r"/candidate/"))),
                "img_count": len(c.find_all("img")),
                "all_img_srcs": [img.get("src","") for img in c.find_all("img", src=True)][:20],
                "first_candidate_sample_html": samples[:2],
            })
        return jsonify({"url": url, "container_count": len(containers), "containers": out})
    finally:
        driver.quit()

@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    results_cache.clear()
    return jsonify({"status": "cleared"})

@app.route("/health")
def health():
    # Report what binaries were found
    chromium = (
        _find_binary(
            "/run/current-system/sw/bin/chromium",
            "/run/current-system/sw/bin/chromium-browser",
            "/nix/var/nix/profiles/default/bin/chromium",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome", "/snap/bin/chromium",
        ) or "not found"
    )
    chromedriver = (
        _find_binary(
            "/run/current-system/sw/bin/chromedriver",
            "/nix/var/nix/profiles/default/bin/chromedriver",
            "/usr/bin/chromedriver", "/usr/local/bin/chromedriver",
        ) or "not found"
    )
    return jsonify({
        "status": "ok",
        "source": BASE,
        "chromium": chromium,
        "chromedriver": chromedriver,
        "chrome_version": _chrome_version(chromium) if chromium != "not found" else None,
        "endpoints": {
            "GET /results/<slug>": "e.g. /results/jhapa-5 — returns 2082 results",
            "POST /cache/clear":   "Flush cache",
            "GET /health":         "Binary paths + version info",
        },
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
