#!/usr/bin/env python3.6
"""Render a tree from a predicate root pair.
Normally run as a web service.

Usage:
    ontree server [options]
    ontree [options] <predicate-curie> <root-curie>
    ontree --test

Options:
    -a --api=API            Full url to SciGraph api endpoint
    -k --key=APIKEY         apikey for SciGraph instance
    -p --port=PORT          port on which to run the server [default: 8000]
    -f --input-file=FILE    don't use SciGraph, load an individual file instead
    -o --outgoing           if not specified defaults to incoming
    -b --both               if specified goes in both directions
    -t --test               run tests
    -v --verbose            print extra information

"""

from collections import defaultdict, OrderedDict
import os
import re
import asyncio
import subprocess
from pprint import pprint
from pathlib import Path
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import parse_qs
import rdflib
import htmlfn as hfn
import ontquery as oq
from flask import Flask, url_for, redirect, request, render_template, render_template_string, make_response, abort, current_app, send_from_directory
from docopt import docopt, parse_defaults
from htmlfn import htmldoc, titletag, atag, ptag, nbsp
from htmlfn import render_table, table_style
from pyontutils import scigraph
from pyontutils.core import makeGraph, qname, OntId, OntTerm
from pyontutils.utils import getSourceLine, get_working_dir, makeSimpleLogger
from pyontutils.utils import Async, deferred, UTCNOWISO
from pyontutils.config import devconfig
from pyontutils.ontload import import_tree
from pyontutils.hierarchies import Query, creatTree, dematerialize, flatten as flatten_tree
from pyontutils.closed_namespaces import rdfs
from pyontutils.sheets import Sheet
from nifstd.development.sparc.sheets import hyperlink_tree, tag_row, open_custom_sparc_view_yml, YML_DELIMITER
from typing import Union, Dict, List
from IPython import embed
import yaml

log = makeSimpleLogger('ontree')

sgg = scigraph.Graph(cache=False, verbose=True)
sgv = scigraph.Vocabulary(cache=False, verbose=True)
sgc = scigraph.Cypher(cache=False, verbose=True)
sgd = scigraph.Dynamic(cache=False, verbose=True)

a = 'rdfs:subClassOf'
_hpp = 'RO_OLD:has_proper_part'  # and apparently this fails too
hpp = 'http://www.obofoundry.org/ro/ro.owl#has_proper_part'
hpp = 'NIFRID:has_proper_part'
po = 'BFO:0000050'  # how?! WHY does this fail!? the curie is there!
_po = 'http://purl.obolibrary.org/obo/BFO_0000050'
hr = 'RO:0000087'
_hr = 'http://purl.obolibrary.org/obo/RO_0000087'

inc = 'INCOMING'
out = 'OUTGOING'
both = 'BOTH'


def time():
    return str(datetime.utcnow().isoformat()).replace('.', ',')


class ImportChain:  # TODO abstract this a bit to support other onts, move back to pyontutils
    def __init__(self, sgg=sgg, sgc=sgc, wasGeneratedBy='FIXME#L{line}'):
        self.sgg = sgg
        self.sgc = sgc
        self.wasGeneratedBy = wasGeneratedBy

    def get_scigraph_onts(self):
        self.results = self.sgc.execute('MATCH (n:Ontology) RETURN n', 1000)
        return self.results

    def get_itrips(self):
        results = self.get_scigraph_onts()
        iris = sorted(set(r['iri'] for r in results))
        gin = lambda i: (i, self.sgg.getNeighbors(i, relationshipType='isDefinedBy',
                                                  direction='OUTGOING'))
        nodes = Async()(deferred(gin)(i) for i in iris)
        imports = [(i, *[(e['obj'], 'owl:imports', e['sub'])
                         for e in n['edges']])
                   for i, n in nodes if n]
        self.itrips = sorted(set(tuple(rdflib.URIRef(OntId(e).iri) for e in t)
                                 for i, *ts in imports if ts for t in ts))
        return self.itrips

    def make_import_chain(self, ontology='nif.ttl'):
        itrips = self.get_itrips()
        if not any(ontology in t[0] for t in itrips):
            return None, None

        ontologies = ontology,  # hack around bad code in ontload
        import_graph = rdflib.Graph()
        [import_graph.add(t) for t in itrips]

        self.tree, self.extra = next(import_tree(import_graph, ontologies))
        return self.tree, self.extra

    def make_html(self):
        line = getSourceLine(self.__class__)
        wgb = self.wasGeneratedBy.format(line=line)
        prov = makeProv('owl:imports', 'NIFTTL:nif.ttl', wgb)
        tree, extra  = self.make_import_chain()
        if tree is None:
            html_all = ''
        else:
        
            html = extra.html.replace('NIFTTL:', '')
            html_all = hfn.htmldoc(html,
                                   other=prov,
                                   styles=hfn.tree_styles)

        self.html = html_all
        return html_all

    def write_import_chain(self, location='/tmp/'):
        html = self.make_html()
        if not html:
            self.path = '/tmp/noimport.html'
        else:
            self.name = Path(next(iter(tree.keys()))).name
            self.path = Path(location, f'{self.name}-import-closure.html')

        with open(self.path.as_posix(), 'wt') as f:
            f.write(html)  # much more readable


