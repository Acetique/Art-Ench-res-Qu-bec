#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Art Enchères Québec — collecteur Gélineau V1 (HTML public HiBid).
Dépendances: requests, beautifulsoup4. Ne contourne aucune restriction d'accès.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'data' / 'gelineau.json'
REPORT = ROOT / 'data' / 'rapport-gelineau.json'
BASE = 'https://hibid.com'
COMPANY = BASE + '/company/87825/encans-gelineau-et-fils-inc'
# Catalogues publics confirmés; d'autres sont découverts sur la page de l'entreprise.
SEEDS = [
    BASE + '/quebec/catalog/784138/encans-g-lineau-and-fils-no-71',
    BASE + '/canada/catalog/772786/encans-g-lineau-and-fils-no-70',
    BASE + '/quebec/catalog/537190/encans-g-lineau-and-fils-no-52',
    BASE + '/quebec/catalog/502294/encans-g-lineau-and-fils-no-47',
    BASE + '/quebec/catalog/406312/encans-g-lineau-and-fils-no-37',
    BASE + '/quebec/catalog/349662/encans-g-lineau-and-fils-no-30',
    BASE + '/quebec/catalog/309514/encans-g-lineau-and-fils-no-25',
]
HEADERS = {'User-Agent': 'ArtEncheresQuebec/1.0 (+public-catalog-research)', 'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.8'}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
PAUSE = 2
MAX_PAGES = 12
MAX_CATALOGUES = 30


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('hibid.com', 'www.hibid.com', 'canada.hibid.com'):
        return None
    return url.split('#', 1)[0]


def allowed(url):
    parsed = urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    robots = RobotFileParser()
    robots.set_url(origin + '/robots.txt')
    try:
        resp = SESSION.get(origin + '/robots.txt', timeout=20)
        if resp.status_code == 200:
            robots.parse(resp.text.splitlines())
            return robots.can_fetch(HEADERS['User-Agent'], url)
        return resp.status_code in (404, 410)
    except requests.RequestException:
        return False


ROBOTS = {}


