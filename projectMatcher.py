import sqlite3
from datetime import datetime
import pandas as pd
import time
import random
import math
import json
import re
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import os

load_dotenv()


FREELANCERMAP_USERNAME = os.getenv('FREELANCERMAP_USERNAME')
FREELANCERMAP_PASSWORD = os.getenv('FREELANCERMAP_PASSWORD')

MAX_PAGES = 100
MIN_SCORE = 35

PROFILE = {
    'skills': [
        'Python', 'JavaScript', 'React', 'Vue', 'MySQL',
        'HTML', 'CSS', 'PHP', 'GCP', 'AWS', 'Cloud',
        'AI', 'Frontend', 'Vue.js', 'JS'
    ],
    'preferred_keywords': [
        'Webentwicklung', 'Backend', 'Frontend', 'Fullstack',
        'API', 'Wordpress', 'GCP', 'AWS', 'Cloud', 'AI', 'OpenAI'
    ],
    'excluded_keywords': ['SAP', 'Drupal']
}


class FreelancermapDatabase:
    def __init__(self, db_path="freelancermap.db"):
        self.conn = sqlite3.connect(db_path)
        self.create_tables()

    def create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                link TEXT UNIQUE,
                company TEXT,
                description TEXT,
                keywords TEXT,
                created_date DATETIME,
                is_top_project BOOLEAN,
                is_endcustomer BOOLEAN,
                scrape_date DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                title TEXT,
                link TEXT,
                company TEXT,
                description TEXT,
                keywords TEXT,
                created_date DATETIME,
                is_top_project BOOLEAN,
                is_endcustomer BOOLEAN,
                match_score FLOAT,
                match_debug TEXT,
                match_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id)
            )
        """)
        self.conn.commit()


class FreelancermapScraper:
    _UA = (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    )

    def __init__(self, db, username, password, max_pages=2, search_config=None):
        self.db = db
        self.base_url = "https://www.freelancermap.de"
        self.username = username
        self.password = password
        self.max_pages = max_pages
        self.search_config = search_config or {}
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        # Interactive (non-headless) browser for manual fallback
        self._ipw = None
        self._ibrowser = None
        self._ictx = None
        self._ipage = None
        self._interactive_mode = False  # once True, use interactive for all pages

    def _ensure_browser(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._ctx = self._browser.new_context(
                locale='de-DE', user_agent=self._UA)
            self._page = self._ctx.new_page()

    def _ensure_interactive_browser(self):
        """Open or reuse a visible (non-headless) browser with the same session."""
        if self._ipage and not self._ipage.is_closed():
            return True
        try:
            cookies = self._ctx.cookies() if self._ctx else []
        except Exception:
            cookies = []
        try:
            from playwright.sync_api import sync_playwright
            self._ipw = sync_playwright().start()
            self._ibrowser = self._ipw.chromium.launch(
                headless=False, args=['--window-size=1280,900'])
            self._ictx = self._ibrowser.new_context(
                locale='de-DE', user_agent=self._UA,
                viewport={'width': 1280, 'height': 900})
            self._ictx.add_cookies(cookies)
            self._ipage = self._ictx.new_page()
            return True
        except Exception as e:
            print(f"Interaktiver Browser konnte nicht geöffnet werden: {e}")
            return False

    def _close_interactive_browser(self):
        for attr, obj in (
            ('_ipage', None), ('_ictx', None),
            ('_ibrowser', 'close'), ('_ipw', 'stop'),
        ):
            try:
                o = getattr(self, attr)
                if o:
                    if obj:
                        getattr(o, obj)()
            except Exception:
                pass
            setattr(self, attr, None)
        self._interactive_mode = False

    def close(self):
        self._close_interactive_browser()
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None
        self._ctx = None
        self._page = None

    def __del__(self):
        self.close()

    def login(self):
        try:
            print("Starte Login-Prozess...")
            self._ensure_browser()

            print("Lade Login-Seite...")
            self._page.goto(f"{self.base_url}/login", wait_until='networkidle', timeout=30000)

            print("Führe Login durch...")
            self._page.fill('input[name="login"]', self.username)
            self._page.fill('input[name="password"]', self.password)

            with self._page.expect_navigation(wait_until='domcontentloaded', timeout=15000):
                self._page.press('input[name="password"]', 'Enter')

            print(f"Response URL: {self._page.url}")

            time.sleep(1)
            self._page.goto(f"{self.base_url}/mein_account.html", wait_until='domcontentloaded', timeout=15000)

            content = self._page.content()
            if any(x in content for x in ['Mein Konto', 'Profil', 'Logout', 'Abmelden']):
                print("Login erfolgreich!")
                return True

            print("Login fehlgeschlagen: Keine Login-Indikatoren gefunden")
            return False

        except Exception as e:
            print(f"Fehler beim Login: {str(e)}")
            return False

    def get_page_url(self, page_number):
        cfg = self.search_config

        direct_url = cfg.get('direct_url', '').strip()
        if direct_url:
            # Replace existing pagenr= or append it
            if re.search(r'[?&]pagenr=\d+', direct_url):
                url = re.sub(r'(pagenr=)\d+', rf'\g<1>{page_number}', direct_url)
            else:
                sep = '&' if '?' in direct_url else '?'
                url = f"{direct_url}{sep}pagenr={page_number}"
            # Ensure absolute URL
            if url.startswith('/'):
                url = f"{self.base_url}{url}"
            return url

        from urllib.parse import quote
        parts = []

        query = cfg.get('search_query', '').strip()
        if query:
            parts.append(f"query={quote(query)}")

        categories = cfg.get('categories', [])
        if categories:
            parts.append(f"categories={','.join(str(c) for c in categories)}")

        contract_types = cfg.get('contract_types', ['contracting'])
        for i, ct in enumerate(contract_types):
            parts.append(f"projectContractTypes[{i}]={ct}")

        remote = cfg.get('remote_percent', None)
        if remote is not None:
            parts.append(f"remoteInPercent[0]={remote}")

        countries = cfg.get('countries', [1, 2, 3])
        for c in countries:
            parts.append(f"countries[]={c}")

        if cfg.get('endcustomer_only', False):
            parts.append("endcustomer=1")

        parts.append(f"sort={cfg.get('sort', 1)}")
        parts.append(f"pagenr={page_number}")

        return f"{self.base_url}/projekte?{'&'.join(parts)}"

    def get_page(self, page_number):
        url = self.get_page_url(page_number)
        print(f"\nAbrufen von Seite {page_number}")
        print(f"URL: {url}")

        # Separate buckets: responses whose URL contains pagenr=N (page-specific)
        # vs. everything else (recommendations, featured, etc.)
        page_specific = []
        other_captured = []

        def _extract_items(data):
            if isinstance(data, list) and data and isinstance(data[0], dict) and 'title' in data[0]:
                return list(data)
            if isinstance(data, dict):
                for key in ('initialTopResults', 'initialResults'):
                    pass  # handled below to avoid break
                items = []
                for key in ('initialTopResults', 'initialResults'):
                    if key in data and isinstance(data[key], list) and data[key]:
                        items.extend(data[key])
                if items:
                    return items
                for key in ('projects', 'hits', 'items', 'results', 'data'):
                    if key in data and isinstance(data[key], list) and data[key]:
                        return list(data[key])
            return []

        def _on_response(response):
            if response.status != 200:
                return
            ct = response.headers.get('content-type', '')
            if 'json' not in ct:
                return
            try:
                data = response.json()
                items = _extract_items(data)
                if not items:
                    return
                rurl = response.url
                label = f"{rurl[:120]} → {len(items)} Projekte"
                if f'pagenr={page_number}' in rurl:
                    page_specific.extend(items)
                    print(f"  [Seite {page_number}] API: {label}")
                else:
                    other_captured.extend(items)
                    print(f"  [Sonstige]    API: {label}")
            except Exception:
                pass

        try:
            self._page.on('response', _on_response)
            self._page.goto(url, wait_until='networkidle', timeout=30000)
            self._page.remove_listener('response', _on_response)

            content = self._page.content()

            if "Bitte melden Sie sich an" in content:
                print("Login-Session abgelaufen, versuche erneuten Login...")
                if self.login():
                    return self.get_page(page_number)
                return []

            soup = BeautifulSoup(content, 'html.parser')

            # 1. Beste Quelle: API-Response mit passender pagenr im URL
            if page_specific:
                print(f"Projekte via Seiten-spezifischer API: {len(page_specific)}")
                projects = [self._parse_json_project(p, is_top=False) for p in page_specific]
                return [p for p in projects if p]

            # 2. Script-Tag: nur zuverlässig wenn currentPage übereinstimmt
            projects, current_page, _ = self._extract_from_script_json(soup)
            if current_page is not None and current_page != page_number:
                print(f"Script-Tag zeigt Seite {current_page}, erwartet {page_number} – versuche Klick …")
                clicked_projects = (
                    self._interactive_get_projects(page_number)
                    if self._interactive_mode
                    else self._click_and_extract(page_number)
                )
                if clicked_projects:
                    return clicked_projects
            elif projects:
                print(f"Projekte via Script-Tag (SSR Seite {current_page}): {len(projects)}")
                return projects

            # 3. Sonstige API-Responses als Fallback
            if other_captured:
                print(f"Projekte via sonstige API-Antwort: {len(other_captured)}")
                projects = [self._parse_json_project(p, is_top=False) for p in other_captured]
                return [p for p in projects if p]

            # 4. React-gerendertes HTML (DOM nach Hydration)
            projects = self._extract_from_html(soup)
            if projects:
                print(f"Projekte via gerendertem HTML: {len(projects)}")
                return projects

            print("Keine Projekte auf dieser Seite gefunden!")
            return []

        except Exception as e:
            print(f"Fehler beim Laden der Seite {page_number}: {str(e)}")
            self._page.remove_listener('response', _on_response)
            return []

    def _js_click_next(self, pw_page, page_number):
        """Tries to JS-click the next/direct paginator button. Returns 'direct', 'next' or 'not-found'."""
        return pw_page.evaluate(f'''
            (function() {{
                var direct = document.querySelector('.paginator-item[data-page="{page_number}"]');
                if (!direct) {{
                    var links = document.querySelectorAll('a[href*="pagenr={page_number}"]');
                    direct = links.length ? links[0] : null;
                }}
                if (direct) {{ direct.click(); return "direct"; }}
                var next = document.querySelector('[aria-label="next-page"]')
                        || document.querySelector('.paginator-item.next')
                        || document.querySelector('a.next');
                if (next) {{ next.click(); return "next"; }}
                return "not-found";
            }})()
        ''')

    def _collect_from_responses(self, responses, page_number):
        """Parse raw response items into project dicts."""
        projects = [self._parse_json_project(p, is_top=False) for p in responses]
        return [p for p in projects if p]

    def _click_and_extract(self, page_number):
        """Try JS click in headless browser; fall back to interactive browser."""
        captured = []

        def _on_resp(response):
            if response.status != 200:
                return
            if 'json' not in response.headers.get('content-type', ''):
                return
            try:
                data = response.json()
                items = []
                if isinstance(data, list) and data and isinstance(data[0], dict) and 'title' in data[0]:
                    items = list(data)
                elif isinstance(data, dict):
                    for key in ('initialTopResults', 'initialResults'):
                        if key in data and isinstance(data[key], list) and data[key]:
                            items.extend(data[key])
                    if not items:
                        for key in ('projects', 'hits', 'items', 'results', 'data'):
                            if key in data and isinstance(data[key], list) and data[key]:
                                items = list(data[key])
                                break
                if items:
                    captured.extend(items)
            except Exception:
                pass

        try:
            self._page.on('response', _on_resp)
            result = self._js_click_next(self._page, page_number)
            print(f"  JS-Klick (headless): {result}")
            if result != 'not-found':
                self._page.wait_for_load_state('networkidle', timeout=15000)
            self._page.remove_listener('response', _on_resp)

            if captured:
                projects = self._collect_from_responses(captured, page_number)
                if projects:
                    print(f"  Projekte via headless Klick: {len(projects)}")
                    return projects
        except Exception as e:
            print(f"  Headless-Klick Fehler: {e}")
            try:
                self._page.remove_listener('response', _on_resp)
            except Exception:
                pass

        # JS click failed or returned nothing → open interactive browser
        print(f"  Öffne interaktiven Browser für Seite {page_number} …")
        return self._interactive_get_projects(page_number)

    def _interactive_get_projects(self, page_number):
        """Navigate in non-headless browser, try auto-click, else wait for user."""
        if not self._ensure_interactive_browser():
            return []

        url = self.get_page_url(page_number)
        page = self._ipage
        captured = []

        def _on_resp(response):
            if response.status != 200:
                return
            if 'json' not in response.headers.get('content-type', ''):
                return
            try:
                data = response.json()
                items = []
                if isinstance(data, list) and data and isinstance(data[0], dict) and 'title' in data[0]:
                    items = list(data)
                elif isinstance(data, dict):
                    for key in ('initialTopResults', 'initialResults'):
                        if key in data and isinstance(data[key], list) and data[key]:
                            items.extend(data[key])
                    if not items:
                        for key in ('projects', 'hits', 'items', 'results', 'data'):
                            if key in data and isinstance(data[key], list) and data[key]:
                                items = list(data[key])
                                break
                if items:
                    captured.extend(items)
            except Exception:
                pass

        try:
            page.on('response', _on_resp)
            page.goto(url, wait_until='networkidle', timeout=30000)
            baseline = len(captured)  # responses from initial page load

            # Try auto-click in the visible browser
            result = self._js_click_next(page, page_number)
            print(f"  JS-Klick (interaktiv): {result}")

            if result != 'not-found':
                # Auto-click worked – wait for data
                self._interactive_mode = True
                page.wait_for_load_state('networkidle', timeout=10000)
            else:
                # Show overlay and let user click manually
                page.evaluate(f'''
                    (function() {{
                        var old = document.getElementById("__si");
                        if (old) old.remove();
                        var d = document.createElement("div");
                        d.id = "__si";
                        d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999999;"
                            + "background:rgba(20,90,200,0.95);color:#fff;padding:14px 20px;"
                            + "font:bold 15px sans-serif;text-align:center;";
                        d.textContent = "Scraper wartet – Klicke den WEITER-Button für Seite {page_number}."
                            + " Fenster schließen = Seite überspringen.";
                        document.body.prepend(d);
                        document.querySelectorAll(
                            "[aria-label=\\"next-page\\"], .paginator-item.next"
                        ).forEach(function(el) {{
                            el.style.cssText += ";outline:4px solid red !important;"
                                + "background:#ffcc00 !important;opacity:1 !important;"
                                + "visibility:visible !important;";
                        }});
                    }})()
                ''')
                print(f"  Warte auf Benutzer-Klick für Seite {page_number} (Fenster schließen = überspringen) …")

                # Wait up to 2 minutes for new API response or window close
                import time
                deadline = time.time() + 120
                while not page.is_closed() and time.time() < deadline:
                    if len(captured) > baseline:
                        time.sleep(1.0)  # let remaining responses arrive
                        break
                    time.sleep(0.3)

                if page.is_closed():
                    print(f"  Browser geschlossen – Seite {page_number} übersprungen")
                    self._close_interactive_browser()
                    return []

                if len(captured) <= baseline:
                    print(f"  Timeout – Seite {page_number} übersprungen")
                    return []

                # User clicked → enable interactive mode for remaining pages
                self._interactive_mode = True
                print(f"  Benutzer hat geklickt – interaktiver Modus aktiv für alle weiteren Seiten")

            page.remove_listener('response', _on_resp)
            new_items = captured[baseline:]
            projects = self._collect_from_responses(new_items, page_number)
            print(f"  Interaktiver Browser: {len(projects)} Projekte (Seite {page_number})")
            return projects

        except Exception as e:
            print(f"  Fehler im interaktiven Browser: {e}")
            try:
                page.remove_listener('response', _on_resp)
            except Exception:
                pass
            return []

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _parse_script_tag(self, soup):
        """Return the raw JSON dict from the ProjectSearch script tag, or None."""
        script = soup.find(
            'script',
            class_='js-react-on-rails-component',
            attrs={'data-component-name': 'ProjectSearch'},
        )
        if not script or not script.string:
            return None
        try:
            return json.loads(script.string)
        except json.JSONDecodeError:
            return None

    def _extract_from_script_json(self, soup):
        """Parse projects from the React-on-Rails JSON script tag.
        Returns (projects, current_page, total_pages)."""
        data = self._parse_script_tag(soup)
        if not data:
            return [], None, None

        current_page = data.get('currentPage')

        # Total pages: highest pagenr= in the pagination HTML
        pagination_html = data.get('initialPagination', '')
        page_nums = [int(n) for n in re.findall(r'pagenr=(\d+)', pagination_html)]
        total_pages = max(page_nums) if page_nums else None

        projects = []
        for p in data.get('initialTopResults', []):
            parsed = self._parse_json_project(p, is_top=True)
            if parsed:
                projects.append(parsed)
        for p in data.get('initialResults', []):
            parsed = self._parse_json_project(p, is_top=False)
            if parsed:
                projects.append(parsed)

        if not projects:
            for key in ('projects', 'top_projects', 'hits', 'items'):
                items = data.get(key, [])
                if items:
                    for p in items:
                        parsed = self._parse_json_project(p, is_top=(key == 'top_projects'))
                        if parsed:
                            projects.append(parsed)

        return projects, current_page, total_pages

    def get_total_pages(self):
        """Load page 1 and return the total page count detected from pagination."""
        self._ensure_browser()
        url = self.get_page_url(1)
        print(f"Ermittle Gesamtseitenanzahl …")
        self._page.goto(url, wait_until='networkidle', timeout=30000)
        content = self._page.content()
        soup = BeautifulSoup(content, 'html.parser')
        _, _, total_pages = self._extract_from_script_json(soup)
        if total_pages:
            print(f"Gesamtseitenanzahl erkannt: {total_pages}")
        else:
            print("Konnte Gesamtseitenanzahl nicht ermitteln.")
        return total_pages

    def _parse_json_project(self, p, is_top=False):
        """Convert a raw JSON project object into the internal dict format."""
        try:
            title = p.get('title', 'N/A')

            # Link: current API stores path in links.project (/projekt/slug)
            # plink and url are often null – only use as last resort
            links_obj = p.get('links', {})
            project_path = links_obj.get('project', '')
            if project_path:
                link = f"{self.base_url}{project_path}"
            else:
                slug = p.get('slug', '')
                plink = p.get('plink') or p.get('url') or ''
                if plink:
                    link = f"{self.base_url}{plink}" if plink.startswith('/') else plink
                elif slug:
                    link = f"{self.base_url}/projekt/{slug}"
                else:
                    link = 'N/A'

            company = p.get('company') or p.get('poster', {}).get('company') or 'N/A'

            # Description: strip Quill-editor HTML
            raw_desc = p.get('description', '')
            description = BeautifulSoup(raw_desc, 'html.parser').get_text(strip=True) if raw_desc else 'N/A'

            # Keywords: try several sources in order of preference
            matching = p.get('matching', {})
            kw_list = (
                matching.get('keywordsLocalized')
                or matching.get('textLocalized')
                or matching.get('titleLocalized')
                or p.get('skills')
                or p.get('keywords')
                or []
            )
            def _kw_label(k):
                if isinstance(k, dict):
                    return k.get('de') or k.get('nameDe') or k.get('name') or str(k)
                return str(k)
            keywords = ', '.join(_kw_label(k) for k in kw_list) if kw_list else 'N/A'

            created_raw = p.get('created', '')
            try:
                dt = datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
                created_date = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                created_date = created_raw or datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Top project: topProject field is non-null for promoted listings
            is_top_project = (p.get('topProject') is not None) or is_top

            is_endcustomer = bool(p.get('endcustomer'))

            return {
                'titel': title,
                'link': link,
                'firma': company,
                'beschreibung': description,
                'keywords': keywords,
                'eintragungsdatum': created_date,
                'ist_top_projekt': is_top_project,
                'ist_endkundenprojekt': is_endcustomer,
            }
        except Exception as e:
            print(f"Fehler beim Parsen des JSON-Projekts: {e}")
            return None

    def _extract_from_html(self, soup):
        """Fallback: extract projects from server-rendered HTML after React hydration."""
        projects = []
        hit_list = soup.find('div', class_='hit-list')
        container = hit_list if hit_list else soup.find('body')
        if not container:
            return []

        seen_links = set()
        # Match individual project detail page URLs
        for a in container.find_all('a', href=True):
            href = a['href']
            if not re.search(r'/projekt(?:e)?[-/]', href):
                continue
            full_href = f"{self.base_url}{href}" if href.startswith('/') else href
            if full_href in seen_links:
                continue
            seen_links.add(full_href)

            # Walk up to find the enclosing project card
            card = a
            while card.parent and card.parent != container:
                card = card.parent

            data = self._extract_project_from_card(card, a, full_href)
            if data:
                projects.append(data)

        return projects

    def _extract_project_from_card(self, card, title_link, full_href):
        """Extract fields from a rendered project card element."""
        try:
            title = title_link.get_text(strip=True) or 'N/A'

            # Company: look for a link or element with 'company' in class
            company_elem = card.find(class_=re.compile(r'company', re.I))
            if not company_elem:
                company_elem = card.find('a', href=re.compile(r'/company|/firma'))
            company = company_elem.get_text(strip=True) if company_elem else 'N/A'

            # Keywords
            kw_elems = card.find_all(class_=re.compile(r'keyword|tag|skill|badge|label', re.I))
            keywords_list = list(dict.fromkeys(
                e.get_text(strip=True) for e in kw_elems if e.get_text(strip=True)
            ))
            keywords = ', '.join(keywords_list) if keywords_list else 'N/A'

            # Date
            date_elem = card.find(class_=re.compile(r'date|created|time', re.I))
            if not date_elem:
                date_elem = card.find('time')
            created_date = (
                date_elem.get('datetime') or date_elem.get_text(strip=True)
                if date_elem else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

            is_top = bool(card.find(class_=re.compile(r'top.?project|premium', re.I)))
            is_endcustomer = bool(card.find(class_=re.compile(r'endcustomer|endkunde', re.I)))

            return {
                'titel': title,
                'link': full_href,
                'firma': company,
                'beschreibung': 'N/A',
                'keywords': keywords,
                'eintragungsdatum': created_date,
                'ist_top_projekt': is_top,
                'ist_endkundenprojekt': is_endcustomer,
            }
        except Exception as e:
            print(f"Fehler beim Extrahieren der Karte: {e}")
            return None

    def scrape(self):
        try:
            if not self.login():
                return

            for page in range(1, self.max_pages + 1):
                projects = self.get_page(page)
                if not projects:
                    break

                for project in projects:
                    self.db.conn.execute("""
                        INSERT OR IGNORE INTO projects
                        (title, link, company, description, keywords,
                        created_date, is_top_project, is_endcustomer)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        project['titel'],
                        project['link'],
                        project['firma'],
                        project['beschreibung'],
                        project['keywords'],
                        project['eintragungsdatum'],
                        project['ist_top_projekt'],
                        project['ist_endkundenprojekt']
                    ))
                self.db.conn.commit()

                if page < self.max_pages:
                    time.sleep(random.uniform(2, 4))
        finally:
            self.close()


