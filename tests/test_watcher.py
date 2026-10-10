from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path

from watcher.update_catalog import discover, parse_specs, update

URL = 'https://scdusa-ir.com/mwir/'
OLD = 'https://scdusa-ir.com/products/mini-blackbird-1280-hfm/'
NEW = 'https://scdusa-ir.com/products/orion-4096-hfm/'
LIST_OLD = f'''<html><main><h1>MWIR Detectors</h1><article><h3>Mini Blackbird 1280 HFM</h3><a href="{OLD}">Product details</a></article></main></html>'''
LIST_NEW = f'''<html><main><h1>MWIR Detectors</h1><article><h3>Mini Blackbird 1280 HFM</h3><a href="{OLD}">Product details</a></article><article><h3>Orion 4096 HFM</h3><a href="{NEW}">Product details</a></article></main></html>'''
DETAIL = '''<html><title>Orion 4096 HFM</title><main><h1>Orion 4096 HFM</h1><p>New infrared sensor with array resolution 4096 x 3072 pixels, pixel pitch 7.5 µm, spectral response 3.6–4.9 µm, NETD &lt;25mK at F/2 and frame rate up to 60fps.</p></main></html>'''
CONFIG = {'id':'test_scd','manufacturer':'SCD','url':URL,'band':'MWIR','mode':'links','path_regex':r'^/products/[^/]+/?$','min_results':1}

class WatchTests(unittest.TestCase):
    def test_discovery(self):
        a = discover(CONFIG, LIST_NEW)
        self.assertEqual(len(a),2)
        self.assertIn('orion-4096-hfm',a[-1].link)

    def test_spec_extraction(self):
        a = parse_specs('Orion 4096 HFM','resolution 4096 x 3072 pixels, pixel pitch 7.5 µm, spectral response 3.6–4.9 µm, NETD <25mK; frame rate up to 60fps','MWIR')
        self.assertEqual(a['technology'],'HFM')
        self.assertEqual((a['width'],a['height']),(4096,3072))
        self.assertEqual(a['pitch_um'],7.5)
        self.assertEqual(a['spectral_range'],'3.6–4.9 µm')
        self.assertEqual(a['netd_mk'],25)
        self.assertEqual(a['max_fps'],60)

    def test_first_run_is_baseline_and_new_then_no_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'data').mkdir()
            (root/'watcher').mkdir()
            (root/'data/catalog.json').write_text(json.dumps([{
                'id':'old','manufacturer':'SCD','model':'Mini Blackbird 1280 HFM','band':'MWIR',
                'source_url':OLD,'pitch_um':10}]))
            (root/'data/source_state.json').write_text(json.dumps({'sources':{},'events':[]}))
            def first(url,_session): return {URL:LIST_OLD}[url]
            first_run=update(root,fetcher=first,now='2026-10-10T10:00:00Z',configs=[CONFIG])
            self.assertEqual(len(first_run['events']),0)
            self.assertEqual(len(first_run['catalog']),1)
            def second(url,_session): return {URL:LIST_NEW,NEW.rstrip("/"):DETAIL}[url]
            second_run=update(root,fetcher=second,now='2026-10-11T10:00:00Z',configs=[CONFIG])
            self.assertEqual(len(second_run['events']),1)
            self.assertEqual(len(second_run['catalog']),2)
            new=next(r for r in second_run['catalog'] if r['id'].startswith('auto-'))
            self.assertEqual(new['technology'],'HFM')
            self.assertEqual(new['spectral_range'],'3.6–4.9 µm')
            self.assertEqual(new['cooler_options'],[])
            third=update(root,fetcher=second,now='2026-10-12T10:00:00Z',configs=[CONFIG])
            self.assertEqual(len(third['events']),1)
            self.assertEqual(len(third['catalog']),2)

    def test_failure_is_not_interpreted_as_empty_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'data').mkdir()
            (root/'data/catalog.json').write_text('[]')
            (root/'data/source_state.json').write_text(json.dumps({'sources':{},'events':[]}))
            def broken(_url,_session):return '<html><body>Rate limit</body></html>'
            s=update(root,fetcher=broken,now='2026-10-10T10:00:00Z',configs=[CONFIG])
            self.assertEqual(s['sources'][0]['status'],'error')
            saved=json.loads((root/'data/source_state.json').read_text())
            self.assertFalse(saved['sources']['test_scd'].get('initialized'))

if __name__=='__main__': unittest.main()
