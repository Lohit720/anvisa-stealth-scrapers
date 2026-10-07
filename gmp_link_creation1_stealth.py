# scraping_links_final_v5_multi.py
# Same extraction logic as v4, but now loops over MULTIPLE listing URLs
# and writes one combined CSV (with a source_url column) at the end.
# Usage: python scraping_links_final_v5_multi.py

from playwright.sync_api import sync_playwright, TimeoutError
import time, csv, os, re, urllib.parse
import pandas as pd

# =====================================================================================================
# STEALTH CHROME  (SeleniumBase "Stealthy Playwright Mode")  +  CLOUDFLARE CHALLENGE HELPER
# -----------------------------------------------------------------------------------------------------
# pip install seleniumbase playwright        (no "playwright install" needed - your real Chrome is used)
# This block is the ONLY addition to the script (plus the launch line and handle_challenge(...) calls
# right after page loads).  Playwright now attaches to your real, installed Chrome via connect_over_cdp().
#
# Cloudflare: handle_challenge(page) checks for a Cloudflare / Turnstile screen after a page load.
#   1) it first gives the challenge a few seconds to clear by itself,
#   2) then tries SeleniumBase's solve_captcha() (clicks the Turnstile checkbox),
#   3) if it is still there it WAITS for YOU: click the checkbox in the Chrome window and the script
#      continues automatically as soon as the challenge disappears (no Enter key needed).
# =====================================================================================================
from seleniumbase import sb_cdp

try:
    _STEALTH_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _STEALTH_BASE_DIR = os.getcwd()

STEALTH_CHROME_OPTIONS = {
    # Persistent Chrome profile: keeps the Cloudflare "cf_clearance" cookie between launches (and between
    # your scripts if they sit in the same folder). Delete the folder to reset.
    "user_data_dir": os.path.join(_STEALTH_BASE_DIR, "stealth_chrome_profile"),
    # If Chrome is not found automatically, uncomment and set the path:
    # "binary_location": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # "proxy": "user:pass@host:port",
    # "locale": "pt-BR",
}

CF_GRACE_SECONDS = 6            # let the challenge clear on its own before touching it
CF_AUTO_SOLVE_TRIES = 2         # how many times to try solve_captcha() automatically
CF_MANUAL_WAIT_SECONDS = 300    # how long to wait for you to click it yourself

_ACTIVE_STEALTH = {}

_CF_DETECT_JS = """() => {
  const title = (document.title || '').toLowerCase();
  const body = ((document.body && document.body.innerText) || '').slice(0, 3000).toLowerCase();
  const sels = ['#challenge-form', '#cf-challenge-running', '#challenge-running', '#challenge-stage',
                '#cf-wrapper', '#turnstile-wrapper', '.cf-turnstile',
                'iframe[src*="challenges.cloudflare.com"]'];
  if (sels.some(s => document.querySelector(s))) return true;
  if (['just a moment', 'attention required', 'um momento', 'verificação de segurança',
       'security check'].some(k => title.includes(k))) return true;
  return ['verify you are human', 'verifying you are human', 'checking your browser',
          'verificando se você é humano', 'verifique se você é humano',
          'needs to review the security of your connection'].some(k => body.includes(k));
}"""


def _stealth_launch_kwargs(headless, extra):
    opts = dict(STEALTH_CHROME_OPTIONS)
    opts.update({k: v for k, v in (extra or {}).items() if v is not None})
    if "binary_location" in opts:                       # friendly aliases -> SeleniumBase names
        opts["browser_executable_path"] = opts.pop("binary_location")
    if "locale" in opts:
        opts["lang"] = opts.pop("locale")
    if headless:
        opts["headless"] = True
    return opts


def _same_page(url_a, url_b):
    return (url_a or "").split("#")[0].rstrip("/") == (url_b or "").split("#")[0].rstrip("/")