def fetch(url):
    url = canonical(url)
    if not url:
        raise ValueError('URL externe non autorisée')
    host = urlparse(url).netloc
    if host not in ROBOTS:
        ROBOTS[host] = allowed(url)
    if not ROBOTS[host]:
        raise PermissionError('robots.txt inaccessible ou accès interdit: ' + host)
    resp = SESSION.get(url, timeout=35, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError('Redirection non suivie: ' + url)
    resp.raise_for_status()
    if 'html' not in resp.headers.get('Content-Type', '').lower():
        raise ValueError('Réponse non HTML: ' + url)
    time.sleep(PAUSE)
    return BeautifulSoup(resp.text, 'html.parser')


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def money(text):
    val = re.sub(r'[^\d.,]', '', text)
    if not val:
        return None
    if '.' in val and len(val.rsplit('.', 1)[-1]) == 2:
        val = val.rsplit('.', 1)[0]
    elif ',' in val and len(val.rsplit(',', 1)[-1]) == 2:
        val = val.rsplit(',', 1)[0]
    val = re.sub(r'\D', '', val)
    return int(val) if val else None


PRICE_RANGE = re.compile(r'([\d][\d, .]*)\s*[-–]\s*([\d][\d, .]*)\s*(?:CAD|CDN|Can)?', re.I)
REALIZED = re.compile(r'(?:Price\s+Realized|Prix\s+r[ée]alis[ée])\s*:?\s*([\d][\d, .]*)\s*(?:CAD|CDN|Can)?', re.I)
LOT = re.compile(r'\bLot\s*#?\s*(\d+[a-z]?)\s*[|:\-]?\s*(.*)', re.I)


def catalog_id(url):
    m = re.search(r'/(?:catalog|auction)/(\d+)', url)
    return m.group(1) if m else None


def discover(soup):
    found = []
    for a in soup.select('a[href]'):
        url = canonical(urljoin(BASE, a.get('href', '')))
        if url and catalog_id(url) and url not in found:
            found.append(url)
    return found


def lot_blocks(soup):
    """N'accepte que les éléments contenant un lien explicite vers un lot."""
    seen = set()
    for a in soup.select('a[href*="/lot/"]'):
        href = canonical(urljoin(BASE, a.get('href', '')))
        if not href or not re.search(r'/lot/\d+', href) or href in seen:
            continue
        seen.add(href)
        block = a
        for parent in list(a.parents)[:6]:
            txt = clean(parent.get_text(' ', strip=True))
            if re.search(r'\bLot\s*\d+', txt, re.I) and len(txt) < 1600:
                block = parent
                if PRICE_RANGE.search(txt) or 'Price Realized' in txt or 'Est.' in txt:
                    break
        yield href, block


def image_url(block):
    for img in block.select('img'):
        for key in ('data-src', 'data-lazy-src', 'src'):
            raw = img.get(key)
            if raw and not raw.startswith('data:'):
                url = urljoin(BASE, raw)
                if urlparse(url).scheme == 'https':
                    return url
    return None


def parse_lot(url, block, auction_id, auction_title, is_past):
    text = clean(block.get_text(' ', strip=True))
    match = LOT.search(text)
    if not match:
        return None
    number = match.group(1)
    title = clean(match.group(2))
    title = re.split(r'\b(?:Est\.|Price Realized|Internet Bidding|Shipping Available|View Lot)\b', title, maxsplit=1, flags=re.I)[0]
    title = title[:240].strip(' |:-')
    if not title:
        return None
    # Fourchette seulement lorsqu'elle est identifiée comme estimation.
    est_match = re.search(r'\bEst\.?\s*' + PRICE_RANGE.pattern, text, re.I)
    if not est_match:
        # Les anciens catalogues affichent parfois la fourchette sans « Est. ».
        est_match = PRICE_RANGE.search(text)
    low = money(est_match.group(1)) if est_match else None
    high = money(est_match.group(2)) if est_match else None
    if low is not None and (high is None or low > high):
        low = high = None
    sold = REALIZED.search(text) if is_past else None
    realized = money(sold.group(1)) if sold else None
    item_id = hashlib.sha256(f'gelineau|{auction_id}|{number}'.encode()).hexdigest()[:20]
    return {
        'id': item_id, 'id_passage': item_id, 'maison': 'Gélineau et Fils',
        'vente': auction_title, 'id_vente': auction_id, 'lot': number,
        'titre': title, 'artiste': title.split(' / ')[0].strip() if ' / ' in title else None,
        'estimation_min': low, 'estimation_max': high,
        'estimation': f'{low:,} - {high:,} CAD' if low is not None else None,
        'prix_marteau': realized, 'prix_vendu': realized, 'prix': realized,
        'prix_realise_verifie': realized is not None,
        'type_prix': 'prix_realise' if realized is not None else ('estimation' if low is not None else 'inconnu'),
        'statut': 'vente_passee' if is_past else 'en_cours',
        'devise': 'CAD', 'image': image_url(block), 'url': url,
        'date_collecte': stamp(),
    }


def pages_for(soup, url):
    links = {url}
    auction = catalog_id(url)
    for a in soup.select('a[href]'):
        target = canonical(urljoin(url, a['href']))
        if target and catalog_id(target) == auction and re.search(r'[?&](?:page|p)=\d+', target, re.I):
            links.add(target)
    return sorted(links)[:MAX_PAGES]


def main():
    print('ART ENCHERES QUEBEC — COLLECTEUR GELINEAU V1', flush=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    previous = []
    if OUTPUT.exists():
        obj = json.loads(OUTPUT.read_text(encoding='utf-8'))
        previous = obj if isinstance(obj, list) else obj.get('oeuvres', [])
    catalogs = list(SEEDS)
    errors = []
    try:
        home = fetch(COMPANY)
        for url in discover(home):
            if catalog_id(url) not in {catalog_id(u) for u in catalogs}:
                catalogs.insert(0, url)
    except Exception as exc:
        errors.append({'catalogue': 'entreprise', 'erreur': str(exc)})
    items = {}
    for url in catalogs[:MAX_CATALOGUES]:
        try:
            soup = fetch(url)
            heading = soup.find('h1')
            title = clean(heading.get_text(' ', strip=True)) if heading else f'Gélineau {catalog_id(url)}'
            body = clean(soup.get_text(' ', strip=True))
            past = bool(re.search(r'\bPast Auction\b|Encan termin[ée]', body, re.I))
            page_urls = pages_for(soup, url)
            print(f'Catalogue {catalog_id(url)} : {title} — pages découvertes {len(page_urls)}', flush=True)
            for page_url in page_urls:
                page = soup if page_url == url else fetch(page_url)
                for lot_url, block in lot_blocks(page):
                    item = parse_lot(lot_url, block, catalog_id(url), title, past)
                    if item:
                        items[item['id']] = item
        except Exception as exc:
            errors.append({'catalogue': url, 'erreur': str(exc)})
            print('AVERTISSEMENT :', url, exc, flush=True)
    # La base existante est conservée, sans remplacer des résultats vérifiés par des champs vides.
    merged = {r['id']: r for r in previous if isinstance(r, dict) and r.get('id')}
    for key, fresh in items.items():
        old = merged.get(key, {})
        for field in ('prix_marteau', 'prix_vendu', 'prix', 'image', 'estimation_min', 'estimation_max'):
            if fresh.get(field) is None and old.get(field) is not None:
                fresh[field] = old[field]
        if old.get('prix_realise_verifie') and fresh.get('prix_marteau') is not None:
            fresh['prix_realise_verifie'] = True
        merged[key] = fresh
    report = {'date': stamp(), 'catalogues_tentes': len(catalogs[:MAX_CATALOGUES]),
              'lots_nouveaux_detectes': len(items), 'lots_conserves': len(merged),
              'prix_realises': sum(x.get('prix_marteau') is not None for x in merged.values()),
              'images_referencees': sum(bool(x.get('image')) for x in merged.values()),
              'erreurs': errors}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not items and not previous:
        print('ECHEC : aucun lot extrait. Vérifier le rapport; HTML dynamique ou accès bloqué.', flush=True)
        raise SystemExit(1)
    OUTPUT.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()