def graphFromGithub(link, verbose=False):
    # mmmm no validation
    # also caching probably
    if verbose:
        log.info(link)
    return makeGraph('', graph=rdflib.Graph().parse(f'{link}?raw=true', format='turtle'))


def makeProv(pred, root, wgb):
    return [titletag(f'Transitive closure of {root} under {pred}'),
            f'<meta name="date" content="{UTCNOWISO()}">',
            f'<link rel="http://www.w3.org/ns/prov#wasGeneratedBy" href="{wgb}">']


def connectivity_query(relationship=None, start=None, end=None):
    j = sgd.dispatch('/dynamic/shortestSimple?'
                     'start_id={start.quoted}&'
                     'end_id={end.quoted}&'
                     'relationship={relationship}')

    kwargs['json'] = j
    tree, extras = creatTree(*Query(root, pred, direction, depth), **kwargs)
    return htmldoc(extras.html, styles=hfn.tree_styles)

def render(pred, root, direction=None, depth=10, local_filepath=None, branch='master',
           restriction=False, wgb='FIXME', local=False, verbose=False, flatten=False,):

    kwargs = {'local':local, 'verbose':verbose}
    prov = makeProv(pred, root, wgb)
    if local_filepath is not None:
        github_link = f'https://github.com/SciCrunch/NIF-Ontology/raw/{branch}/{local_filepath}'
        prov.append(f'<link rel="http://www.w3.org/ns/prov#wasDerivedFrom" href="{github_link}">')
        g = graphFromGithub(github_link, verbose)
        labels_index = {g.qname(s):str(o) for s, o in g.g[:rdfs.label:]}
        if pred == 'subClassOf':
            pred = 'rdfs:subClassOf'  # FIXME qname properly?
        elif pred == 'subPropertyOf':
            pred = 'rdfs:subPropertyOf'
        try:
            kwargs['json'] = g.make_scigraph_json(pred, direct=not restriction)
            kwargs['prefixes'] = {k:str(v) for k, v in g.namespaces.items()}
        except KeyError as e:
            if verbose:
                log.error(str(e))
            return abort(422, 'Unknown predicate.')
    else:
        kwargs['graph'] = sgg
        # FIXME this does not work for a generic scigraph load ...
        # and it should not be calculated every time anyway!
        # oh look, here we are needed a class again
        if False:
            versionIRI = [e['obj']
                        for e in sgg.getNeighbors('http://ontology.neuinfo.org/'
                                                  'NIF/ttl/nif.ttl')['edges']
                        if e['pred'] == 'versionIRI'][0]
            #print(versionIRI)
            prov.append(f'<link rel="http://www.w3.org/ns/prov#wasDerivedFrom" href="{versionIRI}">')  # FIXME wrong and wont resolve
        prov.append('<meta name="representation" content="SciGraph">')  # FIXME :/
    kwargs['html_head'] = prov
    try:
        if root.startswith('http'):  # FIXME this codepath is completely busted?
            if 'prefixes' in kwargs:
                rec = None
                for k, v in kwargs.items():
                    if root.startswith(v):
                        rec = k + 'r:' + root.strip(v)  # FIXME what?!
                        break
                if rec is None:
                    raise KeyError('no prefix found for {root}')
            else:
                rec = sgv.findById(root)
            if 'curie' in rec:
                root_curie = rec['curie']
                # FIXME https://github.com/SciGraph/SciGraph/issues/268
                if not root_curie.endswith(':') and '/' not in root_curie:
                    root = root_curie
                else:
                    kwargs['curie'] = root_curie
        elif 'prefixes' not in kwargs and root.endswith(':'):
            kwargs['curie'] = root
            root = sgc._curies[root.rstrip(':')]  # also 268

        tree, extras = creatTree(*Query(root, pred, direction, depth), **kwargs)
        dematerialize(list(tree.keys())[0], tree)
        if flatten:
            if local_filepath is not None:
                def safe_find(n):
                    return {'labels':[labels_index[n]],
                            'deprecated': False  # FIXME inacurate
                           }

            else:
                def safe_find(n):  # FIXME scigraph bug
                    if n.endswith(':'):
                        n = sgc._curies[n.rstrip(':')]
                    elif '/' in n:
                        prefix, suffix = n.split(':')
                        iriprefix = sgc._curies[prefix]
                        n = iriprefix + suffix

                    return sgv.findById(n)

            out = set(n for n in flatten_tree(extras.hierarchy))

            try:
                lrecs = Async()(deferred(safe_find)(n) for n in out)
            except RuntimeError:
                asyncio.set_event_loop(current_app.config['loop'])
                lrecs = Async()(deferred(safe_find)(n) for n in out)

            rows = sorted(((r['labels'][0] if r['labels'] else '')
                           + ',' + n for r, n in zip(lrecs, out)
                           # FIXME still stuff wrong, but better for non cache case
                           if not r['deprecated']), key=lambda lid: lid.lower())
            return '\n'.join(rows), 200, {'Content-Type':'text/plain;charset=utf-8'}

        else:
            return hfn.htmldoc(extras.html,
                               other=prov,
                               styles=hfn.tree_styles)

    except (KeyError, TypeError) as e:
        if verbose:
            log.error(f'{type(e)} {e}')
        if sgg.getNode(root):
            message = 'Unknown predicate or no results.'  # FIXME distinguish these cases...
        elif 'json' in kwargs:
            message = 'Unknown root.'
            r = g.expand(root)
            for s in g.g.subjects():
                if r == s:
                    message = "No results. You are querying a ttl file directly, did you remember to set ?restriction=true?"
                    break
        else:
            message = 'Unknown root.'

        return abort(422, message)