def _wait_chrome_exit(user_data_dir, timeout=10):
    """After quitting, wait until no Chrome process still holds the profile folder, so the next launch
    (which reuses the same profile) never collides with a Chrome that is still shutting down."""
    if not user_data_dir:
        return
    try:
        import psutil
    except ImportError:
        time.sleep(2)
        return
    key = os.path.abspath(user_data_dir).lower()
    end = time.time() + timeout
    while time.time() < end:
        alive = False
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(proc.info["cmdline"] or []).lower()
                if "--user-data-dir" in cmd and key in cmd:
                    alive = True
                    break
            except Exception:
                continue
        if not alive:
            return
        time.sleep(0.3)


class StealthChromeSync:
    """Sync format: real Chrome via sb_cdp + Playwright sync API (connect_over_cdp).
    Use:  with StealthChromeSync(slow_mo=30) as browser:  page = browser.new_page() ... browser.close()"""

    def __init__(self, headless=False, slow_mo=None, **launch_options):
        self._closed = False
        self._claimed_default = False
        self._free_pages = []
        self.sb = None
        self.playwright = None
        self.browser = None
        launch_kwargs = _stealth_launch_kwargs(headless, launch_options)
        self._user_data_dir = launch_kwargs.get("user_data_dir")
        try:
            self.sb = sb_cdp.Chrome(**launch_kwargs)
            self.endpoint_url = self.sb.get_endpoint_url()
            self.playwright = sync_playwright().start()
            kw = {"slow_mo": slow_mo} if slow_mo else {}
            self.browser = self.playwright.chromium.connect_over_cdp(self.endpoint_url, **kw)
            self.context = self.browser.contexts[0]                 # default context keeps the stealth setup
            self.default_page = self.context.pages[0] if self.context.pages else self.context.new_page()
            _ACTIVE_STEALTH["browser"] = self
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def new_page(self):
        """Reuses the first / any blanked tab, otherwise opens a new tab in the same (stealth) context."""
        if self._free_pages:
            return self._free_pages.pop()
        if not self._claimed_default:
            self._claimed_default = True
            return self.default_page
        return self.context.new_page()

    def close_page(self, page):
        """Closing the LAST tab can make real Chrome exit, so the last tab is blanked and reused instead."""
        try:
            if len(self.context.pages) <= 1:
                page.goto("about:blank")
                if page not in self._free_pages:
                    self._free_pages.append(page)
            else:
                page.close()
        except Exception:
            pass

    # ------------------------------ Cloudflare ------------------------------
    def _challenge_present(self, page):
        try:
            return bool(page.evaluate(_CF_DETECT_JS))
        except Exception:
            return False

    def _try_solve(self, page):
        try:
            for tab in self.sb.get_tabs():
                if _same_page(getattr(tab, "url", ""), page.url):
                    self.sb.switch_to_tab(tab)
                    break
        except Exception:
            pass
        self.sb.solve_captcha()

    def handle_challenge(self, page, label=""):
        """Returns True if a challenge was seen (and cleared), False if none / not cleared."""
        if not self._challenge_present(page):
            return False
        print(f"🛡️ Cloudflare challenge detected {label}- trying to clear it ...")
        page.wait_for_timeout(CF_GRACE_SECONDS * 1000)
        cleared = not self._challenge_present(page)
        tries = 0
        while not cleared and tries < CF_AUTO_SOLVE_TRIES:
            tries += 1
            try:
                self._try_solve(page)
            except Exception as e:
                print(f"   solve_captcha attempt {tries} failed: {e}")
            page.wait_for_timeout(4000)
            cleared = not self._challenge_present(page)
        if not cleared:
            print("\a👉 Please click the Cloudflare checkbox in the Chrome window. "
                  f"Waiting up to {CF_MANUAL_WAIT_SECONDS}s - the script continues by itself once it clears.")
            waited = 0
            while waited < CF_MANUAL_WAIT_SECONDS:
                page.wait_for_timeout(2000)
                waited += 2
                if not self._challenge_present(page):
                    cleared = True
                    break
        if cleared:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            print("✅ Cloudflare challenge cleared.")
            return True
        print("⚠️ Cloudflare challenge still present - continuing anyway.")
        return False

    # -------------------------------- cleanup --------------------------------
    def close(self):
        """Disconnect Playwright, then quit Chrome. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if _ACTIVE_STEALTH.get("browser") is self:
            _ACTIVE_STEALTH.pop("browser", None)
        for step in (lambda: self.browser.close(),
                     lambda: self.playwright.stop(),
                     lambda: self.sb.quit()):
            try:
                step()
            except Exception:
                pass
        _wait_chrome_exit(self._user_data_dir)


def handle_challenge(page, label=""):
    """Module-level shortcut: check the page for a Cloudflare challenge using the active stealth browser."""
    b = _ACTIVE_STEALTH.get("browser")
    return b.handle_challenge(page, label) if b else False

# =====================================================================================================


# -------------------------
# Configuration
# -------------------------
# Add as many listing URLs as you need here. Each one is processed exactly
# the way the single-URL script processed START_URL.
START_URLS = [
    "https://consultas.anvisa.gov.br/#/certificadosdeboaspraticas-medicamento/c/?cnpjSolicitante=03978166000175&tipoCertificado=2",
    "https://consultas.anvisa.gov.br/#/certificadosdeboaspraticas-medicamento/c/?cnpjSolicitante=03978166000175&tipoCertificado=1&internacional=true&status=0",
    "https://consultas.anvisa.gov.br/#/certificadosdeboaspraticas/c/?cnpjSolicitante=03978166000175&tipoCertificado=1&status=0",
]

PER_PAGE_50 = '.ng-table-counts button:has-text("50")'
PAGE_NUMBER_LINK = 'ul.pagination a:has-text("{}")'
NEXT_ARROW = 'ul.pagination a:has-text("»")'
ACTIVE_PAGE_LOCATOR = 'ul.pagination li.active a, ul.pagination li.active span'
ROW_SELECTOR = "tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope"

OUT_CSV = "gmp_anvisa_list_new_final.csv"
OUT_XLSX = "gmp_anvisa_list_new_final.xlsx"

SHORT_WAIT = 0.6
NAV_POLL = 0.35
NAV_TRIES = 18
BACK_WAIT = 0.8
ROW_WAIT_TIMEOUT = 18
MAX_ADVANCES = 200
LONG_WAIT = 1.2

# -------------------------
# JS snippets executed in page
# -------------------------
INVOKE_DETAIL_JS = """(idx) => {
  try {
    const rows = document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope');
    if(!rows || idx<0 || idx>=rows.length) return {status:'row-missing'};
    const el = rows[idx];
    if(window.angular && window.angular.element){
      const ngEl = window.angular.element(el);
      const scope = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope());
      if(scope && typeof scope.detail === 'function'){
        try {
          scope.$apply(function(){ scope.detail(scope.produto || scope.empresa || scope.item || scope); });
          return {status:'invoked'};
        } catch(e){ return {status:'invoked-except', error:String(e)} }
      }
    }
    const clickable = el.querySelector('td[ng-click^="detail"], td[ng-click]');
    try {
      if(clickable){ clickable.click(); return {status:'clicked-cell'} }
      el.click();
      return {status:'clicked-row'};
    } catch(e) {
      try { el.dispatchEvent(new MouseEvent('click',{bubbles:true})); return {status:'dispatched'} } catch(e2){}
      return {status:'no-action', error:String(e)};
    }
  } catch(e) { return {status:'error', error:String(e)} }
}"""

READ_SCOPE_JS = """(idx) => {
  try {
    const rows = document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope');
    if(!rows || idx<0 || idx>=rows.length) return null;
    const el = rows[idx];
    if(!(window.angular && window.angular.element)) return null;
    const ngEl = window.angular.element(el);
    const s = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope());
    if(!s) return null;
    const out = {};
    const cand = (obj, keys) => {
      for(const k of keys){ if(obj && (k in obj) && obj[k] != null) return obj[k]; }
      return null;
    };
    const empresa = s.empresa || s.produto || s.item || s;
    out.idCertificado = cand(empresa, ['idCertificado','id','codigo','codigoCertificado','numero','numeroCertificado','nCertificado','certificadoId','id_certificado']);
    out.produto_id = cand(s.produto || {}, ['id','codigo','codigoExterno','codigoProduto']);
    out.processo_numero = cand(s.processo || {}, ['numero','id']);
    out.nome = cand(empresa, ['nome','nomeComercial','descricao']);
    out._keys = Object.keys(empresa||{}).slice(0,50);
    return out;
  } catch(e) { return null; }
}"""

# -------------------------
# Helpers
# -------------------------
def debug(msg):
    print("[DEBUG]", msg)

def wait_for_render(page, timeout=8000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except TimeoutError:
        pass
    time.sleep(SHORT_WAIT)

def try_click_selector(page, selector, timeout=3000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
        el = page.query_selector(selector)
        if not el:
            return False
        box = el.bounding_box()
        if box is None:
            page.evaluate("(sel)=>{ const e=document.querySelector(sel); if(e) e.click(); }", selector)
            return True
        el.click(timeout=5000)
        return True
    except Exception as e:
        debug(f"try_click_selector({selector}) failed: {e}")
        try:
            page.evaluate("(sel)=>{ const e=document.querySelector(sel); if(e) e.click(); }", selector)
            return True
        except Exception:
            return False

def _extract_fragment_query(listing_url):
    """Return the fragment's query string including the leading '?' if present."""
    try:
        if '#' not in listing_url:
            return ''
        frag = listing_url.split('#', 1)[1]
        if '?' in frag:
            return frag[frag.index('?'):]
    except Exception:
        pass
    return ''

