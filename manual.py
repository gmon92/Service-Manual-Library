#!/usr/bin/env python3
"""Local HTML manual index. Standard library only."""
import argparse, json, os, re, sqlite3, sys, zipfile, stat
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

DEFAULT_LIBRARY = Path(__file__).resolve().parent / "Vehicle Manuals"
NOTICE = ('Third-party manual: verify vehicle, safety warnings, procedures and values '
          'against authoritative service information. Read the original HTML for layout and diagrams.')


def within(path, root):
    return path.resolve().is_relative_to(root.resolve())


class Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.text=[]; self.title=[]; self.links=[]; self.skip=0; self.intitle=False
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('script','style'): self.skip+=1
        if tag=='title': self.intitle=True
        if tag in ('a','img','link','script'):
            v=a.get('src') if tag in ('img','script') else a.get('href')
            if v: self.links.append((tag,v,a.get('alt','')))
        if tag in ('p','div','br','li','tr','h1','h2','h3','h4'): self.text.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style') and self.skip: self.skip-=1
        if tag=='title': self.intitle=False
        if tag in ('p','div','li','tr','h1','h2','h3','h4'): self.text.append('\n')
    def handle_data(self, data):
        if self.intitle: self.title.append(data)
        if not self.skip and not self.intitle: self.text.append(data+' ')

def extract(args):
    dest=Path(args.dest).resolve()
    if dest.exists(): raise ValueError('Destination already exists; refusing to overwrite: '+str(dest))
    with zipfile.ZipFile(args.zip) as z:
        members=z.infolist(); seen=set(); roots=set()
        for i in members:
            name=i.filename
            # ZipInfo normalizes Windows separators and truncates NULs when reading.
            raw=i.orig_filename
            if '\\' in raw or raw.startswith('/') or '\x00' in raw: raise ValueError('Unsafe ZIP path: '+repr(raw))
            if any(x in ('.','..','') for x in name.rstrip('/').split('/')): raise ValueError('Unsafe ZIP path: '+name)
            p=PurePosixPath(name); parts=p.parts
            if not parts or any(x in ('.','..') or ':' in x or x.rstrip(' .')!=x for x in parts): raise ValueError('Unsafe ZIP path: '+name)
            if any(re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)', x, re.I) for x in parts): raise ValueError('Reserved Windows path: '+name)
            if stat.S_ISLNK(i.external_attr >> 16): raise ValueError('ZIP symlink: '+name)
            key='/'.join(parts).casefold()
            if key in seen: raise ValueError('Duplicate ZIP path: '+name)
            seen.add(key); roots.add(parts[0])
        if len(roots)!=1: raise ValueError('Expected exactly one archive root')
        regular = {i.filename.rstrip('/').casefold() for i in members if not i.is_dir()}
        for i in members:
            if any('/'.join(PurePosixPath(i.filename).parts[:n]).casefold() in regular
                   for n in range(1, len(PurePosixPath(i.filename).parts))):
                raise ValueError('ZIP file/directory collision: '+i.filename)
        if any(len(PurePosixPath(i.filename).parts)==1 and not i.is_dir() for i in members):
            raise ValueError('Archive root must be a directory')
        bad=z.testzip()
        if bad: raise ValueError('CRC failure: '+bad)
        dest.mkdir(parents=True,exist_ok=False)
        try:
            for i in members:
                parts=PurePosixPath(i.filename).parts[1:]
                if not parts: continue
                out=dest.joinpath(*parts)
                if i.is_dir(): out.mkdir(parents=True,exist_ok=True); continue
                out.parent.mkdir(parents=True,exist_ok=True)
                with z.open(i) as src, out.open('xb') as dst:
                    import shutil; shutil.copyfileobj(src,dst)
        except BaseException:
            print('Extraction incomplete; inspect/remove destination before retry: '+str(dest),file=sys.stderr); raise
    print('Extracted',len(members),'members to',dest)

def target(root, rel, url):
    u=urlsplit(url)
    if u.scheme or u.netloc or not u.path: return None
    path=unquote(u.path).replace('\\','/')
    # Paths are interpreted relative to source, never outside the manual.
    path=os.path.normpath(os.path.join(os.path.dirname(rel),path)).replace('\\','/')
    if path=='..' or path.startswith('../') or path.startswith('/') or ':' in path: return '[outside manual] '+url
    return path