class fakeRequest:
    args = {}


def getArgs(request):
    want = {'direction':inc,  # INCOMING OUTGOING BOTH
            'depth':10,
            'branch':'master',
            'restriction':False,  # True False
            'local':False,  # True False  # canonoical vs scigraph ? interlex?
            'flatten':False,
           }

    def convert(k):
        if k in request.args:
            v = request.args[k]
        else:
            return want[k]

        if isinstance(want[k], bool):
            if v.lower() == 'true':
                return True
            elif v.lower() == 'false':
                return False
            else:
                raise TypeError(f'Expected a bool, got "{v}" instead.')
        elif isinstance(want[k], int):
            try:
                return int(v)
            except (TypeError, ValueError) as e:
                raise TypeError(f'Expected an int, got "{v}" instead.') from e
        else:
            return v


    return {k:convert(k)
            for k, v in want.items()}

def sanitize(pred, kwargs):
    if pred == 'isDefinedBy' and kwargs['depth'] > 1:
        return abort(400, 'isDefinedBy not allowed for queries with depth > 1.')

examples = (
    ('Brain parts', hpp, 'UBERON:0000955', '?direction=OUTGOING'),  # FIXME direction=lol doesn't cause issues...
    ('Brain parts alt', po, 'UBERON:0000955'),
    ('Brain parts alt flat', po, 'UBERON:0000955', '?flatten=true'),
    ('Anatomical entities', a, 'UBERON:0001062'),
    ('Cell parts', a, 'GO:0044464'),
    ('Cells', a, 'SAO:1813327414'),
    ('Proteins', a, 'SAO:26622963'),
    ('GPCRs', a, 'NIFEXT:5012'),
    ('Mulitmeric ion channels', a, 'NIFEXT:2502'),
    ('Monomeric ion channels', a, 'NIFEXT:2500'),
    ('Diseases', a, 'DOID:4'),
    ('Vertebrata', a, 'NCBITaxon:7742', '?depth=40'),
    ('Metazoa', a, 'NCBITaxon:33208', '?depth=40'),
    ('Rodentia', a, 'NCBITaxon:9989'),
    ('Insecta', a, 'NCBITaxon:50557', '?depth=40'),
    ('Neurotransmitters', hr, 'CHEBI:25512'),
    ('Neurotransmitters', a, 'NLXMOL:100306'),
    ('IRIs ok for roots', a, 'http://uri.neuinfo.org/nif/nifstd/nlx_mol_100306'),
    ('Provenance', 'isDefinedBy',
     'http://ontology.neuinfo.org/NIF/ttl/generated/chebislim.ttl', '?depth=1'),
)