class ProjectMatcher:
    def __init__(self, db):
        self.db = db

    def find_matches(self, profile, min_score=30):
        projects = pd.read_sql_query("""
            SELECT * FROM projects
            WHERE created_date >= date('now', '-30 days')
        """, self.db.conn)

        # Delete stale match entries for all projects about to be re-evaluated
        # so that profile changes (e.g. updated exclusion criteria) take effect
        # immediately instead of leaving outdated match_debug rows in the DB.
        if not projects.empty:
            project_ids = projects['id'].tolist()
            placeholders = ','.join('?' * len(project_ids))
            self.db.conn.execute(
                f"DELETE FROM matches WHERE project_id IN ({placeholders})",
                project_ids,
            )

        matches = []
        for _, row in projects.iterrows():
            score, debug = self.calculate_match_score(row, profile)
            if score >= min_score:
                matches.append({
                    'project_id': row['id'],
                    'title': row['title'],
                    'link': row['link'],
                    'company': row['company'],
                    'description': row['description'],
                    'keywords': row['keywords'],
                    'created_date': row['created_date'],
                    'is_top_project': row['is_top_project'],
                    'is_endcustomer': row['is_endcustomer'],
                    'match_score': score,
                    'match_debug': debug
                })

        for match in matches:
            self.db.conn.execute("""
                INSERT INTO matches (
                    project_id, title, link, company, description, keywords,
                    created_date, is_top_project, is_endcustomer,
                    match_score, match_debug
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                match['project_id'], match['title'], match['link'],
                match['company'], match['description'], match['keywords'],
                match['created_date'], match['is_top_project'],
                match['is_endcustomer'], match['match_score'],
                match['match_debug']
            ))
        self.db.conn.commit()

    def calculate_match_score(self, row, profile):
        score = 0
        debug_info = []

        excluded = []
        if row['description']:
            excluded.extend([k for k in profile['excluded_keywords']
                            if k.lower() in row['description'].lower()])
        if row['keywords']:
            excluded.extend([k for k in profile['excluded_keywords']
                            if k.lower() in row['keywords'].lower()])
        if row['title']:
            excluded.extend([k for k in profile['excluded_keywords']
                            if k.lower() in row['title'].lower()])

        if excluded:
            debug_info.append(f"Ausgeschlossen wegen: {list(set(excluded))}")
            return 0, "\n".join(debug_info)

        if row['keywords'] and row['keywords'] != 'N/A':
            project_keywords = set(kw.strip().lower() for kw in row['keywords'].split(','))
            profile_skills = set(s.lower() for s in profile['skills'])

            exact_matches = project_keywords.intersection(profile_skills)
            exact_score = (len(exact_matches) / max(len(project_keywords), 1)) * 30

            partial_matches = set()
            for p_kw in project_keywords:
                for skill in profile_skills:
                    if p_kw != skill and (skill in p_kw or p_kw in skill):
                        partial_matches.add(p_kw)

            partial_score = (len(partial_matches) / max(len(project_keywords), 1)) * 20

            score += exact_score + partial_score
            debug_info.append(f"Exact Keyword Score: {exact_score:.2f}")
            debug_info.append(f"Partial Keyword Score: {partial_score:.2f}")
            debug_info.append(f"Exact Matches: {exact_matches}")
            debug_info.append(f"Partial Matches: {partial_matches}")

        if row['description']:
            desc_lower = row['description'].lower()

            matching_skills = [s.lower() for s in profile['skills'] if s.lower() in desc_lower]
            skill_score = min((len(matching_skills) * 4), 20)

            matching_preferred = [k.lower() for k in profile['preferred_keywords'] if k.lower() in desc_lower]
            preferred_score = min((len(matching_preferred) * 2), 10)

            score += skill_score + preferred_score
            debug_info.append(f"Description Skills Score: {skill_score}")
            debug_info.append(f"Description Preferred Score: {preferred_score}")
            debug_info.append(f"Skills in Description: {matching_skills}")
            debug_info.append(f"Preferred in Description: {matching_preferred}")

        try:
            created = datetime.strptime(row['created_date'], '%Y-%m-%d %H:%M:%S')
            days_old = (datetime.now() - created).days
            time_score = 20 * math.exp(-days_old / 15)
            score += time_score
            debug_info.append(f"Time Score: {time_score:.2f}")
            debug_info.append(f"Days Old: {days_old}")
        except Exception:
            pass

        return score, "\n".join(debug_info)

    def get_statistics(self):
        stats = pd.read_sql_query("""
            SELECT
                AVG(match_score) as avg_score,
                COUNT(*) as total_matches,
                MAX(m.match_date) as latest_match,
                MIN(p.created_date) as oldest_project,
                COUNT(DISTINCT p.company) as unique_companies
            FROM matches m
            JOIN projects p ON m.project_id = p.id
            WHERE m.match_date >= date('now', '-7 days')
        """, self.db.conn)
        return stats.to_dict('records')[0]

    def export_matches(self, min_score=30, export_path=None):
        if export_path is None:
            export_path = f"matches_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        matches_df = pd.read_sql_query("""
            SELECT
                p.title, p.company, p.keywords, p.description,
                p.created_date, p.link, p.is_top_project,
                p.is_endcustomer, m.match_score, m.match_debug
            FROM matches m
            JOIN projects p ON m.project_id = p.id
            WHERE m.match_score >= ?
            ORDER BY m.match_score DESC
        """, self.db.conn, params=[min_score])

        matches_df.to_csv(export_path, index=False, sep=';')
        return export_path


if __name__ == "__main__":
    db = FreelancermapDatabase()
    scraper = FreelancermapScraper(
        db=db,
        username=FREELANCERMAP_USERNAME,
        password=FREELANCERMAP_PASSWORD,
        max_pages=MAX_PAGES
    )
    scraper.scrape()

    matcher = ProjectMatcher(db)
    matcher.find_matches(PROFILE, min_score=MIN_SCORE)
    stats = matcher.get_statistics()

    print(f"Matches: {stats['total_matches']}")
    print(f"Durchschnitt Score: {stats['avg_score']:.2f}")

    matcher.export_matches(min_score=MIN_SCORE)