def _extract_entity_path(listing_url):
    """Return the entity path segment used in this listing's fragment,
    e.g. 'certificadosdeboaspraticas-medicamento' or 'certificadosdeboaspraticas'.
    Falls back to 'certificadosdeboaspraticas-medicamento' if it can't be determined."""
    try:
        if '#' not in listing_url:
            return 'certificadosdeboaspraticas-medicamento'
        frag = listing_url.split('#', 1)[1].lstrip('/')
        path_part = frag.split('?')[0]
        segs = [s for s in path_part.split('/') if s]
        if segs:
            return segs[0]
    except Exception:
        pass
    return 'certificadosdeboaspraticas-medicamento'

def _build_cert_url(listing_url, cert_id):
    """Construct final URL in exact format required and preserve fragment query.
    Uses the SAME entity path segment as the listing_url it came from, so a
    'certificadosdeboaspraticas' source doesn't get mislabeled as
    'certificadosdeboaspraticas-medicamento' (or vice versa)."""
    base = listing_url.split('#')[0].rstrip('/')
    frag_q = _extract_fragment_query(listing_url)
    entity_path = _extract_entity_path(listing_url)
    return f"{base}/#/{entity_path}/{cert_id}/{frag_q}"

def normalize_link(link, listing_url):
    if not link:
        return ""
    link = link.strip()
    m = re.match(r'(https?://[^/]+)#(.*)', link)
    if m:
        return m.group(1) + "/#" + m.group(2)
    if link.startswith("#"):
        base = listing_url.split("#")[0].rstrip("/")
        if link.startswith("#/"):
            return base + "/" + link
        return base + "/#" + link.lstrip("#")
    if link.startswith("/"):
        base = listing_url.split("#")[0].rstrip("/")
        return base + link
    base = listing_url.split("#")[0].rstrip("/")
    return urllib.parse.urljoin(base + "/", link)