extra_examples = (
    ('Cell septum', a, 'GO:0044457'),
    ('Old NIFGA part of', hpp, 'BIRNLEX:796'),
    ('Cereberal cortex parts', po, 'UBERON:0002749'),
    ('Broken iri of borken curie', a, 'http://uri.interlex.org/paxinos/uris/rat/labels/'),
    ('Broken curie', a, 'PAXRAT:'),
)

file_examples = (
    ('Resources', a, 'NLXRES:20090101', 'ttl/resources.ttl'),
    ('Staging branch', a, 'PAXRAT:',
     'ttl/generated/parcellation/paxinos-rat-labels.ttl', '?branch=staging'),
    ('Restriction example', hpp, 'UBERON:0000955',
     'ttl/bridge/uberon-bridge.ttl', '?direction=OUTGOING&restriction=true'),
)

dynamic_examples = (
    ('Shortest path', 'shortestSimple',
     '?start_id=UBERON:0000955&end_id=UBERON:0001062&relationship=subClassOf'),
    ('Shortest path table', 'shortestSimple',
     '?start_id=UBERON:0000955&end_id=UBERON:0001062&relationship=subClassOf&format=table'),
    ('Stomach parts', 'prod/sparc/organParts/FMA:7148', None),
    ('Parc graph', 'prod/sparc/parcellationGraph', '?direction=INCOMING'),
    ('Parc arts', 'prod/sparc/parcellationArtifacts/NCBITaxon:10116', '?direction=INCOMING'),
    ('Parc arts', 'prod/sparc/parcellationRoots/NCBITaxon:10116', '?direction=INCOMING'),
)