def build(a):
    root=Path(a.source).resolve(); db=Path(a.db).resolve()
    if not root.is_dir(): raise ValueError('Source directory not found')
    if within(db, root): raise ValueError('Index must be outside the source directory')
    if db.exists(): raise ValueError('Index already exists; refusing to overwrite: '+str(db))
    if not a.vehicle.strip(): raise ValueError('Vehicle label must not be empty')
    files=set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            p=Path(directory)/name
            if p.is_symlink() or not within(p, root):
                raise ValueError('Source contains a symlink or escaped path: '+str(p))
        files.update((Path(directory)/name).relative_to(root).as_posix() for name in names)
    html=sorted(p for p in files if p.lower().endswith(('.html','.htm')))
    if not html: raise ValueError('No HTML files found')
    db.parent.mkdir(parents=True,exist_ok=True)
    # Reserve exclusively before SQLite opens it, closing the overwrite race.
    with db.open('xb'): pass
    con=None
    try:
        con=sqlite3.connect(str(db))
        con.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE pages(id INTEGER PRIMARY KEY,path TEXT UNIQUE,title TEXT,body TEXT); CREATE VIRTUAL TABLE search USING fts5(title,body,content="pages",content_rowid="id"); CREATE TABLE links(page_id INTEGER,kind TEXT,url TEXT,alt TEXT,target TEXT,missing INTEGER); CREATE INDEX missing_idx ON links(missing);')
        con.executemany('INSERT INTO meta VALUES (?,?)',[('vehicle',a.vehicle),('source',str(root)),('schema','1')])
        for rel in html:
            p=Parser(); p.feed((root / rel).read_text(encoding='utf-8',errors='replace'))
            title=' '.join(p.title).strip() or rel
            body=re.sub(r'[ \t\r\f\v]+',' ',''.join(p.text)); body=re.sub(r'\n\s*\n+','\n',body).strip()
            cur=con.execute('INSERT INTO pages(path,title,body) VALUES (?,?,?)',(rel,title,body)); pid=cur.lastrowid
            con.execute('INSERT INTO search(rowid,title,body) VALUES (?,?,?)',(pid,title,body))
            links=[]
            for kind,url,alt in p.links:
                t=target(root,rel,url); missing=int(t is not None and t not in files and t.rstrip('/')+'/index.html' not in files)
                links.append((pid,kind,url,alt,t,missing))
            con.executemany('INSERT INTO links VALUES (?,?,?,?,?,?)',links)
        con.commit()
        print('Vehicle:',a.vehicle,'\nPages:',len(html),'\nMissing link occurrences / distinct targets:',*con.execute('SELECT count(*),count(DISTINCT target) FROM links WHERE missing=1').fetchone(),'\nIndex:',db)
    except BaseException:
        if con is not None: con.close()
        db.unlink(missing_ok=True); raise
    con.close()