def wait_for_url_change(page, original_url, contains=None, attempts=NAV_TRIES, poll=NAV_POLL):
    for _ in range(attempts):
        try:
            cur = page.url
        except Exception:
            cur = ""
        if cur and cur != original_url:
            if contains:
                if contains in cur:
                    return cur
            else:
                return cur
        time.sleep(poll)
    return ""

# -------------------------
# Anchor / modal scanning
# -------------------------
def find_any_anchor_with_certificados(page, listing_url):
    try:
        anchors = page.query_selector_all("a[href*='certificadosdeboaspraticas-medicamento/'], a[href*='certificadosdeboaspraticas-medicamento?']")
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            if not href:
                continue
            nh = normalize_link(href, listing_url)
            if re.search(r'/certificadosdeboaspraticas-medicamento/\d+', nh) or re.search(r'[?&](numero|id|nCertificado)=\d+', nh):
                debug(f"find_any_anchor: strict anchor -> {nh}")
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    return _build_cert_url(listing_url, m.group(1))
        anchors2 = page.query_selector_all("a[href*='certificadosdeboaspraticas'], a[href*='/certificados']")
        for a in anchors2:
            href = (a.get_attribute("href") or "").strip()
            if not href:
                continue
            nh = normalize_link(href, listing_url)
            if re.search(r'\d{5,12}', nh):
                debug(f"find_any_anchor: broad anchor -> {nh}")
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    return _build_cert_url(listing_url, m.group(1))
    except Exception as e:
        debug(f"find_any_anchor error: {e}")
    return ""