def server(api_key=None, verbose=False):
    f = Path(__file__).resolve()
    working_dir = get_working_dir(__file__)
    if working_dir:
        git_dir = working_dir / '.git'
    else:
        git_dir = Path('/dev/null')

    try:
        commit = subprocess.check_output(['git',
                                          '--git-dir', git_dir.as_posix(),
                                          '--work-tree', working_dir.as_posix(),
                                          'rev-parse', 'HEAD'],
                                         stderr=subprocess.DEVNULL).decode().rstrip()
    except subprocess.CalledProcessError:
        commit = 'master' # 'NO-REPO-AT-MOST-TODO-GET-LATEST-HASH'
    wasGeneratedBy = ('https://github.com/tgbugs/pyontutils/blob/'
                      f'{commit}/pyontutils/{f.name}'
                      '#L{line}')
    line = getSourceLine(render)
    wgb = wasGeneratedBy.format(line=line)

    importchain = ImportChain(wasGeneratedBy=wasGeneratedBy)
    importchain.make_html()  # run this once, restart services on a new release

    loop = asyncio.get_event_loop()
    app = Flask('ontology tree service')
    app.config['loop'] = loop

    # gsheets = GoogleSheets()
    sparc_view = open_custom_sparc_view_yml()
    log.info('starting index load')

    basename = 'trees'

    @app.route(f'/{basename}', methods=['GET'])
    @app.route(f'/{basename}/', methods=['GET'])
    def route_():
        d = url_for('route_docs')
        e = url_for('route_examples')
        i = url_for('route_import_chain')
        return htmldoc(atag(d, 'Docs'),
                       '<br>',
                       atag(e, 'Examples'),
                       '<br>',
                       atag(i, 'Import chain'),
                       title='NIF ontology hierarchies')

    @app.route(f'/{basename}/docs', methods=['GET'])
    def route_docs():
        return redirect('https://github.com/SciCrunch/NIF-Ontology/blob/master/docs')  # TODO

    @app.route(f'/{basename}/examples', methods=['GET'])
    def route_examples():
        links = render_table([[name,
                               atag(url_for("route_query", pred=pred, root=root) + (args[0] if args else ''),
                                    f'../query/{pred}/{root}{args[0] if args else ""}')]
                              for name, pred, root, *args in examples],
                             'Root class', '../query/{predicate-curie}/{root-curie}?direction=INCOMING&depth=10&branch=master&local=false',
                             halign='left')

        flinks = render_table([[name,
                                atag(url_for("route_filequery", pred=pred, root=root, file=file) + (args[0] if args else ''),
                                     f'../query/{pred}/{root}/{file}{args[0] if args else ""}')]
                               for name, pred, root, file, *args in file_examples],
                              'Root class', '../query/{predicate-curie}/{root-curie}/{ontology-filepath}?direction=INCOMING&depth=10&branch=master&restriction=false',
                              halign='left')

        dlinks = render_table([[name,
                                atag(url_for("route_dynamic", path=path) + (querystring if querystring else ''),
                                     f'../query/dynamic/{path}{querystring if querystring else ""}')]
                               for name, path, querystring in dynamic_examples],
                              'Root class', '../query/dynamic/{path}?direction=OUTGOING&dynamic=query&args=here',
                              halign='left')



        return htmldoc(links, flinks, dlinks, title='Example hierarchy queries')

    @app.route(f'/{basename}/sparc/connectivity/query', methods=['GET'])
    def route_sparc_connectivity_query():
        kwargs = request.args
        log.debug(kwargs)
        return hfn.htmldoc('form here',
            title='Connectivity query')
        return connectivity_query(**kwargs)

    @app.route(f'/{basename}/sparc/connectivity/view', methods=['GET'])
    def route_sparc_connectivity_view():
        kwargs = request.args
        log.debug(kwargs)
        return hfn.htmldoc(title='Connectivity view')

    @app.route(f'/{basename}/dynamic/<path:path>', methods=['GET'])
    def route_dynamic(path):
        args = dict(request.args)
        if 'direction' in args:
            direction = args.pop('direction')
        else:
            direction = 'OUTGOING'  # should always be outgoing here since we can't specify?

        if 'format' in args:
            format_ = args.pop('format')
        else:
            format_ = None

        j = sgd.dispatch(path, **args)
        if not j['edges']:
            log.error(pprint(j))
            return abort(400)

        kwargs = {'json': j}
        tree, extras = creatTree(*Query(None, None, direction, None), **kwargs)
        #print(extras.hierarhcy)
        print(tree)
        if format_ is not None:
            if format_ == 'table':
                #breakpoint()
                def nowrap(class_, tag=''):
                    return (f'{tag}.{class_}'
                            '{ white-space: nowrap; }')

                ots = [OntTerm(n) for n in flatten_tree(extras.hierarchy) if 'CYCLE' not in n]
                #rows = [[ot.label, ot.asId().atag(), ot.definition] for ot in ots]
                rows = [[ot.label, hfn.atag(ot.iri, ot.curie), ot.definition] for ot in ots]

                return htmldoc(hfn.render_table(rows, 'label', 'curie', 'definition'),
                               styles=(hfn.table_style, nowrap('col-label', 'td')))

        return htmldoc(extras.html, styles=hfn.tree_styles)

    @app.route(f'/{basename}/imports/chain', methods=['GET'])
    def route_import_chain():
        return importchain.html

    @app.route(f'/{basename}/query/<pred>/<root>', methods=['GET'])
    def route_query(pred, root):
        kwargs = getArgs(request)
        kwargs['wgb'] = wgb
        maybe_abort = sanitize(pred, kwargs)
        if maybe_abort is not None:
            return maybe_abort
        if verbose:
            kwargs['verbose'] = verbose
            log.debug(str(kwargs))
        return render(pred, root, **kwargs)

    @app.route(f'/{basename}/query/<pred>/http:/<path:iri>', methods=['GET'])  # one / due to nginx
    @app.route(f'/{basename}/query/<pred>/https:/<path:iri>', methods=['GET'])  # just in case
    def route_iriquery(pred, iri):  # TODO maybe in the future
        root = 'http://' + iri  # for now we have to normalize down can check request in future
        if verbose:
            log.debug(f'ROOOOT {root}')
        kwargs = getArgs(request)
        kwargs['wgb'] = wgb
        maybe_abort = sanitize(pred, kwargs)
        if maybe_abort is not None:
            return maybe_abort
        if verbose:
            kwargs['verbose'] = verbose
            log.debug(str(kwargs))
        return render(pred, root, **kwargs)

    @app.route(f'/{basename}/query/<pred>/<root>/<path:file>', methods=['GET'])
    def route_filequery(pred, root, file):
        kwargs = getArgs(request)
        kwargs['local_filepath'] = file
        kwargs['wgb'] = wgb
        maybe_abort = sanitize(pred, kwargs)
        if maybe_abort is not None:
            return maybe_abort
        if verbose:
            kwargs['verbose'] = verbose
            log.debug(str(kwargs))
        try:
            return render(pred, root, **kwargs)
        except HTTPError:
            return abort(404, 'Unknown ontology file.')  # TODO 'Unknown git branch.'

    @app.route(f'/{basename}/sparc/view/<tier1>', methods=['GET'])
    @app.route(f'/{basename}/sparc/view/<tier1>/', methods=['GET'])
    @app.route(f'/{basename}/sparc/view/<tier1>/<tier2>', methods=['GET'])
    @app.route(f'/{basename}/sparc/view/<tier1>/<tier2>/', methods=['GET'])
    def route_sparc_view_query(tier1, tier2=None):
        journey = sparc_view
        if tier1 not in journey:
            return abort(404)

        journey = journey[tier1]
        if tier2 is not None:
            if tier2 not in journey:
                return abort(404)
            journey = journey[tier2]

        hyp_rows = hyperlink_tree(journey)

        return htmldoc(
            render_table(hyp_rows),
            title = 'Terms for ' + (tier2 if tier2 is not None else tier1),
            metas = ({'name':'date', 'content':time()},),
        )

    @app.route(f'/{basename}/sparc/view', methods=['GET'])
    @app.route(f'/{basename}/sparc/view/', methods=['GET'])
    def route_sparc_view():
        hyp_rows = []
        spaces = nbsp * 8
        for tier1, tier2_on in sorted(sparc_view.items()):
            url = url_for('route_sparc_view_query', tier1=tier1)
            tier1_row = tier1.split(YML_DELIMITER)
            tier1_row += tier2_on['CURIES']
            tagged_tier1_row = tag_row(tier1_row, url)
            hyp_rows.append(tagged_tier1_row)
            if not tier2_on:
                continue
            # BUG: Will break what we want if more is added to spinal cord
            if len(tier2_on.keys()) > 6:
                continue
            for tier2, tier3_on in tier2_on.items():
                if tier2 == 'CURIES':
                    continue
                url = url_for('route_sparc_view_query', tier1=tier1, tier2=tier2)
                tier2_row = tier2.split(YML_DELIMITER)
                tier2_row += tier3_on['CURIES']
                tagged_tier2_row = tag_row(row=tier2_row, url=url, tier_level=1)
                hyp_rows.append(tagged_tier2_row)
        return htmldoc(
            render_table(hyp_rows),
            title = 'Main Page Sparc',
            styles = ["p {margin: 0px; padding: 0px;}"],
            metas = ({'name':'date', 'content':time()},),
        )

    @app.route(f'/{basename}/sparc/index', methods=['GET'])
    @app.route(f'/{basename}/sparc/index/', methods=['GET'])
    def route_sparc_index():
        hyp_rows = hyperlink_tree(sparc_view)
        return htmldoc(
            render_table(hyp_rows),
            title = 'SPARC Anatomical terms index',
            metas = ({'name':'date', 'content':time()},),
        )

    @app.route(f'/{basename}/sparc', methods=['GET'])
    @app.route(f'/{basename}/sparc/', methods=['GET'])
    def route_sparc():
        # FIXME TODO route to compiled
        p = Path('/var/www/ontology/trees/sparc/sawg.html')
        if p.exists():
            return send_from_directory(p.parent.as_posix(), p.name)
        
        log.critical(f'{devconfig.resources}/sawg.org has not been published')
        return send_from_directory(Path(devconfig.resources).as_posix(), 'sawg.org')
        #return htmldoc(
            #atag(url_for('route_sparc_view'), 'Terms by region or atlas'), '<br>',
            #atag(url_for('route_sparc_index'), 'Index'),
            #title='SPARC Anatomical terms', styles=["p {margin: 0px; padding: 0px;}"],
            #metas = ({'name':'date', 'content':time()},),
        #)

    return app

