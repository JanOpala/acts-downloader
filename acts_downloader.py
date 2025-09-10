import logging
from typing import List, Dict
from urllib.parse import urljoin, urlparse
import re
import os
from typing import Optional
import requests
from bs4 import BeautifulSoup, NavigableString

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

def find_articles(url: str, soup: BeautifulSoup):
    host = urlparse(url).netloc.lower()
    if "legislacja.rcl.gov.pl" in host:
        return soup.select("li .cbp_tmlabel")
    return []

def fetch_subpages(main_url: str) -> List[Dict[str, str]]:
    """
    Pobierz listę pozycji (tytuł + link) z osi czasu projektu na legislacja.rcl.gov.pl.

    Args:
        main_url: URL strony projektu (np. https://legislacja.rcl.gov.pl/projekt/12400101)
        max_items: maksymalna liczba pozycji do zwrócenia

    Returns:
        Lista słowników: {"title": <tytuł>, "link": <absolutny URL>}
    """
    try:
        response = requests.get(main_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        articles = find_articles(main_url, soup)
        news_list: List[Dict[str, str]] = []

        for article in articles:
            a = article if article.name == "a" and article.get("href") else article.select_one("a[href]")
            if not a:
                continue

            href = a.get("href")
            if not href:
                continue

            link = urljoin(main_url, href)
            title = a.get_text(strip=True)
            if title and link:
                news_list.append({"title": title, "link": link})

        return news_list

    except requests.RequestException as e:
        logger.error(f"Failed to fetch RCL page {main_url}: {e}")
        return []

def find_acts(url: str, soup: BeautifulSoup):
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()

    if "legislacja.rcl.gov.pl" in host:
        return soup.select('li .cbp_tmlabel ul .clearbox ul ul li')

    if "sejm.gov.pl" in host:
        return soup.select('.druk')

    if "dziennikustaw.gov.pl" in host:
        anchors = soup.select(
            'td p a[href$=".pdf"], a[href^="/DU/"][href$=".pdf"], a[href*="/DU/"][href$=".pdf"]'
        )
        fixed = []
        for a in anchors:
            if not a.get("href"):
                continue
            if not a.get_text(strip=True):
                label = None

                p = a.find_parent("p")
                if p:
                    b = p.find("b")
                    if b and b.get_text(strip=True):
                        label = b.get_text(strip=True)

                if not label:
                    img = a.find("img")
                    if img:
                        label = img.get("title") or img.get("alt")

                if not label:
                    label = a.get("href").rsplit("/", 1)[-1]

                a.append(NavigableString(" " + label))

            fixed.append(a)
        return fixed
    if host.endswith("gov.pl") and "/web/finanse" in path:
        nodes = soup.select('article#main-content a.file-download[href]')
        if nodes:
            return nodes
        nodes = soup.select('article#main-content a[href*="/attachment/"], article#main-content a[href$=".pdf"]')
        if nodes:
            return nodes
        return soup.select('a.file-download[href], a[href*="/attachment/"], a[href$=".pdf"]')
    return []

def downloadable_acts(url):
    """
    Download legislative acts from the given URL

    Args:
        url: URL of the legislative acts page

    Returns:
        Dictionary of documents to download
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        articles = find_acts(url, soup)
        news_list: List[Dict[str, str]] = []

        for article in articles:
            a = article if article.name == "a" and article.get("href") else article.select_one("a[href]")
            if not a:
                continue

            href = a.get("href")
            if not href:
                continue

            link = urljoin(url, href)
            title = a.get_text(strip=True)
            if title and link:
                news_list.append({"title": title, "link": link})

        return news_list

    except requests.RequestException as e:
        logger.error(f"Failed to fetch RCL page {url}: {e}")
        return []

def _filename_from_cd(content_disposition: Optional[str]) -> Optional[str]:
    if not content_disposition:
        return None
    m = re.search(r"filename\*\s*=\s*[^']+'[^']*'([^;]+)", content_disposition, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r'filename\s*=\s*"([^"]+)"', content_disposition, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", content_disposition, flags=re.I)
    if m:
        return m.group(1).strip()
    return None

def _looks_like_pdf(content: bytes, headers: dict) -> bool:
    if content[:4] == b"%PDF":
        return True
    start = content.lstrip()[:4]
    if start == b"%PDF":
        return True
    ct = headers.get("Content-Type", "").lower()
    return "pdf" in ct

def _download_once(url: str, headers: dict, timeout: int = 60):
    r = requests.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    r.raise_for_status()
    content = b""
    for chunk in r.iter_content(chunk_size=8192):
        if chunk:
            content += chunk
    return r, content

def _safe_dirname(name: str) -> str:
    """
    Uproszczone oczyszczanie nazwy folderu (bez niedozwolonych znaków dla Windows/macOS/Linux).
    """
    sanitized = re.sub(r'[<>:"/\\|?*]+', "_", name).strip()
    sanitized = sanitized.strip(" .")[:120]  
    return sanitized or "untitled"

def download_file(url: str, folder: str, title: Optional[str], subtitle: Optional[str], referer: Optional[str] = None) -> str:
    """
    Pobiera plik spod `url` i zapisuje go do podfolderu `title[/subtitle]` wewnątrz `folder`.
    - Nie dubluje plików (gdy istnieje).
    - Dla gov.pl attachment sprawdza, czy faktycznie pobrał PDF; jeśli nie, próbuje alternatywne ścieżki.
    """
    base_dir = folder
    if title:
        base_dir = os.path.join(base_dir, _safe_dirname(title))
    if subtitle:
        base_dir = os.path.join(base_dir, _safe_dirname(subtitle))
    os.makedirs(base_dir, exist_ok=True)

    req_headers = dict(HEADERS)
    req_headers["Accept"] = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"
    if referer:
        req_headers["Referer"] = referer

    resp, content = _download_once(url, req_headers)
    filename = _filename_from_cd(resp.headers.get("Content-Disposition")) or os.path.basename(urlparse(url).path) or "pobrany_plik"
    dest_path = os.path.join(base_dir, filename)

    if os.path.exists(dest_path):
        return dest_path

    p = urlparse(url)
    if not _looks_like_pdf(content, resp.headers) and p.netloc.endswith("gov.pl") and "/attachment/" in p.path:
        candidates = []
        candidates.append(url.rstrip("/") + "/download")
        candidates.append(url + ("&download=1" if "?" in url else "?download=1"))

        for alt in candidates:
            try:
                resp2, content2 = _download_once(alt, req_headers)
                if _looks_like_pdf(content2, resp2.headers):
                    filename2 = _filename_from_cd(resp2.headers.get("Content-Disposition")) or filename
                    dest_path = os.path.join(base_dir, filename2)
                    if os.path.exists(dest_path):
                        return dest_path
                    with open(dest_path, "wb") as f:
                        f.write(content2)
                    return dest_path
            except requests.RequestException:
                pass 

    with open(dest_path, "wb") as f:
        f.write(content)
    return dest_path


# def download_file(url: str, folder: str, title: str, subtitle: str) -> str:
#     """
#     Pobiera plik spod `url` i zapisuje go do podfolderu `title` wewnątrz `folder`.
#     - Jeśli podfolder nie istnieje, zostanie utworzony.
#     - Jeśli plik o tej samej nazwie już istnieje, nie jest pobierany ponownie (zapobieganie duplikacji).

#     Zwraca pełną ścieżkę do pliku.
#     """
#     base_dir = folder
#     if title:
#         base_dir = os.path.join(folder, _safe_dirname(title))
#     if subtitle:
#         base_dir = os.path.join(base_dir, _safe_dirname(subtitle))
#     os.makedirs(base_dir, exist_ok=True)

#     filename = os.path.basename(urlparse(url).path) or "pobrany_plik"
#     dest_path = os.path.join(base_dir, filename)

#     if os.path.exists(dest_path):
#         return dest_path

#     with requests.get(url, stream=True, timeout=60, headers=HEADERS) as r:
#         r.raise_for_status()
#         with open(dest_path, "wb") as f:
#             for chunk in r.iter_content(chunk_size=8192):
#                 if chunk:  
#                     f.write(chunk)

#     return dest_path

def get_title_from_url(url: str) -> str:
    """
    Extract a readable title from the URL.

    Args:
        url: The URL to extract the title from.

    Returns:
        A cleaned-up title string.
    """
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        host = urlparse(url).netloc.lower()
        if "legislacja.rcl.gov.pl" in host:
            title = soup.select_one(".rcl-title")
        if "sejm.gov.pl" in host:
            title = soup.select_one(".h2")
        if "dziennikustaw.gov.pl" in host or "gov.pl/web/finanse" in host:
            title = soup.find("h2")
        else:
            title = soup.find("title")
        return title.get_text(strip=True) if title else "untitled"

    except requests.RequestException as e:
        logger.error(f"Failed to fetch RCL page {url}: {e}")
        return []


def get_acts(url):
    """
    Fetch and extract text from legislative acts on legislacja.rcl.gov.pl

    Args:
        url: URL of the legislative act page

    Returns:
        Downloaded acts as pdf or text content
    """
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if "legislacja.rcl.gov.pl" in host:
        title = get_title_from_url(url)
        subpages = fetch_subpages(url)
        for subpage in subpages:
            acts = downloadable_acts(subpage['link'])
            for act in acts:
                print(f" - Found document: {act['title']} at {act['link']}")
                download_file(act['link'], "legal_acts", title, subpage['title'])
    if "sejm.gov.pl" in host or "dziennikustaw.gov.pl" in host\
        or (host.endswith("gov.pl") and "/web/finanse" in path):
        title = get_title_from_url(url)
        acts = downloadable_acts(url)
        for act in acts:
            link = act['link']
            print(f" - Found document: {act['title']} at {link}")
            download_file(link, "legal_acts", title, None)
    else:
        logger.warning(f"Unsupported host for acts downloading: {host}")