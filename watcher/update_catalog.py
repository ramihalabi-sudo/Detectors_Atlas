#!/usr/bin/env python3
"""Detector Atlas official-listing watcher.

Runs in a scheduled GitHub Action. It is intentionally conservative:
* monitors product listings from allowlisted manufacturer sites, not news/general SEO
* first successful observation of each listing creates a baseline (no false 'launches')
* on subsequent checks, marks only newly observed product URLs/model tokens
* collects ONLY explicitly identifiable specs, with an evidence URL
* keeps failed/inaccessible sources out of negative-availability conclusions
* does not conflate date detected with product launch date
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger('detector-atlas-watch')
UA = 'DetectorAtlasResearchWatch/1.0 (public product metadata; 6-hour interval; no login)'
BANDS = {'DAY', 'SWIR', 'MWIR', 'LWIR'}
MAX_HTML_BYTES = 2_800_000
SCHEMA_VERSION = 1


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def norm(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def canonical_url(s: str) -> str:
    p = urlparse(s)
    if p.scheme.lower() != 'https':
        raise ValueError('Only HTTPS evidence allowed')
    # Drop tracking queries and fragments, normalize trailing slash, preserve significant case.
    return urlunparse(('https', p.netloc.lower(), p.path.rstrip('/') or '/', '', '', ''))


def same_authorized_host(url: str, listing_url: str) -> bool:
    # Product links must remain on the same manufacturer's host.
    # Non-www/www variants are permitted for the same base host.
    a = urlparse(url).hostname or ''
    b = urlparse(listing_url).hostname or ''
    return a.removeprefix('www.') == b.removeprefix('www.')


def fetch_html(url: str, session: requests.Session) -> str:
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    r = session.get(url, timeout=(8, 24), allow_redirects=True,
                    headers={'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml'})
    r.raise_for_status()
    if not same_authorized_host(r.url, url):
        raise ValueError('Unexpected redirect to a different host')
    ct = r.headers.get('Content-Type', '').lower()
    if ct and 'html' not in ct:
        raise ValueError('Source is not HTML')
    if len(r.content) > MAX_HTML_BYTES:
        raise ValueError('HTML document too large')
    if not r.encoding:
        r.encoding = r.apparent_encoding
    return r.text


@dataclass(frozen=True)
class Candidate:
    key: str
    link: str
    text: str
    page: str
    token_source: bool = False


def visible_label(a: Any) -> str:
    s = a.get_text(' ', strip=True)
    # 'View Article' labels use a neighboring heading.
    if len(s) < 4 or s.lower() in {'view article', 'view product', 'details', 'learn more', 'read more'}:
        parent = a.find_parent(['article', 'section', 'li', 'div'])
        if parent:
            h = parent.find(['h1', 'h2', 'h3', 'h4'])
            if h:
                s = h.get_text(' ', strip=True)
    return ' '.join(s.split())[:135]


def discover(config: dict, html: str) -> list[Candidate]:
    soup = BeautifulSoup(html, 'html.parser')
    # Exclude navigation and footer so category menus are not treated as products.
    for garbage in soup.select('nav, footer, script, style, noscript'):
        garbage.decompose()
    found: dict[str, Candidate] = {}
    if config['mode'] == 'links':
        ptn = re.compile(config['path_regex'], re.I)
        for a in soup.select('a[href]'):
            href = a.get('href', '').strip()
            if not href or href.startswith(('mailto:', 'tel:', '#', 'javascript:')):
                continue
            url = urljoin(config['url'], href)
            if not same_authorized_host(url, config['url']):
                continue
            path = unquote(urlparse(url).path)
            if not ptn.search(path):
                continue
            url = canonical_url(url)
            if url == canonical_url(config['url']):
                continue
            label = visible_label(a) or unquote(path.rstrip('/').split('/')[-1]).replace('-', ' ')
            key = 'url:' + url.lower()
            found[key] = Candidate(key, url, label, config['url'])
    elif config['mode'] == 'model_tokens':
        # Restrict to main body if possible; avoid metadata/head and boilerplate.
        main = soup.find('main') or soup.find('article') or soup.find('body') or soup
        body = main.get_text(' ', strip=True)
        ptn = re.compile(config['model_regex'])
        for m in ptn.finditer(body):
            model = ' '.join(m.group(0).split()).strip()
            key = 'model:' + norm(model)
            if len(norm(model)) < 5:
                continue
            window = body[max(0, m.start()-90):min(len(body), m.end()+220)]
            found[key] = Candidate(key, config['url'], model, window, True)
    else:
        raise ValueError(f'Unknown discovery mode: {config["mode"]}')
    return list(found.values())


def looks_like_not_found(soup: BeautifulSoup) -> bool:
    t = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    return bool(re.search(r'\b(404|not found|access denied|just a moment)\b', t))


def obtain_product_details(candidate: Candidate, source: dict, session: requests.Session,
                           fetcher: Callable[[str, requests.Session], str]) -> tuple[str, str, str]:
    if candidate.token_source:
        return candidate.text, candidate.link, candidate.page
    html = fetcher(candidate.link, session)
    soup = BeautifulSoup(html, 'html.parser')
    if looks_like_not_found(soup):
        raise ValueError('Product page not accessible')
    heading = soup.select_one('h1') or soup.select_one('h2')
    heading_text = heading.get_text(' ', strip=True) if heading else ''
    if len(heading_text) > 135 or heading_text.lower() in {'products', 'home', 'detectors'}:
        heading_text = ''
    name = heading_text or candidate.text
    if not name or name.lower() in {'view product', 'view article', 'details'}:
        name = unquote(urlparse(candidate.link).path.split('/')[-1]).replace('-', ' ')
    main = (soup.find('main') or soup.find('article') or soup.find('body') or soup)
    for x in main.select('script,style,nav,footer,noscript'):
        x.decompose()
    text = main.get_text(' ', strip=True)[:26000]
    if len(text) < 30:
        raise ValueError('Product page contains too little readable content')
    return ' '.join(name.split())[:128], candidate.link, text


def infer_band(model: str, description: str, fixed_band: str | None) -> str | None:
    if fixed_band in BANDS:
        return fixed_band
    # Only a *model-level* band is safe; do not use generic page nav/context.
    t = model.upper()
    for label, ptn in [('LWIR', r'\b(?:LWIR|LW|LONG[- ]?WAVE)\b'),
                       ('MWIR', r'\b(?:MWIR|MW|MID[- ]?WAVE)\b'),
                       ('SWIR', r'\b(?:SWIR|SW|SHORT[- ]?WAVE|ESWIR)\b')]:
        if re.search(ptn, t):
            return label
    # Only explicit product detail heading pattern, not arbitrary body text.
    prefix = description[:180].upper()
    mentions = [b for b in ['LWIR','MWIR','SWIR'] if re.search(r'\b'+b+r'\b',prefix)]
    return mentions[0] if len(mentions) == 1 else None


def parse_float(s: str) -> float:
    return float(s.replace(',', '.'))


def parse_specs(model: str, body: str, fixed_band: str | None = None) -> dict:
    """Extract restricted engineering fields; never infer unknowns or cooling compatibility."""
    b = ' '.join(body.split())
    specs: dict[str, Any] = {}
    band = infer_band(model, b, fixed_band)
    if band:
        specs['band'] = band
    matrix = re.search(r'(?i)(?:resolution|array|format|fpa|active pixels|image size)[^.;\n]{0,75}?\b(\d{2,5})\s*[x×]\s*(\d{2,5})\b', b)
    if not matrix:
        matrix = re.search(r'\b(\d{2,5})\s*[x×]\s*(\d{2,5})\s*(?:px|pixels|pixel\b)', b, flags=re.I)
    if matrix:
        w,h = map(int, matrix.groups())
        if 64 <= w <= 20000 and 1 <= h <= 20000:
            specs.update(width=w, height=h, megapixels=round(w*h/1e6,3))
    pitch = (re.search(r'(?i)(?:pixel\s*pitch|pitch|pixel\s*size)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*(?:µm|μm|um|micron)', b) or
             re.search(r'(?i)\b(\d+(?:[.,]\d+)?)\s*(?:µm|μm|um)\s*(?:pixel\s*pitch|pitch|pixels?)\b', b))
    if pitch:
        x = parse_float(pitch.group(1))
        if 0.5 <= x <= 100:
            specs['pitch_um'] = x
    # Spectral interval accepted only next to wavelength-specific terminology.
    spectral = re.search(r'(?i)(?:spectral(?:\s+(?:response|range|band|coverage|sensitivity))?|wavelength(?:\s+range)?|response\s+band)\s*[:=]?\s*\D{0,24}?(\d+(?:[.,]\d+)?)\s*(?:–|—|-|to|~)\s*(\d+(?:[.,]\d+)?)\s*(?:µm|μm|um)', b)
    if spectral:
        lo,hi = map(parse_float,spectral.groups())
        if 0.25 <= lo < hi <= 30:
            specs['spectral_range'] = f'{lo:g}–{hi:g} µm'
            specs['spectral_verification'] = 'Manufacturer product page (auto-extracted; review)'
    name_and_start = (model + ' ' + b[:350]).upper()
    for label,rx in [('XBn',r'\bXBN\b'),('HFM',r'\bHFM\b'),('InSb',r'\bINSB\b'),
                     ('T2SL',r'\bT2SL\b'),('MCT',r'\bMCT\b'),('QWIP',r'\bQWIP\b'),
                     ('InGaAs',r'\bINGAAS\b'),('VOx',r'\bVOX\b')]:
        if re.search(rx,name_and_start):
            specs['technology']=label
            break
    # NETD pertains to thermal products, and the operating conditions can vary.
    netd = re.search(r'(?i)\bNETD\s*[:<≤=]?\s*(\d+(?:[.,]\d+)?)\s*mK\b', b)
    if netd:
        x = parse_float(netd.group(1))
        if 1 <= x <= 2000:
            specs['netd_mk'] = x
            specs['metric_notes'] = 'NETD from manufacturer page; verify optics/temperature conditions before comparison.'
    fps = (re.search(r'(?i)(?:frame\s*rate|frames?\s*per\s*second)[^.;]{0,45}?(\d+(?:[.,]\d+)?)\s*(?:fps|Hz)', b) or
           re.search(r'(?i)(?:up\s*to|max(?:imum)?)[^.;]{0,20}?(\d+(?:[.,]\d+)?)\s*fps\b', b))
    if fps:
        x = parse_float(fps.group(1))
        if 0.1 <= x <= 20000:
            specs['max_fps'] = x
    if re.search(r'(?i)\b(?:uncooled|microbolometer)\b',name_and_start):
        specs['cooling_class'] = 'Uncooled'
    if re.search(r'(?i)\b(?:cryocooler|stirling|joule.?thomson)\b',name_and_start):
        specs['cooling_class'] = 'Cryogenic (specific cooler not verified)'
    if re.search(r'(?i)\bTEC\s*(?:1|2|cooled)?\b',name_and_start) and 'cooling_class' not in specs:
        specs['cooling_class'] = 'TEC'
    return specs


def record_key(manufacturer: str, model: str, band: str = '') -> str:
    return norm(manufacturer) + '|' + norm(model) + '|' + band.upper()


def catalog_match(catalog: list[dict], manufacturer: str, model: str, band: str | None, link: str) -> bool:
    nk = norm(model)
    if len(nk)<5:
        return True
    for row in catalog:
        if norm(row.get('manufacturer','')) != norm(manufacturer):
            continue
        rmodel = norm(row.get('model',''))
        if nk == rmodel and (band is None or row.get('band')==band):
            return True
        if len(nk)>8 and len(rmodel)>8 and (nk in rmodel or rmodel in nk) and (band is None or row.get('band')==band):
            # Same family text may cover variants; require model token overlap substantial.
            if min(len(nk),len(rmodel))/max(len(nk),len(rmodel)) > 0.82:
                return True
    return False


def make_record(cand: Candidate, manufacturer: str, model: str, url: str,
                specs: dict, moment: str) -> dict | None:
    if specs.get('band') not in BANDS:
        return None
    uid = 'auto-'+hashlib.sha256((manufacturer+'\x1f'+url+'\x1f'+model).encode()).hexdigest()[:16]
    return {
        'id': uid, 'manufacturer': manufacturer, 'model': model, 'band': specs['band'],
        'width': None, 'height': None, 'pitch_um': None, 'megapixels': None,
        'technology': 'Not verified', 'product_type': 'FPA / camera (verify product page)',
        'source_url': url, 'source_level': 'Official manufacturer listing (automatic)',
        'status': 'Newly detected listing — review specifications',
        'notes': 'Automatically discovered on manufacturer website. Detection time is NOT the manufacturer release date. Review model-level values before engineering use.',
        'spectral_range': '', 'cooling': '', 'checked': moment[:10], 'netd_mk': None,
        'qe_percent': None, 'read_noise_e': None, 'max_fps': None, 'metric_notes': None,
        'cooler_options': [], 'cooling_class': 'Not documented',
        'discovered_at': moment, 'watch_status': 'Needs engineering review',
        'spectral_source_url': url if specs.get('spectral_range') else '',
        'technology_source_url': url if specs.get('technology') else '',
        **{k:v for k,v in specs.items() if k in {'band','width','height','pitch_um','megapixels','technology','spectral_range','spectral_verification','netd_mk','metric_notes','max_fps','cooling_class'}}
    }


def make_event(source: dict, cand: Candidate, model: str, url: str, specs: dict,
               record: dict | None, moment: str) -> dict:
    eid = 'evt-'+hashlib.sha256((source['manufacturer']+'|'+cand.key+'|'+url).encode()).hexdigest()[:18]
    return {
        'id': eid, 'kind': 'new_listing', 'manufacturer': source['manufacturer'],
        'model': model, 'detected_at': moment, 'source_id': source['id'],
        'source_url': url, 'listing_url': source['url'],
        'band': specs.get('band'), 'specs': {k:v for k,v in specs.items() if k != 'band'},
        'record_id': record['id'] if record else None,
        'review': 'Auto-extracted. Manufacturer announcement date not confirmed. Verify datasheet.',
        'status': 'Catalogued — review' if record else 'Unclassified candidate — review'
    }


def update(root: Path, fetcher: Callable[[str, requests.Session], str] = fetch_html,
           now: str | None = None, configs: list[dict] | None = None) -> dict:
    moment = now or utcnow()
    catalog_path = root/'data/catalog.json'
    state_path = root/'data/source_state.json'
    live_path = root/'data/live.json'
    configs = configs or json.loads((root/'watcher/sources.json').read_text(encoding='utf-8'))
    catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'version':1,'sources':{},'events':[]}
    state.setdefault('sources',{})
    events = list(state.get('events',[]))
    event_ids = {e['id'] for e in events}
    total_added=0
    statuses=[]
    with requests.Session() as session:
        for config in configs:
            sid = config['id']
            src = state['sources'].get(sid, {})
            previously = set(src.get('known_keys',[]))
            is_initialized = bool(src.get('initialized',False))
            previous_snapshot = src.get('last_ok')
            try:
                html = fetcher(config['url'], session)
                candidates = discover(config,html)
                # An anti-bot/login/error response can be HTML 200 but isn't a valid catalog.
                if len(candidates) < config.get('min_results',1):
                    raise ValueError(f'Only {len(candidates)} product candidates; expected at least {config.get("min_results",1)}')
                present = {c.key for c in candidates}
                if not is_initialized:
                    # FIRST RUN == BASELINE. Do not interpret existing pages as new launches.
                    src['known_keys'] = sorted(present)
                    src['initialized'] = True
                    src['baseline_at'] = moment
                    state['sources'][sid] = src
                    LOG.info('%s: initial baseline (%d entries)',sid,len(present))
                else:
                    old_keys = previously.copy()
                    new_candidates = [c for c in candidates if c.key not in old_keys]
                    for cand in new_candidates:
                        # Only mark a key as seen after product confirmation. Reattempt later if fetch fails.
                        try:
                            model, evidence_url, text = obtain_product_details(cand,config,session,fetcher)
                            specs = parse_specs(model,text,config.get('band'))
                            # Existing catalog entries / a product moved to another page are not a launch.
                            if catalog_match(catalog,config['manufacturer'],model,specs.get('band'),evidence_url):
                                previously.add(cand.key)
                                continue
                            record = make_record(cand,config['manufacturer'],model,evidence_url,specs,moment)
                            # Duplicate source pages of same manufacturer (LYNRED pagination etc).
                            evt = make_event(config,cand,model,evidence_url,specs,record,moment)
                            if evt['id'] not in event_ids:
                                events.insert(0,evt)
                                event_ids.add(evt['id'])
                                if record is not None and not catalog_match(catalog,config['manufacturer'],model,specs.get('band'),evidence_url):
                                    catalog.append(record)
                                    total_added += 1
                                LOG.info('NEW listing: %s %s (%s)',config['manufacturer'],model,evidence_url)
                            previously.add(cand.key)
                        except (requests.RequestException,ValueError) as exc:
                            LOG.warning('%s candidate %s not validated: %s',sid,cand.key,exc)
                    src['known_keys'] = sorted(previously | present.intersection(previously))
                    state['sources'][sid]=src
                src.update(last_ok=moment,last_error=None,last_candidate_count=len(candidates),status='ok')
                statuses.append({'id':sid,'manufacturer':config['manufacturer'],'url':config['url'],
                                 'status':'ok','entries':len(candidates),'last_ok':moment,
                                 'baseline_at':src.get('baseline_at')})
            except (requests.RequestException,ValueError,KeyError) as exc:
                error = f'{type(exc).__name__}: {str(exc)[:230]}'
                LOG.warning('%s scan failed: %s',sid,error)
                src.update(last_error=error,status='error')
                state['sources'][sid]=src
                statuses.append({'id':sid,'manufacturer':config['manufacturer'],'url':config['url'],
                                 'status':'error','error':error,'last_ok':previous_snapshot,
                                 'entries':src.get('last_candidate_count',0)})
            time.sleep(0.15)
    state['events']=events[:300]
    state['version']=SCHEMA_VERSION
    live = {'schema_version': SCHEMA_VERSION, 'last_checked': moment,
            'catalog':catalog,'events':state['events'],'sources':statuses,
            'message': ('Automatic update scan complete. Newly detected listings are NOT necessarily newly launched.'
                        ' Specs are extracted conservatively from public manufacturer listings.')}
    # Atomic replacement avoids partial live JSON during deployment.
    for path,content in [(catalog_path,catalog),(state_path,state),(live_path,live)]:
        temp = path.with_suffix(path.suffix+'.tmp')
        temp.write_text(json.dumps(content,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temp.replace(path)
    LOG.info('Checked %d sources; %d new catalogued listings; %d historical alerts',
             len(statuses),total_added,len(state['events']))
    return live


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args=ap.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    live=update(args.root)
    successes=sum(x['status']=='ok' for x in live['sources'])
    print(f'Source checks succeeded: {successes}/{len(live["sources"])}; alerts: {len(live["events"])}')
    return 0 if successes else 2

if __name__=='__main__':
    sys.exit(main())
