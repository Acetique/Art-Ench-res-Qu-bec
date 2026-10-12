"""Collecte prudente de pages publiques: robots.txt, liens candidats, métadonnées JSON-LD.
Aucune connexion privée, aucun contournement de protections ou de limites."""
import json, re, time, hashlib, os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
UA='ArtEncheresQuebecBot/2.0 (+catalogue indépendant; respect robots.txt)'
SOURCES={
 'BYDealers':['https://bydealers.com/fr/decouvrez-les-oeuvres/'],
 'Heffel':['https://www.heffel.com/'],
 'Gélineau':['https://www.encangelineau.com/']
}
S=requests.Session();S.headers.update({'User-Agent':UA,'Accept':'text/html,application/xhtml+xml'})
robots={}
def allowed(url):
 p=urlparse(url);origin=f'{p.scheme}://{p.netloc}'
 if origin not in robots:
  rp=RobotFileParser();rp.set_url(origin+'/robots.txt')
  try:rp.read();robots[origin]=rp
  except Exception:robots[origin]=None
 return robots[origin] is not None and robots[origin].can_fetch(UA,url)
def fetch(url):
 if not allowed(url): print('SKIP robots:',url);return None
 try:
  r=S.get(url,timeout=18);r.raise_for_status()
  if 'html' not in r.headers.get('Content-Type',''):return None
  time.sleep(1.5)
  return BeautifulSoup(r.text,'html.parser')
 except requests.RequestException as e:print('SKIP network:',url,str(e)[:120]);return None

def objects(obj):
 if isinstance(obj,list):
  for x in obj:yield from objects(x)
 elif isinstance(obj,dict):
  yield obj
  if '@graph' in obj:yield from objects(obj['@graph'])

def extract(soup,url,house):
 found=[]
 for script in soup.select('script[type="application/ld+json"]'):
  try: raw=json.loads(script.string or script.get_text())
  except (ValueError,TypeError):continue
  for x in objects(raw):
   typ=x.get('@type',[]);typ=[typ] if isinstance(typ,str) else typ
   if not any(t in typ for t in ('VisualArtwork','Product','IndividualProduct')):continue
   name=x.get('name','');name=name if isinstance(name,str) else ''
   if not name:continue
   artist=x.get('creator') or x.get('artist') or x.get('brand') or {}
   if isinstance(artist,dict):artist=artist.get('name','')
   if isinstance(artist,list):artist=', '.join(a.get('name','') if isinstance(a,dict) else str(a) for a in artist)
   image=x.get('image','');image=image[0] if isinstance(image,list) and image else image
   if isinstance(image,dict):image=image.get('url','')
   offer=x.get('offers') or {};offer=offer[0] if isinstance(offer,list) and offer else offer
   if not isinstance(offer,dict):offer={}
   amount=offer.get('price','')
   status='en vente' if 'InStock' in str(offer.get('availability','')) else 'inconnu'
   item_url=urljoin(url,str(x.get('url') or url))
   if urlparse(item_url).netloc!=urlparse(url).netloc:continue
   found.append(dict(maison=house,artiste=str(artist),titre=name,statut=status,prix=str(amount) if amount!='' else '',date='',url=item_url,image=urljoin(url,str(image)) if image else '',dimensions='',lot='',type_prix='prix affiché (non vérifié comme marteau)' if amount!='' else '',verification='métadonnées publiques JSON-LD',date_collecte=datetime.now(timezone.utc).date().isoformat()))
 return found

def main():
 output=ROOT/'data'/'oeuvres.json'; old=[]
 if output.exists():
  try:old=json.loads(output.read_text(encoding='utf-8'))
  except ValueError:pass
 index={r['maison']+'|'+r['url']:r for r in old if r.get('maison') and r.get('url')}
 stats={}
 for house,starts in SOURCES.items():
  n=0
  for start in starts:
   soup=fetch(start)
   if soup is None:continue
   urls=[start]
   # Exploration bornée: uniquement liens explicitement liés à une œuvre/lot.
   for a in soup.select('a[href]'):
    href=urljoin(start,a['href']);p=urlparse(href)
    if p.netloc!=urlparse(start).netloc or p.scheme!='https':continue
    if re.search(r'(lot|artwork|oeuvre|produit|item|auction)',p.path,re.I) and href not in urls:urls.append(href)
    if len(urls)>=21:break
   for i,url in enumerate(urls):
    page=soup if i==0 else fetch(url)
    if page is None:continue
    for item in extract(page,url,house):
     key=house+'|'+item['url'];index[key]={**index.get(key,{}),**item};n+=1
  stats[house]=n
 output.parent.mkdir(parents=True,exist_ok=True)
 output.write_text(json.dumps(sorted(index.values(),key=lambda x:x.get('date_collecte',''),reverse=True),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 (ROOT/'data'/'etat.json').write_text(json.dumps({'date_execution':datetime.now(timezone.utc).isoformat(),'nouveaux_ou_actualises':stats,'total':len(index)},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print('Statistiques:',stats,'Total:',len(index))
if __name__=='__main__':main()