def wait_for_modal_anchor(page, listing_url, timeout=6.0):
    try:
        msel = ".modal-content, .modal, .ngdialog, .ui-dialog, .modal-body, .modal .modal-body"
        modal = page.wait_for_selector(msel, timeout=int(timeout*1000))
        if not modal:
            return ""
        try:
            a = modal.query_selector("a[href*='certificadosdeboaspraticas-medicamento/'], a[href*='certificadosdeboaspraticas-medicamento?']")
            if a:
                href = (a.get_attribute("href") or "").strip()
                nh = normalize_link(href, listing_url)
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    debug(f"wait_for_modal_anchor: modal-anchor strict -> {nh}")
                    return _build_cert_url(listing_url, m.group(1))
            a2 = modal.query_selector_all("a[href]")
            for a3 in a2:
                href = (a3.get_attribute("href") or "").strip()
                if not href:
                    continue
                nh = normalize_link(href, listing_url)
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    debug(f"wait_for_modal_anchor: modal-anchor-digit -> {nh}")
                    return _build_cert_url(listing_url, m.group(1))
        except Exception:
            pass
        text = modal.text_content() or ""
        m = re.search(r'(\d{5,12})', re.sub(r'[\.\-\s]', '', text))
        if m:
            debug(f"wait_for_modal_anchor: modal-text -> {m.group(1)}")
            return _build_cert_url(listing_url, m.group(1))
    except Exception as e:
        debug(f"wait_for_modal_anchor: {e}")
    return ""

def construct_certificado_url_from_scope(listing_url, scope_info):
    if not scope_info:
        return ""
    keys = ['idCertificado','id','codigo','codigoCertificado','numeroCertificado','nCertificado','certificadoId','numero','produto_id']
    for k in keys:
        v = scope_info.get(k) if isinstance(scope_info, dict) else None
        if v:
            return _build_cert_url(listing_url, v)
    try:
        s = str(scope_info)
        m = re.search(r'(\d{6,12})', s)
        if m:
            return _build_cert_url(listing_url, m.group(1))
    except Exception:
        pass
    return ""

# -------------------------
# Row extraction logic
# -------------------------
def return_and_wait_rows(page, expected_count=1, timeout_s=12):
    try:
        page.evaluate("window.history.back()")
    except Exception:
        try:
            page.go_back()
        except Exception:
            pass
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            rows = page.query_selector_all(ROW_SELECTOR)
            if rows and (len(rows) >= expected_count or len(rows) > 0):
                time.sleep(0.45)
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False

