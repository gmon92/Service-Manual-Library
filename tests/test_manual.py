from contextlib import closing, redirect_stdout, redirect_stderr
from io import StringIO
from unittest.mock import patch
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manual


SCRIPT = Path(manual.__file__).resolve()
REAL_ROOT = Path('C:/Users/gabem/Documents/Vehicle Manuals')


def cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, encoding='utf-8')


class LibraryConfigurationTests(unittest.TestCase):
    def test_default_library_is_beside_script(self):
        self.assertEqual(manual.DEFAULT_LIBRARY, SCRIPT.parent / 'Vehicle Manuals')

    def test_cli_default_is_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            script = project / 'manual.py'
            shutil.copyfile(SCRIPT, script)
            library = project / 'Vehicle Manuals'
            library.mkdir()
            result = subprocess.run(
                [sys.executable, str(script), 'vehicles', '--json'], cwd=root,
                capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data['library_root'], str(library.resolve()))
            self.assertEqual(data['status'], 'no_indexes')


class ManualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.make_vehicle('Alpha', 'Brake service',
            'Brake pads: inspect wear. WARNING: Support the vehicle safely.')
        self.other = self.make_vehicle('Beta', 'Cooling service', 'Water pump replacement.')

    def make_vehicle(self, vehicle, title, body):
        source = self.root / vehicle / 'Original'
        source.mkdir(parents=True)
        (source / 'page.html').write_text(
            f'<title>{title}</title><p>{body}</p><img src="diagram.png" alt="Diagram">'
            '<a href="absent.html">Next</a><a href="https://example.com">External</a>',
            encoding='utf-8')
        (source / 'diagram.png').write_bytes(b'fixture')
        db = source.parent / 'Index' / 'manual.sqlite'
        result = cli('index', '--source', source, '--db', db, '--vehicle', vehicle)
        self.assertEqual(result.returncode, 0, result.stderr)
        return db

    def query(self, *args, vehicle='Alpha'):
        return cli('search', '--library-root', self.root, '--vehicle', vehicle, *args)

    def test_list_vehicles(self):
        r = cli('vehicles', '--library-root', self.root, '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual([v['vehicle'] for v in data['vehicles']], ['Alpha', 'Beta'])
        self.assertTrue(all(v['source_exists'] for v in data['vehicles']))

    def test_plain_language_punctuation_and_reserved_words(self):
        for query in ('brake pads?', 'brake-pads', 'brake AND pads', 'brake "pads',
                      'brake: pads', 'How do I inspect brake pads?'):
            with self.subTest(query=query):
                r = self.query(query, '--json')
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(json.loads(r.stdout)['results']), 1)

    def test_advanced_fts(self):
        r = self.query('brak* OR cooling', '--fts', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads(r.stdout)['results']), 1)
        r = self.query('"unclosed', '--fts', '--json')
        self.assertEqual(r.returncode, 1)
        self.assertIn('error', json.loads(r.stdout))

    def test_isolation_and_no_results(self):
        r = self.query('water pump', '--json')
        d = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(d['status'], 'no_results')
        self.assertEqual(d['vehicle'], 'Alpha')
        self.assertEqual(d['results'], [])
        self.assertIn('No results', self.query('water pump').stdout)

    def test_selection_requires_unambiguous_vehicle(self):
        r = cli('search', '--library-root', self.root, 'brake', '--json')
        self.assertEqual(r.returncode, 1)
        self.assertIn('vehicle', json.loads(r.stdout)['error'].lower())
        r = self.query('brake', vehicle='alpha', )
        self.assertEqual(r.returncode, 1)

    def test_duplicate_label_is_ambiguous(self):
        duplicate = self.root / 'duplicate.sqlite'
        shutil.copyfile(self.db, duplicate)
        r = self.query('brake', '--json')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ambiguous', json.loads(r.stdout)['error'].lower())
        r = cli('search', '--db', self.db, '--vehicle', 'Alpha', 'brake', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_explicit_db_retains_vehicle_guard(self):
        for vehicle in (None, 'Beta'):
            args = ['search', '--db', self.db, 'brake', '--json']
            if vehicle:
                args += ['--vehicle', vehicle]
            r = cli(*args)
            self.assertEqual(r.returncode, 1)
        r = cli('search', '--db', self.db, '--vehicle', 'Alpha', 'brake')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Vehicle: Alpha', r.stdout)
        self.assertIn('Citation:', r.stdout)

    def test_read_structured_context_and_live_link_status(self):
        (self.root / 'Alpha/Original/diagram.png').unlink()
        r = cli('read', '--db', self.db, '--vehicle', 'Alpha', 'page.html', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d['vehicle'], 'Alpha')
        self.assertIn('Support', d['body'])
        self.assertTrue(d['citation'].startswith('file:'))
        self.assertTrue(d['source_exists'])
        self.assertTrue(d['warnings'])
        self.assertEqual(d['diagrams'][0]['status'], 'missing')
        self.assertFalse(d['diagrams'][0]['missing_at_index'])
        self.assertEqual(d['links'][1]['status'], 'missing')
        self.assertEqual(d['links'][2]['status'], 'unchecked')

    def test_missing_source_still_reads_index(self):
        shutil.rmtree(self.root / 'Alpha/Original')
        for command, value in [('search', 'brake'), ('read', 'page.html')]:
            r = cli(command, '--db', self.db, '--vehicle', 'Alpha', value, '--json')
            self.assertEqual(r.returncode, 0, r.stderr)
            d = json.loads(r.stdout)
            record = d['results'][0] if command == 'search' else d
            self.assertFalse(record['source_exists'])
            self.assertTrue(any('source' in w.lower() for w in d['warnings']))

    def test_missing_single_source_page(self):
        (self.root / 'Alpha/Original/page.html').unlink()
        r = self.query('brake', '--json')
        d = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(d['results'][0]['source_exists'])
        self.assertTrue(any('Source page is missing' in w for w in d['warnings']))

    def test_empty_and_missing_library(self):
        empty = self.root / 'empty'
        empty.mkdir()
        r = cli('--library-root', empty, '--json', 'vehicles')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['status'], 'no_indexes')
        r = cli('vehicles', '--library-root', empty / 'absent', '--json')
        self.assertEqual(r.returncode, 1)
        self.assertIn('Library root not found', json.loads(r.stdout)['error'])

    def test_explicit_db_overrides_library_root(self):
        r = cli('--library-root', self.root / 'absent', '--json', 'search',
                '--db', self.db, '--vehicle', 'Alpha', 'brake')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['vehicle'], 'Alpha')

    def test_missing_db_and_invalid_json_arguments(self):
        for args in [('search', 'brake', '--db', self.root / 'absent.db', '--vehicle', 'Alpha'),
                     ('search',), ('search', 'brake', '--limit', 'abc')]:
            r = cli(*args, '--json')
            self.assertEqual(r.returncode, 1)
            self.assertEqual(json.loads(r.stdout)['status'], 'error')
        self.assertFalse((self.root / 'absent.db').exists())

    def test_limits_empty_query_and_missing_page(self):
        for args in [('search', '!!!'), ('search', 'brake', '--limit', '0'),
                     ('search', 'brake', '--limit', '-1'), ('read', '../absent.html')]:
            r = cli(*args, '--db', self.db, '--vehicle', 'Alpha', '--json')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('error', json.loads(r.stdout))

    def test_broken_json(self):
        r = cli('broken', '--db', self.db, '--vehicle', 'Alpha', '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['missing_occurrences'], 1)

    def test_read_only(self):
        before = self.db.read_bytes()
        for args in [('search', 'brake'), ('read', 'page.html'), ('broken',)]:
            r = cli(*args, '--db', self.db, '--vehicle', 'Alpha')
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(before, self.db.read_bytes())

    def test_index_never_overwrites_or_writes_source(self):
        source = self.root / 'Alpha/Original'
        for dest in (self.db, source / 'new.sqlite'):
            r = cli('index', '--source', source, '--db', dest, '--vehicle', 'Alpha')
            self.assertEqual(r.returncode, 1)
        self.assertFalse((source / 'new.sqlite').exists())

    def test_discovery_ignores_unrelated_sqlite(self):
        with closing(sqlite3.connect(self.root / 'unrelated.db')) as c:
            c.execute('CREATE TABLE unrelated(value)')
            c.commit()
        r = cli('vehicles', '--library-root', self.root, '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(json.loads(r.stdout)['vehicles']), 2)
        self.assertTrue(json.loads(r.stdout)['warnings'])

    def test_extract_safety_and_original_archive(self):
        for name in ('root/../evil', 'root/./evil', 'root/CON.txt', 'root/a\\b', 'root/C:evil'):
            archive = self.root / 'test.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                info = zipfile.ZipInfo('placeholder')
                # Bypass Windows ZipInfo constructor's separator normalization.
                info.filename = info.orig_filename = name
                z.writestr(info, 'bad')
            original = archive.read_bytes()
            r = cli('extract', '--zip', archive, '--dest', self.root / 'extracted')
            self.assertEqual(r.returncode, 1, name)
            self.assertFalse((self.root / 'extracted').exists())
            self.assertEqual(original, archive.read_bytes())

    def test_extract_collisions_symlinks_and_multiple_roots(self):
        for names in [('root/A.html', 'root/a.html'), ('root/a', 'root/a/b'),
                      ('one/a', 'two/b'), ('only-file',)]:
            archive = self.root / 'invalid.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                for name in names:
                    z.writestr(name, 'fixture')
            r = cli('extract', '--zip', archive, '--dest', self.root / 'rejected')
            self.assertEqual(r.returncode, 1, names)
            self.assertFalse((self.root / 'rejected').exists())
        with zipfile.ZipFile(archive, 'w') as z:
            info = zipfile.ZipInfo('root/link')
            info.create_system = 3
            info.external_attr = 0o120777 << 16
            z.writestr(info, '../outside')
        self.assertEqual(cli('extract', '--zip', archive, '--dest', self.root / 'rejected').returncode, 1)
        self.assertFalse((self.root / 'rejected').exists())

    def test_build_failure_removes_only_new_index(self):
        source = self.root / 'bad/Original'
        source.mkdir(parents=True)
        original = '<p>Fixture</p><a href="http://[invalid">broken</a>'
        (source / 'page.html').write_text(original)
        db = source.parent / 'Index/manual.sqlite'
        r = cli('index', '--source', source, '--db', db, '--vehicle', 'Bad')
        self.assertEqual(r.returncode, 1)
        self.assertFalse(db.exists())
        self.assertEqual((source / 'page.html').read_text(), original)

    def test_outside_links_never_get_citations(self):
        source = self.root / 'Outside/Original'
        source.mkdir(parents=True)
        (source / 'page.html').write_text('<p>fixture</p><img src="../secret.png">')
        db = source.parent / 'Index/manual.sqlite'
        self.assertEqual(cli('index', '--source', source, '--db', db, '--vehicle', 'Outside').returncode, 0)
        r = cli('read', 'page.html', '--db', db, '--vehicle', 'Outside', '--json')
        link = json.loads(r.stdout)['diagrams'][0]
        self.assertEqual(link['status'], 'outside_manual')
        self.assertIsNone(link['citation'])

    def test_extract_valid_and_refuses_existing(self):
        archive = self.root / 'valid.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('root/page.html', '<p>fixture</p>')
        dest = self.root / 'extracted'
        self.assertEqual(cli('extract', '--zip', archive, '--dest', dest).returncode, 0)
        self.assertEqual((dest / 'page.html').read_text(), '<p>fixture</p>')
        self.assertEqual(cli('extract', '--zip', archive, '--dest', dest).returncode, 1)


class LibraryDestinationTests(unittest.TestCase):
    """Exercise the default-library policy using only disposable fixture paths."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / 'Vehicle Manuals'
        self.library.mkdir()
        self.default = patch.object(manual, 'DEFAULT_LIBRARY', self.library)
        self.default.start()
        self.addCleanup(self.default.stop)
        self.source = self.library / 'Existing/Original'
        self.source.mkdir(parents=True)
        (self.source / 'page.html').write_bytes(b'<title>Fixture</title><p>Brake service</p>')
        self.archive = self.library / 'vehicle.zip'
        with zipfile.ZipFile(self.archive, 'w') as z:
            z.writestr('root/page.html', '<p>New fixture</p>')
        self.before = self.snapshot()

    def snapshot(self):
        return {p.relative_to(self.library).as_posix(): p.read_bytes()
                for p in self.library.rglob('*') if p.is_file()}

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = manual.main(list(map(str, args)))
        return result, out.getvalue() + err.getvalue()

    def assert_originals_unchanged(self):
        after = self.snapshot()
        for name, content in self.before.items():
            self.assertEqual(after[name], content, name)

    def test_new_extraction_under_default_library(self):
        dest = self.library / 'New Vehicle/Original'
        code, output = self.run_command('extract', '--zip', self.archive, '--dest', dest)
        self.assertEqual(code, 0, output)
        self.assertEqual((dest / 'page.html').read_text(), '<p>New fixture</p>')
        self.assert_originals_unchanged()

    def test_new_index_under_default_library(self):
        db = self.library / 'Existing/Index/manual.sqlite'
        code, output = self.run_command('index', '--source', self.source,
                                        '--db', db, '--vehicle', 'Fixture')
        self.assertEqual(code, 0, output)
        with closing(manual.connect_index(db)[0]) as c:
            self.assertEqual(c.execute('SELECT path FROM pages').fetchall(), [('page.html',)])
        self.assert_originals_unchanged()
        before = self.snapshot()
        code, output = self.run_command('index', '--source', self.source,
                                        '--db', db, '--vehicle', 'Replacement')
        self.assertEqual(code, 1, output)
        self.assertIn('Index already exists', output)
        self.assertEqual(before, self.snapshot())

    def test_existing_destinations_remain_protected(self):
        empty = self.library / 'Empty'
        empty.mkdir()
        existing_index = self.library / 'existing.sqlite'
        existing_index.write_bytes(b'existing index sentinel')
        before = self.snapshot()
        for dest in (self.source, empty, self.source / 'page.html', existing_index):
            with self.subTest(dest=dest):
                code, output = self.run_command('extract', '--zip', self.archive, '--dest', dest)
                self.assertEqual(code, 1, output)
                self.assertIn('Destination already exists', output)
                code, output = self.run_command('index', '--source', self.source,
                                                '--db', dest, '--vehicle', 'Fixture')
                self.assertEqual(code, 1, output)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(list(empty.iterdir()), [])

    def test_index_inside_source_remains_forbidden(self):
        code, output = self.run_command('index', '--source', self.source,
            '--db', self.source / 'nested/new.sqlite', '--vehicle', 'Fixture')
        self.assertEqual(code, 1, output)
        self.assertIn('Index must be outside the source directory', output)
        self.assertFalse((self.source / 'nested').exists())
        self.assertEqual(self.before, self.snapshot())

    def test_unsafe_zip_under_default_library_remains_forbidden(self):
        archive = Path(self.temp.name) / 'unsafe.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('root/../escape.html', 'unsafe fixture')
        dest = self.library / 'New Vehicle/Original'
        code, output = self.run_command('extract', '--zip', archive, '--dest', dest)
        self.assertEqual(code, 1, output)
        self.assertIn('Unsafe ZIP path', output)
        self.assertFalse(dest.parent.exists())
        self.assertEqual(self.before, self.snapshot())


class RealCadillacTests(unittest.TestCase):
    @unittest.skipUnless(REAL_ROOT.exists(), 'Local Cadillac library unavailable')
    def test_existing_cadillac_search_read_readonly(self):
        db = REAL_ROOT / '2013 Cadillac XTS FWD/Index/manual.sqlite'
        self.assertTrue(db.is_file())
        digest = hashlib.sha256(db.read_bytes()).digest()
        stat_before = db.stat()
        index_files_before = sorted(p.name for p in db.parent.iterdir())
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as c:
            meta = dict(c.execute('SELECT key,value FROM meta'))
        r = cli('search', '--db', db, '--vehicle', meta['vehicle'], 'water pump', '--json', '--limit', '2')
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertTrue(d['results'])
        page = d['results'][0]['path']
        source = Path(meta['source']) / page
        source_before = source.read_bytes()
        r = cli('read', '--db', db, '--vehicle', meta['vehicle'], page, '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)['body'])
        self.assertEqual(source_before, source.read_bytes())
        self.assertEqual(digest, hashlib.sha256(db.read_bytes()).digest())
        self.assertEqual(stat_before.st_mtime_ns, db.stat().st_mtime_ns)
        self.assertEqual(index_files_before, sorted(p.name for p in db.parent.iterdir()))
        r = cli('vehicles', '--library-root', REAL_ROOT, '--json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(meta['vehicle'], [v['vehicle'] for v in json.loads(r.stdout)['vehicles']])


if __name__ == '__main__':
    unittest.main()