def connect_index(path):
    """Immutable mode prevents even SQLite journal/SHM writes beside the index."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Index does not exist: ' + str(path))
    c = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        c.execute('PRAGMA query_only=ON')
        m = dict(c.execute('SELECT key,value FROM meta'))
        if m.get('schema') != '1' or not m.get('vehicle') or not m.get('source'):
            raise ValueError('Unsupported manual index metadata: ' + str(path))
        if not Path(m['source']).is_absolute():
            raise ValueError('Index source must be an absolute path')
        c.execute('SELECT id,path,title,body FROM pages LIMIT 0')
        c.execute('SELECT page_id,kind,url,alt,target,missing FROM links LIMIT 0')
        c.execute('SELECT title,body FROM search LIMIT 0')
        return c, m
    except BaseException:
        c.close()
        raise


def discover(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('Library root not found: ' + str(root))
    vehicles, warnings = [], []
    def walk_error(error):
        warnings.append('Cannot scan directory: ' + str(error))
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not (Path(directory)/d).is_symlink()
                         and within(Path(directory)/d, root))
        for name in sorted(names):
            path = Path(directory)/name
            if path.suffix.lower() not in ('.sqlite', '.sqlite3', '.db', '.db3'):
                continue
            if path.is_symlink() or not within(path, root):
                warnings.append('Skipped index symlink: ' + str(path))
                continue
            try:
                c, m = connect_index(path)
                c.close()
                vehicles.append(dict(vehicle=m['vehicle'], db=str(path.resolve()),
                                     source=m['source'], source_exists=Path(m['source']).is_dir()))
            except (ValueError, OSError, sqlite3.Error) as e:
                warnings.append('Skipped index ' + str(path) + ': ' + str(e))
    return sorted(vehicles, key=lambda v: (v['vehicle'], v['db'])), warnings


def open_db(a):
    # An exact vehicle is mandatory even when only one index is installed.
    if not a.vehicle:
        raise ValueError('Specify an exact --vehicle label; use vehicles to list indexes')
    a.discovery_warnings = []
    if not a.db:
        vehicles, a.discovery_warnings = discover(a.library_root)
        matches = [v for v in vehicles if v['vehicle'] == a.vehicle]
        if not matches:
            raise ValueError('No index for vehicle: ' + a.vehicle + '; use vehicles to list indexes')
        if len(matches) != 1:
            raise ValueError('Ambiguous vehicle label: ' + a.vehicle + '; select an explicit --db')
        a.db = matches[0]['db']
    c, m = connect_index(a.db)
    if m['vehicle'] != a.vehicle:
        c.close()
        raise ValueError('Vehicle mismatch; index is for: ' + m['vehicle'])
    return c, m


def source_path(m, relative):
    root = Path(m['source'])
    path = root / relative
    if (not relative or relative.startswith(('/', '\\')) or ':' in relative
            or '..' in relative.replace('\\', '/').split('/') or not within(path, root)):
        raise ValueError('Unsafe path in manual index: ' + relative)
    return path.resolve()


def envelope(a, m):
    warnings = [NOTICE, *a.discovery_warnings]
    if not Path(m['source']).is_dir():
        warnings.append('Source directory is missing; displaying cached index text only: ' + m['source'])
    return dict(command=a.command, status='ok', vehicle=m['vehicle'],
                db=str(Path(a.db).resolve()), source=m['source'], warnings=warnings)


def page_record(c, m, pid, path, title, body):
    source = source_path(m, path)
    warnings = []
    if not source.is_file():
        warnings.append('Source page is missing; displaying cached index text only: ' + str(source))
    # Preserve complete body in read; these excerpts are only a best-effort aid.
    safety = [line.strip() for line in body.splitlines()
              if re.search(r'\b(warning|caution|danger|notice)\b', line, re.I)]
    links = []
    for kind, url, alt, t, missing in c.execute(
            'SELECT kind,url,alt,target,missing FROM links WHERE page_id=? ORDER BY rowid', (pid,)):
        citation, exists = None, None
        if t is None:
            status = 'unchecked'
        elif t.startswith('[outside manual]'):
            status = 'outside_manual'
        else:
            try:
                local = source_path(m, t)
                if local.is_dir():
                    local = source_path(m, t.rstrip('/') + '/index.html')
                citation, exists = local.as_uri(), local.is_file()
                status = 'present' if exists else 'missing'
            except ValueError:
                status = 'outside_manual'
        links.append(dict(kind=kind, url=url, alt=alt, target=t, citation=citation,
                          missing_at_index=bool(missing), exists_now=exists, status=status))
    return dict(path=path, title=title, citation=source.as_uri(), source_exists=source.is_file(),
                warnings=warnings, safety_notices=safety, links=links,
                diagrams=[link for link in links if link['kind'] == 'img'],
                missing_links_at_index=sum(link['missing_at_index'] for link in links),
                missing_links_now=sum(link['status'] == 'missing' for link in links))


# Natural input is a literal AND search, with common question/connector words removed.
STOP_WORDS = set('a an the and or to of for in on with how do does i my is are can '
                 'what where please me show tell about'.split())


def search_query(query, advanced):
    if not query.strip():
        raise ValueError('Search query is empty')
    if advanced:
        return query
    tokens = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    tokens = [t for t in tokens if t.casefold() not in STOP_WORDS]
    if not tokens:
        raise ValueError('Search query needs at least one searchable word')
    return ' AND '.join('"' + t + '"' for t in tokens)


def search(a):
    query = search_query(a.query, a.fts)
    c, m = open_db(a)
    with closing(c):
        data = envelope(a, m)
        try:
            rows = c.execute("""SELECT p.id,p.path,p.title,p.body,
                snippet(search,1,'[',']',' ... ',24)
                FROM search JOIN pages p ON p.id=search.rowid WHERE search MATCH ?
                ORDER BY bm25(search,5,1),p.path LIMIT ?""", (query, a.limit)).fetchall()
        except sqlite3.OperationalError as e:
            if a.fts:
                raise ValueError('Advanced FTS query failed: ' + str(e)) from e
            raise
        data['query'], data['query_mode'] = a.query, 'fts' if a.fts else 'plain'
        data['results'] = []
        for pid, path, title, body, snippet in rows:
            record = page_record(c, m, pid, path, title, body)
            record['snippet'] = snippet
            data['results'].append(record)
            data['warnings'].extend(record['warnings'])
        data['result_count'] = len(rows)
        data['limit'] = a.limit
        if not rows:
            data['status'] = 'no_results'
        return data


def read(a):
    c, m = open_db(a)
    with closing(c):
        row = c.execute('SELECT id,title,body FROM pages WHERE path=?', (a.page,)).fetchone()
        if not row:
            raise ValueError('Page not found in index: ' + a.page)
        pid, title, body = row
        data = envelope(a, m)
        record = page_record(c, m, pid, a.page, title, body)
        record['warnings'] = data['warnings'] + record['warnings']
        data.update(record)
        data['body'] = body
        return data


def broken(a):
    c, m = open_db(a)
    with closing(c):
        data = envelope(a, m)
        data['warnings'].append('Broken link totals are the indexing snapshot, not current file checks.')
        total, distinct = c.execute('SELECT count(*),count(DISTINCT target) FROM links WHERE missing=1').fetchone()
        rows = c.execute('SELECT target,count(*) FROM links WHERE missing=1 GROUP BY target '
                         'ORDER BY count(*) DESC,target LIMIT ?', (a.limit,)).fetchall()
        data.update(missing_occurrences=total, distinct_targets=distinct,
                    targets=[dict(target=t, occurrences=n) for t, n in rows], limit=a.limit)
        return data


def vehicles(a):
    entries, warnings = discover(a.library_root)
    return dict(command='vehicles', status='ok' if entries else 'no_indexes',
                library_root=str(Path(a.library_root).resolve()), vehicles=entries, warnings=warnings)


def print_page(record):
    print(record['path'] + ' | ' + record['title'])
    print('Citation:', record['citation'])
    print('Source exists:', record['source_exists'])
    if 'body' in record:
        print('\n' + record['body'])
    else:
        print('  ' + record['snippet'])
    for notice in record['safety_notices']:
        print('Safety notice (text excerpt):', notice)
    print('Missing links: indexing snapshot:', record['missing_links_at_index'],
          '/ currently:', record['missing_links_now'])
    print('LINKS AND DIAGRAMS:')
    for link in record['links']:
        print(' ', link['kind'], link['url'], '[' + link['status'] + ']',
              '[MISSING at indexing]' if link['missing_at_index'] else '', link['alt'])
        if link['citation']:
            print('   Citation:', link['citation'])


def output(data, as_json):
    if as_json:
        print(json.dumps(data, ensure_ascii=True, indent=2))
        return
    if 'vehicle' in data:
        print('Vehicle:', data['vehicle'])
        print('Index:', data['db'])
    for warning in data['warnings']:
        print('Warning:', warning)
    if data['command'] == 'vehicles':
        for v in data['vehicles']:
            print(v['vehicle'], '|', v['db'], '| source:',
                  'present' if v['source_exists'] else 'MISSING', '|', v['source'])
        print('Indexes:', len(data['vehicles']))
        if not data['vehicles']:
            print('No vehicle indexes found.')
    elif data['command'] == 'search':
        for record in data['results']:
            print_page(record)
        print('No results.' if not data['results'] else 'Results: ' + str(data['result_count']))
    elif data['command'] == 'read':
        print_page(data)
    elif data['command'] == 'broken':
        print('Missing occurrences / distinct targets:', data['missing_occurrences'], data['distinct_targets'])
        for row in data['targets']:
            print(row['occurrences'], row['target'])


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = ArgumentParser(description=__doc__)
    # Support common options before or after the command without overwriting them.
    def common(parser, suppress=False):
        default = argparse.SUPPRESS if suppress else str(DEFAULT_LIBRARY)
        parser.add_argument('--library-root', default=default, help='Root to scan for vehicle indexes')
        parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS if suppress else False,
                            help='Emit one JSON object (search/read/broken/vehicles)')
    common(p)
    sub = p.add_subparsers(dest='command', required=True)
    x = sub.add_parser('extract', help='Safely extract a trusted ZIP into a new directory')
    x.add_argument('--zip', required=True)
    x.add_argument('--dest', required=True)
    x.set_defaults(func=extract)
    x = sub.add_parser('index', help='Build a new schema-1 index without changing the source')
    x.add_argument('--source', required=True)
    x.add_argument('--db', required=True)
    x.add_argument('--vehicle', required=True)
    x.set_defaults(func=build)
    for name, fn in [('vehicles', vehicles), ('search', search), ('read', read), ('broken', broken)]:
        x = sub.add_parser(name)
        common(x, suppress=True)
        x.set_defaults(func=fn)
        if name != 'vehicles':
            x.add_argument('--db', help='Explicit index path; overrides discovery')
            x.add_argument('--vehicle', help='Exact, case-sensitive vehicle label (required)')
        if name == 'search':
            x.add_argument('query', help='Plain search words by default; quote as one shell argument')
            x.add_argument('--fts', action='store_true', help='Interpret query as advanced SQLite FTS5 syntax')
        if name == 'read':
            x.add_argument('page', help='Exact indexed path relative to the manual source')
        if name in ('search', 'broken'):
            x.add_argument('--limit', type=int, default=10 if name == 'search' else 20)
    try:
        a = p.parse_args(argv)
        if getattr(a, 'limit', 1) <= 0:
            raise ValueError('--limit must be a positive integer')
        if a.command in ('extract', 'index') and a.json:
            raise ValueError('--json is supported for vehicles/search/read/broken only')
        data = a.func(a)
        if data is not None:
            output(data, a.json)
    except (ValueError, OSError, sqlite3.Error, zipfile.BadZipFile) as e:
        if '--json' in argv:
            print(json.dumps(dict(status='error', error=str(e)), ensure_ascii=True))
        else:
            print('Error:', e, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