def extract_link_for_row_certificados(page, listing_url, idx):
    try:
        pre_scope = page.evaluate(READ_SCOPE_JS, idx)
        if pre_scope:
            debug(f"certificados: pre-click scope_info -> {pre_scope}")
            fallback = construct_certificado_url_from_scope(listing_url, pre_scope or {})
            if fallback:
                debug(f"certificados: pre-click scope-fallback -> {fallback}")
                return fallback
    except Exception as e:
        debug(f"certificados: pre-click scope eval error: {e}")

    try:
        page.evaluate("(idx)=>{ const rows=document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope'); if(rows && idx < rows.length) rows[idx].scrollIntoView({block:'center'}); }", idx)
    except Exception:
        pass

    rows = page.query_selector_all(ROW_SELECTOR)
    if idx >= len(rows):
        debug("certificados: index out of range")
        return ""

    row = rows[idx]
    try:
        try:
            row.click()
        except Exception:
            page.evaluate("(el)=>el.click()", row)
    except Exception as e:
        debug(f"certificados: row.click failed: {e}")
        return ""

    time.sleep(0.25)

    new_url = wait_for_url_change(page, listing_url, contains="certificadosdeboaspraticas", attempts=12, poll=0.3)
    if new_url:
        debug(f"certificados: navigation detected -> {new_url}")
        try:
            page.go_back()
        except Exception:
            try:
                page.evaluate("window.history.back()")
            except Exception:
                pass
        m = re.search(r'(\d{5,12})', new_url)
        if m:
            return _build_cert_url(listing_url, m.group(1))
        return normalize_link(new_url, listing_url)

    modal_href = wait_for_modal_anchor(page, listing_url, timeout=5.5)
    if modal_href:
        debug(f"certificados: modal anchor/text -> {modal_href}")
        try:
            close_btn = page.query_selector(".modal .close, .modal button.close, .modal .btn-close")
            if close_btn:
                try:
                    close_btn.click()
                except Exception:
                    page.evaluate("(el)=>el.click()", close_btn)
            else:
                page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            return_and_wait_rows(page, expected_count=1, timeout_s=6)
        except Exception:
            pass
        return modal_href

    anchor = find_any_anchor_with_certificados(page, listing_url)
    if anchor:
        debug(f"certificados: global anchor found -> {anchor}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return anchor

    try:
        scope_info = page.evaluate(READ_SCOPE_JS, idx)
        debug(f"certificados: scope_info -> {scope_info}")
    except Exception as e:
        debug(f"certificados: scope eval error: {e}")
        scope_info = None
    fallback = construct_certificado_url_from_scope(listing_url, scope_info or {})
    if fallback:
        debug(f"certificados: scope-fallback -> {fallback}")
        try:
            page.evaluate("window.history.back()")
        except Exception:
            pass
        return fallback

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    returned = return_and_wait_rows(page, expected_count=1, timeout_s=6)
    if not returned:
        debug("certificados: could not recover listing after click")
    return ""

def extract_link_for_row_generic(page, current_page, idx):
    listing_url = page.url
    rows = page.query_selector_all(ROW_SELECTOR)
    if idx >= len(rows):
        return ""

    if "certificadosdeboaspraticas" in listing_url or "certificados" in listing_url:
        return extract_link_for_row_certificados(page, listing_url, idx)

    try:
        page.evaluate("(idx)=>{ const rows=document.querySelectorAll('tbody tr[ng-repeat], tbody tr.ng-scope, tbody tr.linha_certificado'); if(rows && idx < rows.length) rows[idx].scrollIntoView({block:'center'}); }", idx)
    except Exception:
        pass
    try:
        page.evaluate(INVOKE_DETAIL_JS, idx)
    except Exception:
        pass
    new_url = wait_for_url_change(page, listing_url, contains="medicamentos", attempts=12, poll=0.35)
    if new_url:
        try:
            page.go_back()
        except Exception:
            try:
                page.evaluate("window.history.back()")
            except Exception:
                pass
        return normalize_link(new_url, listing_url)
    try:
        scope_info = page.evaluate(READ_SCOPE_JS, idx)
    except Exception:
        scope_info = None
    if scope_info and isinstance(scope_info, dict):
        proc = scope_info.get("processo_numero") or scope_info.get("produto_id") or scope_info.get("id")
        if proc:
            base = listing_url.split("#")[0].rstrip("/")
            frag_q = _extract_fragment_query(listing_url)
            return f"{base}/#/medicamentos/{urllib.parse.quote(str(proc))}/{frag_q}"
    return ""

# -------------------------
# Per-listing-URL processing (this is the same logic the old main() ran once,
# now wrapped so it can run for each URL in START_URLS)
# -------------------------
def process_listing_url(page, listing_url):
    """Runs the full paginated extraction for a single listing URL.
    Returns a set of unique certificate links found for this URL."""
    collected_for_this_url = set()

    print("Opening:", listing_url)
    page.goto(listing_url, wait_until="domcontentloaded")
    handle_challenge(page)
    wait_for_render(page)

    print("Attempting to click '50' per-page button...")
    if try_click_selector(page, PER_PAGE_50):
        print("Clicked 50 — waiting for rows/pagination to render.")
        wait_for_render(page)
    else:
        print("50-button not available or click failed — continuing without.")

    current = None
    try:
        el = page.query_selector(ACTIVE_PAGE_LOCATOR)
        if el:
            txt = (el.text_content() or "").strip()
            current = int(txt) if txt.isdigit() else 1
    except Exception:
        current = 1
    if not current:
        current = 1

    safety = 0
    while safety < MAX_ADVANCES:
        print(f"--- PROCESSING LISTING PAGE {current} ---")
        wait_for_render(page)
        rows = page.query_selector_all(ROW_SELECTOR)
        total_rows = len(rows)
        print(f"Found {total_rows} rows on page {current}.")

        first_block = min(10, total_rows)
        print(f"Extracting first {first_block} rows...")
        for i in range(first_block):
            print(f" Processing row #{i+1} ...")
            try:
                link = extract_link_for_row_generic(page, current, i)
            except Exception as e:
                link = ""
                debug(f"row#{i+1} exception: {e}")
            if link:
                if link not in collected_for_this_url:
                    collected_for_this_url.add(link)
                    print(f"  row#{i+1}: found -> {link}")
                else:
                    print(f"  row#{i+1}: duplicate -> {link}")
            else:
                print(f"  row#{i+1}: no link captured")

        if total_rows > 10:
            print("Extracting indexes 11..50 with click-50-before-each...")
            rows_now = page.query_selector_all(ROW_SELECTOR)
            max_index = min(len(rows_now), 50) - 1
            for idx in range(10, max_index + 1):
                print(f" Preparing index {idx+1} ... clicking 50 control then extracting")
                try:
                    try_click_selector(page, PER_PAGE_50)
                except Exception:
                    pass
                time.sleep(0.35)
                try:
                    link = extract_link_for_row_generic(page, current, idx)
                except Exception as e:
                    link = ""
                    debug(f"index {idx+1} extract exception: {e}")
                try:
                    try_click_selector(page, PER_PAGE_50)
                except Exception:
                    pass
                if link:
                    if link not in collected_for_this_url:
                        collected_for_this_url.add(link)
                        print(f"  row#{idx+1}: found -> {link}")
                    else:
                        print(f"  row#{idx+1}: duplicate -> {link}")
                else:
                    print(f"  row#{idx+1}: no link captured")

        next_page = current + 1
        print(f"Attempting to advance to page {next_page} ...")
        clicked = try_click_selector(page, PAGE_NUMBER_LINK.format(next_page))
        if clicked:
            wait_for_render(page)
            try:
                el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
            except Exception:
                new_active = None
            if new_active is None or new_active == current:
                time.sleep(LONG_WAIT)
                try:
                    el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                    new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
                except Exception:
                    new_active = None
            if new_active == next_page:
                current = new_active
                safety += 1
                continue
            else:
                debug(f"Clicked numeric but active={new_active}")
        if try_click_selector(page, NEXT_ARROW):
            wait_for_render(page)
            try:
                el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
            except Exception:
                new_active = None
            if new_active and new_active > current:
                current = new_active
                safety += 1
                continue
            else:
                print("Next arrow clicked but didn't advance. Ending this URL.")
                break
        print("No pagination advance possible — ending this URL.")
        break

    return collected_for_this_url

# -------------------------
# Main flow — loops over START_URLS, merges results, writes one combined file
# -------------------------
def main():
    if os.path.exists(OUT_CSV):
        os.remove(OUT_CSV)

    # rows: list of (source_url, link)
    all_rows = []
    seen_links = set()

    with StealthChromeSync() as browser:
        page = browser.new_page()

        for url_idx, listing_url in enumerate(START_URLS, start=1):
            print(f"\n================ SOURCE {url_idx}/{len(START_URLS)} ================")
            try:
                links_for_url = process_listing_url(page, listing_url)
            except Exception as e:
                debug(f"process_listing_url failed for {listing_url}: {e}")
                links_for_url = set()

            for link in sorted(links_for_url):
                if link not in seen_links:
                    seen_links.add(link)
                    all_rows.append((listing_url, link))

        browser.close()

    # write combined CSV
    print(f"\nCollected {len(all_rows)} unique links across {len(START_URLS)} source(s). Writing to {OUT_CSV} ...")
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["GMP Anvisa URL"])
        for source_url, link in all_rows:
            writer.writerow([link])

    # also write an Excel version if openpyxl is available
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "links"
        ws.append(["GMP Anvisa URL"])
        for source_url, link in all_rows:
            ws.append([link])
        wb.save(OUT_XLSX)
        print(f"Also wrote {OUT_XLSX}")
    except ImportError:
        print("openpyxl not installed — skipped .xlsx output (CSV still written). "
              "Install with: pip install openpyxl --break-system-packages")

    print("Done.")
    
    df_gmp_links = pd.read_csv("gmp_anvisa_list_new_final_new.csv", dtype=str)
    
    df_gmp_links['ID'] = ""
    
    df_gmp_links_old = pd.read_csv("gmp_anvisa_list_new_final_old.csv", dtype=str)
    
    for indx in range(df_gmp_links_old.shape[0]):
        
        gmp_link_old = str(df_gmp_links_old.loc[indx, "GMP Anvisa URL"])
        
        if "certificadosdeboaspraticas-medicamento" in gmp_link_old:            
            certificate_id_old = gmp_link_old.split("certificadosdeboaspraticas-medicamento/")[1].split("/")[0]
        if "certificadosdeboaspraticas/" in gmp_link_old:            
            certificate_id_old = gmp_link_old.split("certificadosdeboaspraticas/")[1].split("/")[0]

            
        for idx in range(df_gmp_links.shape[0]):
            
            gmp_link_new = str(df_gmp_links.loc[idx, "GMP Anvisa URL"])
            
            if "certificadosdeboaspraticas-medicamento" in gmp_link_new:                
                certificate_id_new = gmp_link_new.split("certificadosdeboaspraticas-medicamento/")[1].split("/")[0]
            if "certificadosdeboaspraticas/" in gmp_link_new:                
                certificate_id_new = gmp_link_new.split("certificadosdeboaspraticas/")[1].split("/")[0]            
            
            if certificate_id_old == certificate_id_new:
                
                df_gmp_links.loc[idx, 'ID'] = str(df_gmp_links_old.loc[indx, "ID"])
                
        
    df_gmp_links.to_csv("gmp_anvisa_list_new_final_new.csv", index=False)
    
    

if __name__ == "__main__":
    main()