def test():
    global request
    request = fakeRequest()
    request.args['depth'] = 1
    app = server()
    (route_, route_docs, route_filequery, route_examples, route_iriquery,
     route_query, route_dynamic,
    ) = (app.view_functions[k]
         for k in ('route_', 'route_docs', 'route_filequery',
                   'route_examples', 'route_iriquery', 'route_query',
                   'route_dynamic',))

    for _, path, querystring in dynamic_examples:
        log.info(f'ontree testing {path} {querystring}')
        request = fakeRequest()
        if querystring is not None:
            request.args = {k:v[0] if len(v) == 1 else v
                            for k,v in parse_qs(querystring.strip('?')).items()}
        else:
            request.args = {}

        resp = route_dynamic(path)

    for _, predicate, root, *_ in extra_examples + examples:
        if root == 'UBERON:0001062':
            continue  # too big
        if root == 'PAXRAT:':
            continue  # not an official curie yet

        log.info(f'ontree testing {predicate} {root}')
        if root.startswith('http'):
            root = root.split('://')[-1]  # FIXME nginx behavior...
            resp = route_iriquery(predicate, root)
        else:
            resp = route_query(predicate, root)

        return

    for _, predicate, root, file, *args in file_examples:
        log.info(f'ontree testing {predicate} {root} {file}')
        if args and 'restriction' in args[0]:
            request.args['restriction'] = 'true'

        resp = route_filequery(predicate, root, file)

        if args and 'restriction' in args[0]:
            request.args.pop('restriction')

        
