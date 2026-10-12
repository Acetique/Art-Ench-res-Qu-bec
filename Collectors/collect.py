
"""Art Enchères Québec V4: conservative public-catalogue collector.
No bypass of access restrictions; catalogue entries are not sales results.
"""
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
OUTPUT = DATA / 'oeuvres.json'
REPORT = DATA / 'rapport-collecte.json'
USER_AGENT = 'ArtEncheresQuebec/4.0 (public catalogue research)'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/pdf;q=0.9,*/*;q=0.5'}
TIMEOUT = 20
MAX_PAGES_PER_SOURCE = 25
MAX_PDFS_PER_SOURCE = 5
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 160
DELAY_SECONDS = 2
SOURCES = {
    'BYDealers': ['https://bydealers.com/fr/', 'https://bydealers.com/browse-auction-catalogues/'],
    'Heffel': ['https://www.heffel.com/Auction/'],
    'Gelineau': ['https://www.encangelineau.com/'],
}
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
session = requests.Session()
session.headers.update(HEADERS)
robots_cache = {}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def canonical(url):
    return urldefrag(url)[0]


def ident(source, url, lot):
    return hashlib.sha256(f'{source}|{canonical(url)}|{lot}'.encode()).hexdigest()[:20]


def allowed(url):
    p = urlparse(url)
    if p.scheme not in ('http', 'https'):
        return False
    origin = f'{p.scheme}://{p.netloc}'
    if origin not in robots_cache:
        try:
            r = session.get(origin + '/robots.txt', timeout=TIMEOUT)
            if r.status_code == 404 or r.status_code == 410:
                rp = RobotFileParser()
                rp.parse([])
                robots_cache[origin] = rp
                logging.info('robots.txt absent (%s): %s', r.status_code, origin)
            elif r.status_code == 200:
                rp = RobotFileParser()
                rp.parse(r.text.splitlines())
                robots_cache[origin] = rp
            else:
                logging.warning('robots.txt indetermine HTTP %s: %s', r.status_code, origin)
                return False
        except requests.RequestException as exc:
            logging.warning('robots.txt inaccessible %s: %s', origin, exc)
            return False
    return robots_cache[origin].can_fetch(USER_AGENT, url)


def fetch(url):
    if not allowed(url):
        logging.warning('Page non autorisee ou non verifiable: %s', url)
        return None
    try:
        r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=False)
        if r.is_redirect:
            dest = urljoin(url, r.headers.get('Location', ''))
            r.close()
            logging.info('Redirection ignoree (verification requise): %s -> %s', url, dest)
            return None
        r.raise_for_status()
        content_type = r.headers.get('Content-Type', '').lower()
        size = int(r.headers.get('Content-Length', '0') or 0)
        if size > MAX_PDF_BYTES:
            r.close()
            logging.warning('Fichier trop volumineux: %s', url)
            return None
        chunks, total = [], 0
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                logging.warning('Taille maximale depassee: %s', url)
                return None
            chunks.append(chunk)
        body = b''.join(chunks)
        time.sleep(DELAY_SECONDS)
        if 'pdf' in content_type or body[:4] == b'%PDF':
            return 'pdf', body
        if 'html' in content_type or body.lstrip().startswith((b'<!DOCTYPE', b'<html')):
            return 'html', BeautifulSoup(body, 'html.parser')
        return None
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Echec HTTP %s: %s', url, exc)
        return None


def existing():
    if not OUTPUT.exists():
        return {}
    raw = json.loads(OUTPUT.read_text(encoding='utf-8'))
    works = raw if isinstance(raw, list) else raw.get('oeuvres', []) if isinstance(raw, dict) else None
    if not isinstance(works, list):
        raise ValueError('data/oeuvres.json doit contenir une liste')
    return {w['id']: w for w in works if isinstance(w, dict) and w.get('id')}


def price(value):
    s = re.sub(r'[^\d.,]', '', str(value or ''))
    if not s:
        return None
    if ',' in s and '.' in s:
        s = s.replace(',', '') if s.rfind('.') > s.rfind(',') else s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.') if len(s.split(',')[-1]) <= 2 else s.replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def work_record(source, url, title, artist='', lot=None, image=None, amount=None, currency='CAD', kind='inconnu', status='a_verifier', dimensions=None):
    return {
        'id': ident(source, url, lot or title), 'maison': source, 'artiste': clean(artist),
        'titre': clean(title), 'lot': lot, 'dimensions': dimensions, 'image': image,
        'prix': amount, 'prix_marteau': None, 'prix_vendu': None,
        'type_prix': kind, 'devise': currency, 'statut': status,
        'url': url, 'source_page': url, 'date_collecte': timestamp(),
    }


def ld_objects(obj):
    if isinstance(obj, list):
        for value in obj:
            yield from ld_objects(value)
    elif isinstance(obj, dict):
        yield obj
        if '@graph' in obj:
            yield from ld_objects(obj['@graph'])


def parse_jsonld(soup, source, url):
    records = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for obj in ld_objects(data):
            types = obj.get('@type', [])
            types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
            if not set(types).intersection({'VisualArtwork', 'Painting', 'Product'}):
                continue
            title = clean(obj.get('name'))
            if not title:
                continue
            creator = obj.get('creator', '')
            artist = creator.get('name', '') if isinstance(creator, dict) else creator if isinstance(creator, str) else ''
            offer = obj.get('offers') or {}
            offer = offer[0] if isinstance(offer, list) and offer else offer
            offer = offer if isinstance(offer, dict) else {}
            img = obj.get('image')
            img = img[0] if isinstance(img, list) and img else img
            img = img.get('url') if isinstance(img, dict) else img
            item_url = urljoin(url, obj.get('url') or url)
            amount = price(offer.get('price'))
            records.append(work_record(source, item_url, title, artist,
                image=urljoin(url, img) if isinstance(img, str) else None,
                amount=amount, currency=offer.get('priceCurrency', 'CAD'),
                kind='prix_affiche' if amount is not None else 'inconnu'))
    return records


# Conservative extraction: catalogue PDF lot lines only when number and title are on same line.
# These are UNVERIFIED catalogue records, not completed sales.
LOT_PATTERN = re.compile(r'^\s*(?:LOT\s*)?(\d{1,4})\s*[-–.:]\s*(.{8,140})$', re.I)

def parse_pdf(body, source, url):
    records = []
    try:
        doc = fitz.open(stream=body, filetype='pdf')
        logging.info('PDF %s: %s pages', url, len(doc))
        for i in range(min(len(doc), MAX_PDF_PAGES)):
            lines = doc[i].get_text('text').splitlines()
            for line in lines:
                m = LOT_PATTERN.match(line)
                if not m:
                    continue
                lot, description = m.groups()
                # Avoid obvious headings and boilerplate; no prices inferred.
                if re.search(r'^(page|telephone|fax|www\.|copyright)', description, re.I):
                    continue
                record = work_record(source, url, description, lot=lot)
                record['id'] = ident(source, url, lot)
                record['source_page_pdf'] = i + 1
                record['origine'] = 'catalogue_pdf_non_verifie'
                records.append(record)
        doc.close()
    except Exception as exc:
        logging.warning('PDF illisible %s: %s', url, exc)
    return records


def links(soup, base, source):
    host = urlparse(base).hostname
    results = []
    for a in soup.find_all('a', href=True):
        link = canonical(urljoin(base, a['href']))
        p = urlparse(link)
        if p.hostname != host or p.scheme not in ('https', 'http'):
            continue
        label = clean(a.get_text(' ', strip=True)).lower()
        path = p.path.lower()
        is_pdf = path.endswith('.pdf')
        if source == 'BYDealers':
            relevant = is_pdf and ('catalogue' in path or 'bydealers' in path or 'byd_' in path)
            relevant |= not is_pdf and any(x in (path + ' ' + label) for x in ('catalogue', 'auction', 'enchere', 'encher', 'oeuvre'))
        elif source == 'Heffel':
            relevant = any(x in (path + ' ' + label) for x in ('result', 'auction', 'lot'))
        else:
            relevant = any(x in (path + ' ' + label) for x in ('catalogue', 'encan', 'auction', 'tableau'))
        if relevant and link not in results:
            results.append(link)
    return results


def merge(db, record):
    key = record['id']
    if key not in db:
        db[key] = record
        return True
    old = db[key]
    for k, v in record.items():
        if k != 'date_collecte' and v is not None and old.get(k) in (None, ''):
            old[k] = v
    return False


def run_source(source, seeds, db):
    queue = [canonical(s) for s in seeds]
    seen = set()
    stats = {'pages_consultees': 0, 'pages_accessibles': 0, 'pdf_lus': 0,
             'oeuvres_detectees': 0, 'nouvelles_oeuvres': 0, 'erreurs_ou_blocages': 0}
    while queue and len(seen) < MAX_PAGES_PER_SOURCE:
        url = queue.pop(0)
        if url in seen:
            continue
        if url.lower().endswith('.pdf') and stats['pdf_lus'] >= MAX_PDFS_PER_SOURCE:
            continue
        seen.add(url)
        stats['pages_consultees'] += 1
        logging.info('%s: %s', source, url)
        fetched = fetch(url)
        if fetched is None:
            stats['erreurs_ou_blocages'] += 1
            continue
        kind, content = fetched
        stats['pages_accessibles'] += 1
        if kind == 'pdf':
            stats['pdf_lus'] += 1
            works = parse_pdf(content, source, url)
        else:
            works = parse_jsonld(content, source, url)
            for link in links(content, url, source):
                if link not in seen and link not in queue:
                    queue.append(link)
        stats['oeuvres_detectees'] += len(works)
        for work in works:
            stats['nouvelles_oeuvres'] += int(merge(db, work))
    return stats


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    db = existing()
    report = {'version': 'V4', 'date': timestamp(), 'sources': {}, 'total_oeuvres': 0}
    for source, seeds in SOURCES.items():
        report['sources'][source] = run_source(source, seeds, db)
    values = sorted(db.values(), key=lambda w: (w.get('maison', ''), w.get('titre', '')))
    OUTPUT.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    report['total_oeuvres'] = len(values)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logging.info('TERMINE: %s oeuvres (les entrees PDF ne prouvent pas une vente)', len(values))
    for source, stats in report['sources'].items():
        logging.info('%s: %s pages accessibles, %s PDF, %s nouvelles oeuvres', source,
                     stats['pages_accessibles'], stats['pdf_lus'], stats['nouvelles_oeuvres'])


if __name__ == '__main__':
    main()
