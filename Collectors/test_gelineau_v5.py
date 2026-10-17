
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gélineau V5 — diagnostic isolé de pagination HiBid (catalogue 784138).
N'écrit jamais dans data/gelineau.json. Dépendance: requests.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests

AUCTION_ID = 784138
API = 'https://hibid-api.io/sr/main/v1/search/lot'
PAGE_SIZE = 100
ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / 'data' / 'test-gelineau-v5.json'
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'ArtEncheresQuebec/1.0 (public-catalog-research)',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Origin': 'https://hibid.com',
    'Referer': 'https://hibid.com/quebec/catalog/784138/encans-g-lineau-and-fils-no-71',
})


def check_robots():
    url = 'https://hibid-api.io/robots.txt'
    response = SESSION.get(url, timeout=20)
    if response.status_code in (404, 410):
        return
    if response.status_code != 200:
        raise RuntimeError(f'Impossible de vérifier robots.txt (HTTP {response.status_code}); arrêt par prudence')
    rp = RobotFileParser()
    rp.parse(response.text.splitlines())
    if not rp.can_fetch(SESSION.headers['User-Agent'], API):
        raise RuntimeError('Accès interdit par robots.txt; arrêt')


def fetch_page(number):
    # Forme observée dans DevTools: {query: "", options: {auctionId: 784138}}.
    # Les paramètres de pagination ci-dessous sont une hypothèse à vérifier.
    payload = {
        'query': '',
        'options': {'auctionId': AUCTION_ID, 'pageNumber': number, 'pageSize': PAGE_SIZE},
    }
    response = SESSION.post(API, json=payload, timeout=35, allow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError(f'Redirection HTTP {response.status_code} non suivie')
    response.raise_for_status()
    obj = response.json()
    data = obj.get('data') if isinstance(obj, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get('lots'), list):
        raise ValueError('Réponse inattendue (data.lots absent)')
    ids = [str(item['id']) for item in data['lots'] if isinstance(item, dict) and item.get('id')]
    return {
        'page_demandee': number,
        'page_retournee': data.get('pageNumber'),
        'total_lots_annonces': data.get('totalCount'),
        'total_pages_annoncees': data.get('totalPages'),
        'nombre_ids': len(ids),
        'ids': ids,
    }


def main():
    report = {
        'version': '5.0-test',
        'date': datetime.now(timezone.utc).isoformat(),
        'catalogue': AUCTION_ID,
        'endpoint': API,
        'hypothese_pagination': 'options.pageNumber et options.pageSize (non confirmés)',
        'pages': [],
        'identifiants_uniques': 0,
        'pagination_confirmee': False,
        'erreurs': [],
    }
    seen = set()
    try:
        check_robots()
        for page in range(1, 4):
            result = fetch_page(page)
            new = set(result['ids']) - seen
            seen.update(result['ids'])
            result['nouveaux_ids'] = len(new)
            result['premiers_ids'] = result['ids'][:3]
            result.pop('ids')
            report['pages'].append(result)
            print(f"Page {page}: retour={result['page_retournee']}, ids={result['nombre_ids']}, nouveaux={len(new)}", flush=True)
            if page > 1 and not new:
                report['erreurs'].append('Pagination non confirmée: identifiants répétés; ne pas déployer en production')
                break
            time.sleep(2)
        report['identifiants_uniques'] = len(seen)
        report['pagination_confirmee'] = (
            len(report['pages']) == 3
            and [x['page_retournee'] for x in report['pages']] == [1, 2, 3]
            and len(seen) == 219
        )
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
        report['erreurs'].append(f'{type(exc).__name__}: {exc}')
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['pagination_confirmee']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