def main():
    from docopt import docopt
    args = docopt(__doc__, version='ontree 0.0.0')
    defaults = {o.name:o.value if o.argcount else None for o in parse_defaults(__doc__)}
    verbose = args['--verbose']
    sgg._verbose = verbose
    sgv._verbose = verbose
    sgc._verbose = verbose

    if args['--test']:
        test()
    elif args['server']:
        api = args['--api']
        if api is not None:
            scigraph.scigraph_client.BASEPATH = api
            sgg._basePath = api
            sgv._basePath = api
            sgc._basePath = api
            # reinit curies state
            sgc.__init__(cache=sgc._get == sgc._cache_get, verbose=sgc._verbose)

        api_key = args['--key']
        if api_key:
            sgg.api_key = api_key
            sgv.api_key = api_key
            sgc.api_key = api_key
            scs = OntTerm.query.services[0]
            scs.api_key = api_key
            scs.setup(instrumented=OntTerm)

        app = server(verbose=verbose)
        app.debug = False
        app.run(host='localhost', port=args['--port'], threaded=True)  # nginxwoo
        # FIXME pypy3 has some serious issues yielding when threaded=True, gil issues?
        os.sys.exit()
    else:
        direction = both if args['--both'] else out if args['--incoming'] else inc
        # TODO default direction table to match to expected query behavior based on rdf direction
        pred = args['<predicate-curie>']
        root = args['<root-curie>']
        render(pred, root, direction)

if __name__ == '__main__':
    main()
