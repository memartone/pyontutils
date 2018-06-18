#!/usr/bin/env python3.6
#!/usr/bin/env pypy3
__doc__ = f"""Generate NIF parcellation schemes from external resources.

Usage:
    parcellation [options]

Options:
    -f --fail                   fail loudly on common common validation checks
    -j --jobs=NJOBS             number of parallel jobs to run [default: 9]
    -l --local                  only build files with local source copies

"""

import os
import re
import csv
import glob
from pathlib import Path
from collections import namedtuple, defaultdict, Counter
import requests
from git import Repo
from lxml import etree
from rdflib import Graph, URIRef, Literal, Namespace
from pyontutils.core import rdf, rdfs, owl, dc, dcterms, skos, prov
from pyontutils.core import NIFRID, ilx, ilxtr, TEMP, FSLATS
from pyontutils.core import PAXMUS, PAXRAT, paxmusver, paxratver, HCPMMP
from pyontutils.core import NCBITaxon, UBERON, NIFTTL
from pyontutils.core import Class, Source, Ont, LabelsBase, Collector, annotations, restriction, build
from pyontutils.core import makePrefixes, makeGraph, interlex_namespace, OntMeta, nsExact
from pyontutils.utils import TODAY, async_getter, rowParse, getSourceLine, subclasses
from pyontutils.utils import TermColors as tc #TERMCOLORFUNC
from pyontutils.ttlser import natsort
from pyontutils.scigraph import Vocabulary
from pyontutils.ilx_utils import ILXREPLACE
from pyontutils.hierarchies import creatTree, Query
from pyontutils.process_fixed import ProcessPoolExecutor
from IPython import embed

WRITELOC = '/tmp/parc/'
GENERATED = 'http://ontology.neuinfo.org/NIF/ttl/generated/'
PARC = GENERATED + 'parcellation/'
NOTICE = '**FIXME**'


sgv = Vocabulary(cache=True)

PScheme = namedtuple('PScheme',
                     ['curie',
                      'name',
                      'species',
                      'devstage'])
PScheme('ilxtr:something',
        'some parcellation scheme concept',
        'NCBITaxon:1234',
        'adult')

PSArtifact = namedtuple('PSArtifact',
                        ['curie',
                         'name',
                         'version',
                         'date',
                         'link',
                         'citation',
                         'synonyms',
                         'acronyms'])
PSArtifact('SCR:something',
           'name name',
           'v1',
           '01/01/01',
           'http://wut.wut',
           'scholarly things',
           tuple(),
           tuple())

# annotationProperties
#PARCLAB = 'ilxtr:parcellationLabel'
PARCLAB = 'skos:prefLabel'
ACRONYM = 'NIFRID:acronym'
SYNONYM = 'NIFRID:synonym'

# objectProperties
UNTAXON = 'ilxtr:ancestralInTaxon'
EXTAXON = 'ilxtr:hasInstanceInTaxon'  # FIXME instances?
EXSPECIES = 'ilxtr:hasInstanceInSpecies'
DEFTAXON = 'ilxtr:definedForTaxon'
DEFSPECIES = 'ilxtr:definedForSpecies'
DEVSTAGE = 'ilxtr:definedForDevelopmentalStage'
PARTOF = 'ilxtr:partOf'
HASPART = 'ilxtr:hasPart'
DELINEATEDBY = 'ilxtr:delineatedBy'

# classes
ADULT = 'BIRNLEX:681'
atname = 'Parcellation scheme artifact'
ATLAS_SUPER = ILXREPLACE(atname) # 'NIFRES:nlx_res_20090402'  # alternatives?
psname = 'Brain parcellation scheme concept'
PARC_SUPER = ILXREPLACE(psname)

def check_hierarchy(graph, root, edge, label_edge=None):
    a, b = creatTree(*Query(root, edge, 'INCOMING', 10), json=graph.make_scigraph_json(edge, label_edge))
    print(a)

def add_ops(graph):
    graph.add_op(EXSPECIES)
    graph.add_op(DEFSPECIES)
    graph.add_op(DEVSTAGE)

def make_scheme(graph, scheme, atlas_id=None, parent=PARC_SUPER):
    graph.add_class(scheme.curie, parent, label=scheme.name)
    graph.add_restriction(scheme.curie, DEFSPECIES, scheme.species)
    graph.add_restriction(scheme.curie, DEVSTAGE, scheme.devstage)
    if atlas_id:
        graph.add_trip(scheme.curie, rdfs.isDefinedBy, atlas_id)

def make_atlas(atlas, parent=ATLAS_SUPER):
    out = [
        (atlas.curie, rdf.type, owl.Class),
        (atlas.curie, rdfs.label, atlas.name),
        (atlas.curie, rdfs.subClassOf, parent),
        (atlas.curie, 'ilxtr:atlasVersion', atlas.version),  # FIXME
        (atlas.curie, 'ilxtr:atlasDate', atlas.date),  # FIXME
        (atlas.curie, 'NIFRID:externalSourceURI', atlas.link),  # FXIME probably needs to be optional...
        (atlas.curie, 'NIFRID:definingCitation', atlas.citation),
    ] + \
    [(atlas.curie, SYNONYM, syn) for syn in atlas.synonyms] + \
    [(atlas.curie, ACRONYM, ac) for ac in atlas.acronyms]

    return out

def add_triples(graph, struct, struct_to_triples, parent=None):
    if not parent:
        [graph.add_trip(*triple) for triple in struct_to_triples(struct)]
    else:
        [graph.add_trip(*triple) for triple in struct_to_triples(struct, parent)]

def parcellation_schemes(ontids_atlases):
    ont = OntMeta(GENERATED,
                  'parcellation',
                  'NIF collected parcellation schemes ontology',
                  'NIF Parcellations',
                  'Brain parcellation schemes as represented by root concepts.',
                  TODAY)
    ontid = ont.path + ont.filename + '.ttl'
    PREFIXES = makePrefixes('ilxtr', 'owl', 'skos', 'NIFRID', 'ILXREPLACE')
    graph = makeGraph(ont.filename, PREFIXES, writeloc=WRITELOC)
    graph.add_ont(ontid, *ont[2:])

    for import_id, atlas in sorted(ontids_atlases):
        graph.add_trip(ontid, owl.imports, import_id)
        add_triples(graph, atlas, make_atlas)

    graph.add_class(ATLAS_SUPER, label=atname)

    graph.add_class(PARC_SUPER, label=psname)
    graph.write()


class genericPScheme:
    ont = OntMeta
    concept = PScheme
    atlas = PSArtifact
    PREFIXES = makePrefixes('ilxtr', 'owl', 'skos', 'BIRNLEX', 'NCBITaxon', 'ILXREPLACE')

    def __new__(cls, validate=False):
        error = 'Expected %s got %s'
        if type(cls.ont) != OntMeta:
            raise TypeError(error % (OntMeta, type(cls.ont)))
        elif type(cls.concept) != PScheme:
            raise TypeError(error % (PScheme, type(cls.concept)))
        elif type(cls.atlas) != PSArtifact:
            raise TypeError(error % (PSArtifact, type(cls.atlas)))

        ontid = cls.ont.path + cls.ont.filename + '.ttl'
        PREFIXES = {k:v for k, v in cls.PREFIXES.items()}
        PREFIXES.update(genericPScheme.PREFIXES)
        #if '' in cls.PREFIXES:  # NOT ALLOWED!
            #if PREFIXES[''] is None:
                #PREFIXES[''] = ontid + '/'
        graph = makeGraph(cls.ont.filename, PREFIXES, writeloc=WRITELOC)
        graph.add_ont(ontid, *cls.ont[2:])
        make_scheme(graph, cls.concept, cls.atlas.curie)
        data = cls.datagetter()
        cls.datamunge(data)
        cls.dataproc(graph, data)
        add_ops(graph)
        graph.write()
        if validate or getattr(cls, 'VALIDATE', False):
            cls.validate(graph)
        return ontid, cls.atlas

    @classmethod
    def datagetter(cls):
        """ example datagetter function, make any local modifications here """
        with open('myfile', 'rt') as f:
            rows = [r for r in csv.reader(f)]
        dothing = lambda _: [i for i, v in enumerate(_)]
        rows = [dothing(_) for _ in rows]
        raise NotImplementedError('You need to implement this yourlself!')
        return rows

    @classmethod
    def datamunge(cls, data):
        """ in place modifier of data """
        pass

    @classmethod
    def dataproc(cls, graph, data):
        """ example datagetter function, make any local modifications here """
        for thing in data:
            graph.add_trip(*thing)
        raise NotImplementedError('You need to implement this yourlself!')

    @classmethod
    def validate(cls, graph):
        """ Put any post validation here. """
        raise NotImplementedError('You need to implement this yourlself!')


class CoCoMac(genericPScheme):
    ont = OntMeta(PARC,
                  'cocomacslim',
                  'CoCoMac terminology',
                  'CoCoMac',
                  ('This file is automatically generated from the CoCoMac '
                   'database on the terms from BrainMaps_BrainSiteAcronyms.' + NOTICE),
                  TODAY)
    concept = PScheme(ILXREPLACE(ont.name),
                       'CoCoMac terminology parcellation concept',
                       'NCBITaxon:9544',
                       'ilxtr:various')
    atlas = PSArtifact(ILXREPLACE(ont.name + 'atlas'),
                        'CoCoMac terminology',
                        None, #'no version info',
                        None, #'no date',
                        'http://cocomac.g-node.org',
                        'scholarly things',
                        tuple(),
                        tuple())

    PREFIXES = makePrefixes('NIFRID')
    PREFIXES['cocomac'] = 'http://cocomac.g-node.org/services/custom_sql_query.php?sql=SELECT%20*%20from%20BrainMaps_BrainSiteAcronyms%20where%20ID='  # looking for better options

    @classmethod
    def datagetter(cls):
        url = 'http://cocomac.g-node.org/services/custom_sql_query.php?sql=SELECT * from BrainMaps_BrainSiteAcronyms;&format=json'
        table = requests.get(url).json()
        fields = table['fields']
        data = [fields] + list(table['data'].values())
        return data

    @classmethod
    def dataproc(cls, graph, data):

        class cocomac(rowParse):
            def ID(self, value):
                self.identifier = 'cocomac:' + value  # safe because reset every row (ish)
                graph.add_class(self.identifier, cls.concept.curie)

            def Key(self, value):
                pass

            def Summary(self, value):
                pass

            def Acronym(self, value):
                graph.add_trip(self.identifier, ACRONYM, value)

            def FullName(self, value):
                graph.add_trip(self.identifier, rdfs.label, '(%s) ' % cls.ont.shortname + value)
                graph.add_trip(self.identifier, PARCLAB, value)

            def LegacyID(self, value):
                graph.add_trip(self.identifier, ACRONYM, value)

            def BrainInfoID(self, value):
                pass

        cocomac(data)


def swanson():
    """ not really a parcellation scheme """
    source = 'resources/swanson_aligned.txt'
    ONT_PATH = GENERATED
    filename = 'swanson_hierarchies'
    ontid = ONT_PATH + filename + '.ttl'
    PREFIXES = makePrefixes('ilxtr', 'owl', 'skos', 'NIFRID', '')
    PREFIXES.update({
        #'':ontid + '/',  # looking for better options
        'SWAN':interlex_namespace('swanson/uris/neuroanatomical-terminology/terms/'),
        'SWAA':interlex_namespace('swanson/uris/neuroanatomical-terminology/appendix/'),
    })
    new_graph = makeGraph(filename, PREFIXES, writeloc=WRITELOC)
    new_graph.add_ont(ontid,
                      'Swanson brain partomies',
                      'Swanson 2014 Partonomies',
                      'This file is automatically generated from ' + source + '.' + NOTICE,
                      TODAY)

    # FIXME citations should really go on the ... anatomy? scheme artifact
    definingCitation = 'Swanson, Larry W. Neuroanatomical Terminology: a lexicon of classical origins and historical foundations. Oxford University Press, USA, 2014.'
    definingCitationID = 'ISBN:9780195340624'
    new_graph.add_trip(ontid, 'NIFRID:definingCitation', definingCitation)
    new_graph.add_trip(ontid, 'NIFRID:definingCitationID', definingCitationID)

    with open(source, 'rt') as f:
        lines = [l.strip() for l in f.readlines()]

    # join header on page 794
    lines[635] += ' ' + lines.pop(636)
    #fix for capitalization since this header is reused
    fixed = ' or '.join([' ('.join([n.capitalize() for n in _.split(' (')]) for _ in lines[635].lower().split(' or ')]).replace('human','HUMAN')
    lines[635] = fixed

    data = []
    for l in lines:
        if not l.startswith('#'):
            level = l.count('.'*5)
            l = l.strip('.')
            if ' (' in l:
                if ') or' in l:
                    n1, l = l.split(') or')
                    area_name, citationP =  n1.strip().split(' (')
                    citation = citationP.rstrip(')')
                    d = (level, area_name, citation, 'NEXT SYN')
                    data.append(d)
                    #print(tc.red(tc.bold(repr(d))))

                area_name, citationP =  l.strip().split(' (')
                citation = citationP.rstrip(')')
            else:
                area_name = l
                citation = None

            d = (level, area_name, citation, None)
            #print(d)
            data.append(d)
    results = async_getter(sgv.findByTerm, [(d[1],) for d in data])
    #results = [None] * len(data)
    curies = [[r['curie'] for r in _ if 'UBERON' in r['curie']] if _ else [] for _ in results]
    output = [_[0] if _ else None for _ in curies]

    header = ['Depth', 'Name', 'Citation', 'NextSyn', 'Uberon']
    zoop = [header] + [r for r in zip(*zip(*data), output)] + \
            [(0, 'Appendix END None', None, None, None)]  # needed to add last appendix

    class SP(rowParse):
        def __init__(self):
            self.nodes = defaultdict(dict)
            self._appendix = 0
            self.appendicies = {}
            self._last_at_level = {}
            self.names = defaultdict(set)
            self.children = defaultdict(set)
            self.parents = defaultdict(set)
            self.next_syn = False
            super().__init__(zoop)

        def Depth(self, value):
            if self.next_syn:
                self.synonym = self.next_syn
            else:
                self.synonym = False
            self.depth = value

        def Name(self, value):
            self.name = value

        def Citation(self, value):
            self.citation = value

        def NextSyn(self, value):
            if value:
                self.next_syn = self._rowind
            else:
                self.next_syn = False

        def Uberon(self, value):
            self.uberon = value

        def _row_post(self):
            # check if we are in the next appendix
            # may want to xref ids between appendicies as well...
            if self.depth == 0:
                if self.name.startswith('Appendix'):
                    if self._appendix:
                        self.appendicies[self._appendix]['children'] = dict(self.children)
                        self.appendicies[self._appendix]['parents'] = dict(self.parents)
                        self._last_at_level = {}
                        self.children = defaultdict(set)
                        self.parents = defaultdict(set)
                    _, num, apname = self.name.split(' ', 2)
                    if num == 'END':
                        return
                    self._appendix = int(num)
                    self.appendicies[self._appendix] = {
                        'name':apname.capitalize(),
                        'type':self.citation.capitalize() if self.citation else None}
                    return
                else:
                    if ' [' in self.name:
                        name, taxonB = self.name.split(' [')
                        self.name = name
                        self.appendicies[self._appendix]['taxon'] = taxonB.rstrip(']').capitalize()
                    else:  # top level is animalia
                        self.appendicies[self._appendix]['taxon'] = 'ANIMALIA'.capitalize()

                    self.name = self.name.capitalize()
                    self.citation = self.citation.capitalize()
            # nodes
            if self.synonym:
                self.nodes[self.synonym]['synonym'] = self.name
                self.nodes[self.synonym]['syn-cite'] = self.citation
                self.nodes[self.synonym]['syn-uberon'] = self.uberon
                return
            else:
                if self.citation:  # Transverse Longitudinal etc all @ lvl4
                    self.names[self.name + ' ' + self.citation].add(self._rowind)
                else:
                    self.name += str(self._appendix) + self.nodes[self._last_at_level[self.depth - 1]]['label']
                    #print(level, self.name)
                    # can't return here because they are their own level
                # replace with actually doing something...
                self.nodes[self._rowind]['label'] = self.name
                self.nodes[self._rowind]['citation'] = self.citation
                self.nodes[self._rowind]['uberon'] = self.uberon
            # edges
            self._last_at_level[self.depth] = self._rowind
            # TODO will need something to deal with the Lateral/
            if self.depth > 0:
                try:
                    parent = self._last_at_level[self.depth - 1]
                except:
                    embed()
                self.children[parent].add(self._rowind)
                self.parents[self._rowind].add(parent)

        def _end(self):
            replace = {}
            for asdf in [sorted(n) for k,n in self.names.items() if len(n) > 1]:
                replace_with, to_replace = asdf[0], asdf[1:]
                for r in to_replace:
                    replace[r] = replace_with

            for r, rw in replace.items():
                #print(self.nodes[rw])
                o = self.nodes.pop(r)
                #print(o)

            for vals in self.appendicies.values():
                children = vals['children']
                parents = vals['parents']
                # need reversed so children are corrected before swap
                for r, rw in reversed(sorted(replace.items())):
                    if r in parents:
                        child = r
                        new_child = rw
                        parent = parents.pop(child)
                        parents[new_child] = parent
                        parent = list(parent)[0]
                        children[parent].remove(child)
                        children[parent].add(new_child)
                    if r in children:
                        parent = r
                        new_parent = rw
                        childs = children.pop(parent)
                        children[new_parent] = childs
                        for child in childs:
                            parents[child] = {new_parent}

            self.nodes = dict(self.nodes)

    sp = SP()
    tp = [_ for _ in sorted(['{: <50}'.format(n['label']) + n['uberon'] if n['uberon'] else n['label'] for n in sp.nodes.values()])]
    #print('\n'.join(tp))
    #print(sp.appendicies[1].keys())
    #print(sp.nodes[1].keys())
    nbase = PREFIXES['SWAN'] + '%s'
    json_ = {'nodes':[],'edges':[]}
    parent = ilxtr.swansonBrainRegionConcept
    for node, anns in sp.nodes.items():
        nid = nbase % node
        new_graph.add_class(nid, parent, label=anns['label'])
        new_graph.add_trip(nid, 'NIFRID:definingCitation', anns['citation'])
        json_['nodes'].append({'lbl':anns['label'],'id':'SWA:' + str(node)})
        #if anns['uberon']:
            #new_graph.add_trip(nid, owl.equivalentClass, anns['uberon'])  # issues arrise here...

    for appendix, data in sp.appendicies.items():
        aid = PREFIXES['SWAA'] + str(appendix)
        new_graph.add_class(aid, label=data['name'].capitalize())
        new_graph.add_trip(aid, 'ilxtr:hasTaxonRank', data['taxon'])  # FIXME appendix is the data artifact...
        children = data['children']
        ahp = HASPART + str(appendix)
        apo = PARTOF + str(appendix)
        new_graph.add_op(ahp, transitive=True)
        new_graph.add_op(apo, inverse=ahp, transitive=True)
        for parent, childs in children.items():  # FIXME does this give complete coverage?
            pid = nbase % parent
            for child in childs:
                cid = nbase % child
                new_graph.add_restriction(pid, ahp, cid)  # note hierarhcy inverts direction
                new_graph.add_restriction(cid, apo, pid)
                json_['edges'].append({'sub':'SWA:' + str(child),'pred':apo,'obj':'SWA:' + str(parent)})

    new_graph.write()
    if False:
        Query = namedtuple('Query', ['root','relationshipType','direction','depth'])
        mapping = (1, 1, 1, 1, 30, 83, 69, 70, 74, 1)  # should generate?
        for i, n in enumerate(mapping):
            a, b = creatTree(*Query('SWA:' + str(n), 'ilxtr:swansonPartOf' + str(i + 1), 'INCOMING', 10), json=json_)
            print(a)
    return ontid, None

    #embed()

def main():
    if not os.path.exists(WRITELOC):
        os.mkdir(WRITELOC)

    with ProcessPoolExecutor(4) as ppe:
        funs = [CoCoMac, #cocomac_make,
                swanson]
        futures = [ppe.submit(f) for f in funs]
        print('futures compiled')
        fs = [f.result() for f in futures]
        fs = fs[0] + fs[1:]
        parcellation_schemes(fs[:-1])

    # make a protege catalog file to simplify life
    uriline = '  <uri id="User Entered Import Resolution" name="{ontid}" uri="{filename}"/>'
    xmllines = ['<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
    '<catalog prefer="public" xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">',] + \
    [uriline.format(ontid=f, filename=f.rsplit('/',1)[-1]) for f,_ in fs] + \
    ['  <group id="Folder Repository, directory=, recursive=true, Auto-Update=true, version=2" prefer="public" xml:base=""/>',
    '</catalog>',]
    xml = '\n'.join(xmllines)
    with open('/tmp/catalog-v001.xml','wt') as f:
        f.write(xml)


#
# New impl
# helpers

class DupeRecord:
    def __init__(self, alt_abbrevs=tuple(), structures=tuple(), figures=None, artiris=tuple()):
        self.alt_abbrevs = alt_abbrevs
        self.structures = structures
        self.artiris = artiris


# classes


class Artifact(Class):
    """ Parcellation artifacts are the defining information sources for
        parcellation labels and/or atlases in which those labels are used.
        They may include semantic and/or geometric information. """

    iri = ilxtr.parcellationArtifact
    class_label = 'Parcellation Artifact'
    _kwargs = dict(iri=None,
                   rdfs_label=None,
                   label=None,
                   synonyms=tuple(),
                   abbrevs=tuple(),
                   definition=None,
                   shortname=None,
                   date=None,
                   copyrighted=None,
                   version=None,
                   species=None,
                   devstage=None,
                   source=None,
                   citation=None,
                   docUri=None,
                   comment=None,
                   definingCitations=tuple(),
                   hadDerivation=tuple(),
                  )
    propertyMapping = dict(
        version=ilxtr.artifactVersion,  # FIXME
        date=dc.date,
        sourceUri=ilxtr.sourceUri,  # FIXME
        copyrighted=dcterms.dateCopyrighted,
        source=dc.source,  # use for links to
        hadDerivation=prov.hadDerivation,
        # ilxr.atlasDate
        # ilxr.atlasVersion
    )

    propertyMapping = {**Class.propertyMapping, **propertyMapping}  # FIXME make this implicit


class Terminology(Artifact):
    """ A source for parcellation information that applies to one
        or more spatial sources, but does not itself contain the
        spatial definitions. For example Allen MBA. """

    iri = ilxtr.parcellationTerminology
    class_label = 'Parcellation terminology'
    #class_definition = ('An artifact that only contains semantic information, '
                        #'not geometric information, about a parcellation.')


class CoordinateSystem(Artifact):
    """ An artifact that defines the geometric coordinates used by
        one or more parcellations. """

    iri = ilxtr.parcellationCoordinateSystem
    class_label = 'Parcellation coordinate system'


class Delineation(Artifact):
    """ An artifact that defines the spatial boundaries or landmarks for a parcellation.
        Delineations must be explicitly spatial and are distinct from delineation criteria
        which may provide a non-spatial definition for regions. """

    iri = ilxtr.parcellationDelineation
    class_label = 'Parcellation delineation'
    # TODO registrationCriteria => processive, usually matching delineationCriteria where practical
    # TODO delineationCriteria => definitional


class Atlas(Artifact):
    """ An artifact that contains information about the terminology,
        delineation, and coordinate system for a parcellation. These
        are usually physical atlases where it is not possibly to uniquely
        identify any of the component parts, but only all the parts taken
        together (e.g. via ISBN). """

    iri = ilxtr.parcellationAtlas
    class_label = 'Parcellation atlas'
    # hasPart Delineation, hasPart CoordinateSystem, hasPart Terminology
    # alternately hasPart DelineationCriteria and/or RegistrationCriteria
    # TODO links to identifying atlas pictures


class LabelRoot(Class):
    """ Parcellation labels are strings characthers sometimes associated
        with a unique identifier, such as an index number or an iri. """
    """ Base class for labels from a common source that should live in one file """
    # use this to define the common superclass for a set of labels
    iri = ilxtr.parcellationLabel
    class_label = 'Parcellation Label'
    _kwargs = dict(iri=None,
                   label=None,
                   comment=None,
                   shortname=None,  # used to construct the rdfs:label
                   definingArtifacts=tuple(),  # leave blank if defined for the parent class
                   definingArtifactsS=tuple(),
                  )

    def __init__(self, *args, **kwargs):
        for it_name in ('definingArtifacts', 'definingArtifactsS'):  # TODO abstract to type
            if it_name in kwargs: 
                kwargs[it_name] = tuple(set(kwargs[it_name]))
        super().__init__(*args, **kwargs)


class Label(Class):
    # allen calls these Structures (which is too narrow because of ventricles etc)
    _kwargs = dict(labelRoot=None,
                   label=None,  # this will become the skos:prefLabel
                   altLabel=None,
                   synonyms=tuple(),
                   abbrevs=tuple(),
                   definingArtifacts=tuple(),  # leave blank if defined for the parent class, needed for paxinos
                   definingCitations=tuple(),
                   iri=None,  # use when a class already exists and we need to know its identifier
                  )
    def __init__(self,
                 usedInArtifacts=tuple(),  # leave blank if 1:1 map between labelRoot and use artifacts NOTE even MBA requires validate on this
                 **kwargs
                ):
        super().__init__(**kwargs)
        self.usedInArtifacts = list(usedInArtifacts)

    def usedInArtifact(self, artifact):
        self.usedInArtifacts.append(artifact)

    @property
    def rdfs_label(self):
        if hasattr(self, 'label'):
            if hasattr(self, 'labelRoot'):
                return self.label + ' (' + self.labelRoot.shortname + ')'
            return self.label + ' (WARNING YOUR LABELS HAVE NO ROOT!)'
        else:
            return 'class not initialized but here __init__ you can have this helpful string :)'

    @property
    def rdfs_subClassOf(self):
        return self.labelRoot.iri


class RegionRoot(Class):
    """ Parcellation regions are 'anatomical entities' that correspond to some
        part of a real biological system and are equivalent to an intersection
        between a parcellation label and a specific version of an atlas that
        defines or uses that label and that provides a definitive
        (0, 1, or probabilistic) way to determine whether a particular sample
        corresponds to any given region.
        """
    """
    Centroid regions (anatomical entities)

    species specific labels
    species generic labels (no underlying species specific mapping)

    Symbols             ->
    semantic labels     -> semantic anatomical region                   -> point (aka unbounded connected spatial volume defined by some 'centroid' or canonical member)
    parcellation labels -> probabalistic anatomical parcellation region -> probablistically bounded connected spatial volume
                        -> anatomical parcellation region               -> bounded connected spatial volume (as long as the 3d volume is topoligically equivalent to a sphere, unconnected planes of section are fine)
    """
    iri = ilxtr.parcellationRegion
    class_label = 'Parcellation Region'
    _kwargs = dict(iri=None,
                   atlas=None,  # : Atlas
                   labelRoot=None)  # : LabelRoot


class Region(Class):
    iri = ilxtr.parcellationRegion
    def __init__(self,
                 regionRoot,
                 label):
        self.atlas = regionRoot.atlas
        self.label = label.label

#
# ontologies


class Artifacts(Collector):
    collects = Artifact
    class PaxMouseAt(Atlas):
        """ Any atlas artifact with Paxinos as an author for the adult rat. """
        iri = ilx['paxinos/uris/mouse']  # ilxtr.paxinosMouseAtlas
        class_label = 'Paxinos Mouse Atlas'

    _PaxMouseShared = dict(species=NCBITaxon['10090'],
                           devstage=UBERON['0000113'],  # TODO this is 'Mature' which may not match... RnorDv:0000015 >10 weeks...
                           citation=('INTERNAL SCREAMING'),
                          )

    PaxMouse2 = PaxMouseAt(iri=paxmusver['2'],  # ilxtr.paxm2,
                           label='The Mouse Brain in Stereotaxic Coordinates 2nd Edition',
                           synonyms=('Paxinos Mouse 2nd',),
                           abbrevs=tuple(),
                           shortname='PAXMOUSE2',  # TODO upper for atlas lower for label?
                           copyrighted='2001',
                           version='2nd Edition',  # FIXME ??? delux edition??? what is this
                           **_PaxMouseShared)

    PaxMouse3 = PaxMouseAt(iri=paxmusver['3'],  # ilxtr.paxm3,
                           label='The Mouse Brain in Stereotaxic Coordinates 3rd Edition',
                           synonyms=('Paxinos Mouse 3rd',),
                           abbrevs=tuple(),
                           shortname='PAXMOUSE3',  # TODO upper for atlas lower for label?
                           copyrighted='2008',
                           version='3rd Edition',
                           **_PaxMouseShared)

    PaxMouse4 = PaxMouseAt(iri=paxmusver['4'],  # ilxtr.paxm4,
                           label='The Mouse Brain in Stereotaxic Coordinates 4th Edition',
                           synonyms=('Paxinos Mouse 4th',),
                           abbrevs=tuple(),
                           shortname='PAXMOUSE4',  # TODO upper for atlas lower for label?
                           copyrighted='2012',
                           version='4th Edition',
                           **_PaxMouseShared)

    class PaxRatAt(Atlas):
        """ Any atlas artifact with Paxinos as an author for the adult rat. """
        iri = ilx['paxinos/uris/rat']  # ilxtr.paxinosRatAtlas
        class_label = 'Paxinos Rat Atlas'

    _PaxRatShared = dict(species=NCBITaxon['10116'],
                         devstage=UBERON['0000113'],  # TODO this is 'Mature' which may not match... RnorDv:0000015 >10 weeks...
                         citation=('Paxinos, George, Charles RR Watson, and Piers C. Emson. '
                                   '"AChE-stained horizontal sections of the rat brain '
                                   'in stereotaxic coordinates." Journal of neuroscience '
                                   'methods 3, no. 2 (1980): 129-149.'),
                       )

    PaxRat4 = PaxRatAt(iri=ilx['paxinos/uris/rat/versions/4'],  # ilxtr.paxr4,
                       label='The Rat Brain in Stereotaxic Coordinates 4th Edition',
                       synonyms=('Paxinos Rat 4th',),
                       abbrevs=tuple(),
                       shortname='PAXRAT4',  # TODO upper for atlas lower for label?
                       copyrighted='1998',
                       version='4th Edition',
                       **_PaxRatShared
                      )

    PaxRat6 = PaxRatAt(iri=ilx['paxinos/uris/rat/versions/6'],  # ilxtr.paxr6,
                       label='The Rat Brain in Stereotaxic Coordinates 6th Edition',
                       synonyms=('Paxinos Rat 6th',),
                       abbrevs=tuple(),
                       shortname='PAXRAT6',  # TODO upper for atlas lower for label?
                       copyrighted='2007',
                       version='6th Edition',
                       **_PaxRatShared
                      )

    PaxRat7 = PaxRatAt(iri=ilx['paxinos/uris/rat/versions/7'],  # ilxtr.paxr7,
                       label='The Rat Brain in Stereotaxic Coordinates 7th Edition',
                       synonyms=('Paxinos Rat 7th',
                                 'Paxinso and Watson\'s The Rat Brain in Stereotaxic Coordinates 7th Edition',  # branding >_<
                                ),
                       abbrevs=tuple(),
                       shortname='PAXRAT7',  # TODO upper for atlas lower for label?
                       copyrighted='2014',
                       version='7th Edition',
                       **_PaxRatShared
                      )

    HCPMMP = Terminology(iri=ilx['hcp/uris/mmp/versions/1.0'],  # ilxtr.hcpmmpv1,
                         rdfs_label='Human Connectome Project Multi-Modal human cortical parcellation',
                         shortname='HCPMMP',
                         date='2016-07-20',
                         version='1.0',
                         synonyms=('Human Connectome Project Multi-Modal Parcellation',
                                   'HCP Multi-Modal Parcellation',
                                   'Human Connectome Project Multi-Modal Parcellation version 1.0'),
                         abbrevs=('HCP_MMP', 'HCP-MMP1.0', 'HCP MMP 1.0'),
                         citation='https://doi.org/10.1038/nature18933',
                         species=NCBITaxon['9606'],
                         devstage=UBERON['0000113'],
                        )

class parcArts(Ont):
    """ Ontology file for artifacts that define labels or
        geometry for parcellation schemes. """

    # setup

    path = 'ttl/generated/'
    filename = 'parcellation-artifacts'
    name = 'Parcellation Artifacts'
    #shortname = 'parcarts'
    prefixes = {**makePrefixes('NCBITaxon', 'UBERON', 'skos'), **Ont.prefixes,
                'FSLATS':str(FSLATS),
                'paxmusver':str(paxmusver),
                'paxratver':str(paxratver),
    }

    def __call__(self):
        return super().__call__()

    @property
    def _artifacts(self):
        for collector in subclasses(Collector):
            if collector.__module__ != 'pyontutils.parcellation':  # just run __main__
                yield from collector.arts()

    def _triples(self):
        yield from Artifact.class_triples()
        for art_type in subclasses(Artifact):  # this is ok because all subclasses are in this file...
            # do not comment this out it is what makes the
            # upper classes in the artifacts hierarchy
            yield from art_type.class_triples()
        for artifact in self._artifacts:
            yield from artifact


class parcCore(Ont):
    """ Core OWL2 entities needed for parcellations """

    # setup

    path = 'ttl/generated/'
    filename = 'parcellation-core'
    name = 'Parcellation Core'
    #shortname = 'parcore'  # huehuehue
    prefixes = {**makePrefixes('skos'), **Ont.prefixes}
    imports = NIFTTL['nif_backend.ttl'], parcArts

    # stuff

    parents = LabelRoot, RegionRoot

    def _triples(self):
        for parent in self.parents:
            yield from parent.class_triples()


class RegionsBase(Ont):
    """ An ontology file containing parcellation regions from the
        intersection of an atlas artifact and a set of labels. """
    # TODO find a way to allow these to serialize into one file
    __pythonOnly = True  # FIXME for now perevent export
    imports = parcCore,
    atlas = None
    labelRoot = None
    def __init__(self):
        self.regionRoot = RegionRoot(atlas=self.atlas,
                                     labelRoot=self.labelRoot)


class parcBridge(Ont):
    """ Main bridge for importing the various files that
        make up the parcellation ontology. """

    # setup

    path = 'ttl/bridge/'
    filename = 'parcellation-bridge'
    name = 'Parcellation Bridge'
    imports = ((g[subclass.__name__]
                if subclass.__name__ in g and subclass.__module__ == 'pyontutils.parcellation'  # parcellation is insurance for name reuse
                else subclass)
               for g in (globals(),)
               for subclass in subclasses(LabelsBase)  # XXX wow, well apparently __main__.Class != module.Class
               if not hasattr(subclass, f'_{subclass.__name__}__pythonOnly'))

    @property
    def __imports(self):
        for subclass in subclasses(LabelsBase):
            if not hasattr(subclass, f'_{subclass.__name__}__pythonOnly'):
                yield subclass()


#
# Sources (input files)

class LocalSource(Source):
    _data = tuple()

    def __new__(cls):
        line = getSourceLine(cls)
        cls.iri_head = URIRef(cls.iri_prefix_hd + Path(__file__).name)
        cls._this_file = Path(__file__).absolute()
        repobase = cls._this_file.parent.parent.as_posix()
        cls.repo = Repo(repobase)
        cls.prov()  # have to call prov here ourselves since Source only calls prov if _data is not defined
        if cls.artifact is None:  # for prov...
            class art:
                iri = cls.iri
                def addPair(self, *args, **kwargs):
                    pass

            cls.artifact = art()

        self = super().__new__(cls)
        return self

    @classmethod
    def prov(cls):
        from inspect import getsourcelines
        #source_lines = getSourceLine

        def get_commit_data(start, end):
            records = cls.repo.git.blame('--line-porcelain', f'-L {start},{end}', cls._this_file.as_posix()).split('\n')
            rl = 13
            linenos = [(hexsha, int(nowL), int(thenL)) for r in records[::rl]
                       for hexsha, nowL, thenL, *n in (r.split(' '),)]
            author_times = [int(epoch) for r in records[3::rl] for _, epoch in (r.split(' '),)]
            lines = [r.strip('\t') for r in records[12::rl]]
            index, time = max(enumerate(author_times), key=lambda iv: iv[1])
            commit, then, now = linenos[index]
            # there are some hefty assumptions that go into this
            # that other lines have not been deleted from or added to the code block
            # between commits, or essentially that the code in the block is the
            # same length and has only been shifted by the distance defined by the
            # single commit that that has the maximum timestamp, so beware that
            # this can and will break which is why I use start and end instead of
            # just start like I do with the rest of the lines where I know for sure.
            # This can probably be improved with pickaxe or similar.
            shift = then - now
            then_start = start + shift
            then_end = end + shift
            return commit, then_start, then_end

        source_lines, start = getsourcelines(cls)
        end = start + len(source_lines)
        most_recent_block_commit, then_start, then_end = get_commit_data(start, end)

        cls.iri = URIRef(cls.iri_prefix_wdf.format(file_commit=most_recent_block_commit)
                         + f'{cls._this_file.name}#L{then_start}-L{then_end}')


##
#  Instances
##

# Source instances  TODO put everything under one class as we do for Artifacts?

class resSource(Source):
    source = 'https://github.com/tgbugs/pyontutils.git'


class PaxSr_6(resSource):
    sourceFile = 'pyontutils/resources/paxinos09names.txt'
    artifact = Artifacts.PaxRat6

    @classmethod
    def loadData(cls):
        with open(cls.source, 'rt') as f:
            lines = [l.rsplit('#')[0].strip() for l in f.readlines() if not l.startswith('#')]
        return [l.rsplit(' ', 1) for l in lines]

    @classmethod
    def processData(cls):
        structRecs = []
        out = {}
        for structure, abrv in cls.raw:
            structRecs.append((abrv, structure))
            if abrv in out:
                out[abrv][0].append(structure)
            else:
                out[abrv] = ([structure], ())
        return structRecs, out

    @classmethod
    def validate(cls, structRecs, out):
        print(Counter(_[0] for _ in structRecs).most_common()[:5])
        print(Counter(_[1] for _ in structRecs).most_common()[:5])
        assert len(structRecs) == len([s for sl, _ in out.values() for s in sl]), 'There are non-unique abbreviations'
        errata = {}
        return out, errata


class PaxSrAr(resSource):
    artifact = None

    @classmethod
    def parseData(cls):
        a, b = cls.raw.split('List of Structures')
        if not a:
            los, loa = b.split('List of Abbreviations')
        else:
            los = b
            _, loa = a.split('List of Abbreviations')

        sr = []
        for l in los.split('\n'):
            if l and not l[0] == ';':
                if ';' in l:
                    l, *comment = l.split(';')
                    l = l.strip()
                    print(l, comment)

                #asdf = l.rsplit(' ', 1)
                #print(asdf)
                struct, abbrev = l.rsplit(' ', 1)
                sr.append((abbrev, struct))

        ar = []
        for l in loa.split('\n'):
            if l and not l[0] == ';':
                if ';' in l:
                    l, *comment = l.split(';')
                    l = l.strip()
                    print(l, comment)

                #asdf = l.rsplit(' ', 1)
                #print(asdf)
                abbrev, rest = l.split(' ', 1)
                parts = rest.split(' ')
                #print(parts)
                for i, pr in enumerate(parts[::-1]):
                    #print(i, pr)
                    z = pr[0].isdigit()
                    if not z or i > 0 and z and pr[-1] != ',':
                        break

                struct = ' '.join(parts[:-i])
                figs = tuple(tuple(int(_) for _ in p.split('-'))
                             if '-' in p
                             else (tuple(f'{nl[:-1]}{l}'
                                        for nl, *ls in p.split(',')
                                        for l in (nl[-1], *ls))
                                   if ',' in p or p[-1].isalpha()
                                   else int(p))
                             for p in (_.rstrip(',') for _ in parts[-i:]))
                figs = tuple(f for f in figs if f)  # zero marks abbrevs in index that are not in figures
                #print(struct)
                ar.append((abbrev, struct, figs))
        return sr, ar

    @classmethod
    def processData(cls):
        sr, ar = cls.parseData()
        out = {}
        achild = {}
        for a, s, f in ar:
            if ', layer 1' in s or s.endswith(' layer 1'):  # DTT1 ends in ' layer 1' without a comma
                achild[a[:-1]] = a
                continue  # remove the precomposed, we will deal with them systematically
            if a not in out:
                out[a] = ([s], f)
            else:
                if s not in out[a][0]:
                    print(f'Found new label from ar for {a}:\n{s}\n{out[a][0]}')
                    out[a][0].append(s)

        schild = {}
        for a, s in sr:
            if ', layer 1' in s or s.endswith(' layer 1'):
                schild[a[:-1]] = a
                continue # remove the precomposed, we will deal with them systematically
            if a not in out:
                out[a] = ([s], tuple())
            else:
                if s not in out[a][0]:
                    print(f'Found new label from sr for {a}:\n{s}\n{out[a][0]}')
                    out[a][0].append(s)
                    #raise TypeError(f'Mismatched labels on {a}: {s} {out[a][0]}')

        return sr, ar, out, achild, schild

    @classmethod
    def validate(cls, sr, ar, out, achild, schild):
        def missing(a, b):
            am = a - b
            bm = b - a
            return am, bm
        sabs = set(_[0] for _ in sr)
        aabs = set(_[0] for _ in ar)
        ssts = set(_[1] for _ in sr)
        asts = set(_[1] for _ in ar)
        ar2 = set(_[:2] for _ in ar)
        aam, sam = missing(aabs, sabs)
        asm, ssm = missing(asts, ssts)
        ar2m, sr2m = missing(ar2, set(sr))
        print('OK to skip')
        print(sorted(aam))
        print('Need to be created')
        print(sorted(sam))
        print()
        print(sorted(asm))
        print()
        print(sorted(ssm))
        print()
        #print(sorted(ar2m))
        #print()
        #print(sorted(sr2m))
        #print()

        assert all(s in achild for s in schild), f'somehow the kids dont match {achild} {schild}\n' + str(sorted(set(a) - set(s) | set(s) - set(a)
                                                                                               for a, s in ((tuple(sorted(achild.items())),
                                                                                                             tuple(sorted(schild.items()))),)))
        for k, (structs, figs) in out.items():
            for struct in structs:
                assert not re.match('\d+-\d+', struct) and not re.match('\d+$', struct), f'bad struct {struct} in {k}'

        errata = {'nodes with layers':achild}
        return out, errata


class PaxSrAr_4(PaxSrAr):
    sourceFile = 'pyontutils/resources/pax-4th-ed-indexes.txt'
    artifact = Artifacts.PaxRat4


class PaxSrAr_6(PaxSrAr):
    sourceFile = 'pyontutils/resources/pax-6th-ed-indexes.txt'
    artifact = Artifacts.PaxRat6


class PaxMSrAr_2(PaxSrAr):
    sourceFile = 'pyontutils/resources/paxm-2nd-ed-indexes.txt'
    artifact = Artifacts.PaxMouse2


class PaxMSrAr_3(PaxSrAr):
    sourceFile = 'pyontutils/resources/paxm-3rd-ed-indexes.txt'
    artifact = Artifacts.PaxMouse3


class PaxTree_6(Source):
    source = '~/ni/dev/nifstd/paxinos/tree.txt'
    artifact = Artifacts.PaxRat6

    @classmethod
    def loadData(cls):
        with open(os.path.expanduser(cls.source), 'rt') as f:
            return [l for l in f.read().split('\n') if l]

    @classmethod
    def processData(cls):
        out = {}
        recs = []
        parent_stack = [None]
        old_depth = 0
        layers = {}
        for l in cls.raw:
            depth, abbrev, _, name = l.split(' ', 3)
            depth = len(depth)

            if old_depth < depth:  # don't change
                parent = parent_stack[-1]
                parent_stack.append(abbrev)
                old_depth = depth
            elif old_depth == depth:
                if len(parent_stack) - 1 > depth:
                    parent_stack.pop()

                parent = parent_stack[-1]
                parent_stack.append(abbrev)
            elif old_depth > depth:  # bump back
                for _ in range(old_depth - depth + 1):
                    parent_stack.pop()

                parent = parent_stack[-1]
                parent_stack.append(abbrev)
                old_depth = depth

            struct = None if name == '-------' else name
            o = (depth, abbrev, struct, parent)
            if '-' in abbrev:
                # remove the precomposed, we will deal with them systematically
                maybe_parent, rest = abbrev.split('-', 1)
                if rest.isdigit() or rest == '1a' or rest == '1b':  # Pir1a Pir1b
                    if parent == 'Unknown':  # XXX special cases
                        if maybe_parent == 'Pi':  # i think this was probably caused by an ocr error from Pir3 -> Pi3
                            continue

                    assert maybe_parent == parent, f'you fall into a trap {maybe_parent} {parent}'
                    if parent not in layers:
                        layers[parent] = []

                    layers[parent].append((layer, o))  # FIXME where does layer come from here?
            elif struct is not None and ', layer 1' in struct:
                # remove the precomposed, we will deal with them systematically
                parent_, layer = abbrev[:-1], abbrev[-1]
                if parent_ == 'CxA' and parent == 'Amy':  # XXX special cases
                    parent = 'CxA'
                elif parent == 'Unknown':
                    if parent_ == 'LOT':
                        parent = 'LOT'
                    elif parent_ == 'Tu':
                        parent = 'Tu'

                assert parent_ == parent, f'wrong turn friend {parent_} {parent}'
                if parent not in layers:
                    layers[parent] = []

                layers[parent].append((layer, o))
            else:
                recs.append(o)
                out[abbrev] = ([struct], (), parent)

        errata = {'nodes with layers':layers}
        return recs, out, errata

    @classmethod
    def validate(cls, trecs, tr, errata):
        print(Counter(_[1] for _ in trecs).most_common()[:5])
        ('CxA1', 2), ('Tu1', 2), ('LOT1', 2), ('ECIC3', 2)
        assert len(tr) == len(trecs), 'Abbreviations in tr are not unique!'
        return tr, errata


class PaxFix4(LocalSource):
    artifact = Artifacts.PaxRat4
    _data = ({
        # 1-6b are listed in fig 19 of 4e, no 3/4, 5a, or 5b
        '1':(['layer 1 of cortex'], tuple()),
        '1a':(['layer 1a of cortex'], tuple()),
        '1b':(['layer 1b of cortex'], tuple()),
        '2':(['layer 2 of cortex'], tuple()),
        '3':(['layer 3 of cortex'], tuple()),
        '3/4':(['layer 3/4 of cortex'], tuple()),
        '4':(['layer 4 of cortex'], tuple()),
        '5':(['layer 5 of cortex'], tuple()),
        '5a':(['layer 5a of cortex'], tuple()),
        '5b':(['layer 5b of cortex'], tuple()),
        '6':(['layer 6 of cortex'], tuple()),
        '6a':(['layer 6a of cortex'], tuple()),
        '6b':(['layer 6b of cortex'], tuple()),
    }, {})


class PaxFix6(LocalSource):
    artifact = Artifacts.PaxRat6
    _data = ({
        '1':(['layer 1 of cortex'], tuple()),
        '1a':(['layer 1a of cortex'], (8,)),
        '1b':(['layer 1b of cortex'], (8,)),
        '2':(['layer 2 of cortex'], tuple()),
        '3':(['layer 3 of cortex'], tuple()),
        '3/4':(['layer 3/4 of cortex'], (94,)),
        '4':(['layer 4 of cortex'], tuple()),
        '5':(['layer 5 of cortex'], tuple()),
        '5a':(['layer 5a of cortex'], (52, 94)),
        '5b':(['layer 5b of cortex'], tuple()),
        '6':(['layer 6 of cortex'], tuple()),
        '6a':(['layer 6a of cortex'], tuple()),
        '6b':(['layer 6b of cortex'], tuple()),
    }, {})


class PaxFix(LocalSource):
    _data = ({
        '1':(['layer 1'], tuple()),
        '1a':(['layer 1a'], (8,)),
        '1b':(['layer 1b'], (8,)),
        '2':(['layer 2'], tuple()),
        '3':(['layer 3'], tuple()),
        '3/4':(['layer 3/4'], (94,)),
        '4':(['layer 4'], tuple()),
        '5':(['layer 5'], tuple()),
        '5a':(['layer 5a'], (52, 94)),
        '5b':(['layer 5b'], tuple()),
        '6':(['layer 6'], tuple()),
        '6a':(['layer 6a'], tuple()),
        '6b':(['layer 6b'], tuple()),
    }, {})


class PaxMFix(LocalSource):
    _data = ({}, {})


class HCPMMPSrc(resSource):
    sourceFile = 'pyontutils/resources/human_connectome_project_2016.csv'
    source_original = True
    artifact = Artifacts.HCPMMP

    @classmethod
    def loadData(cls):
        with open(cls.source, 'rt') as f:
            return [r for r in csv.reader(f)][1:]  # skip header

    @classmethod
    def processData(cls):
        return cls.raw,

    @classmethod
    def validate(cls, d):
        return d


#
# Ontology Instances

#
# labels


class HCPMMPLabels(LabelsBase):
    filename = 'hcpmmp'
    name = 'Human Connectome Project Multi-Modal human cortical parcellation'
    shortname = 'hcpmmp'
    imports = parcCore,
    prefixes = {**makePrefixes('NIFRID', 'ilxtr', 'prov'), 'HCPMMP':str(HCPMMP)}
    sources = HCPMMPSrc,
    namespace = HCPMMP
    root = LabelRoot(iri=nsExact(namespace),  # ilxtr.hcpmmproot,
                     label='HCPMMP label root',
                     shortname=shortname,
                     definingArtifacts=(s.artifact.iri for s in sources),
    )

    def _triples(self):
        for source in self.sources:
            for record in source:
                (Parcellation_Index, Area_Name, Area_Description,
                 Newly_Described, Results_Section, Other_Names,
                 Key_Studies) = [r.strip() for r in record]
                iri = HCPMMP[str(Parcellation_Index)]
                onames = [n.strip() for n in Other_Names.split(',') if n.strip()]
                syns = (n for n in onames if len(n) > 3)
                abvs = tuple(n for n in onames if len(n) <= 3)
                cites = tuple(s.strip() for s in Key_Studies.split(','))
                if Newly_Described in ('Yes*', 'Yes'):
                    cites = cites + ('Glasser and Van Essen 2016',)

                yield from Label(labelRoot=self.root,
                                 label=Area_Description,
                                 altLabel=Area_Name,
                                 synonyms=syns,
                                 abbrevs=abvs,
                                 #bibliographicCitation=  # XXX vs definingCitation
                                 definingCitations=cites,
                                 iri=iri)


class PaxLabels(LabelsBase):
    """ Base class for processing paxinos indexes. """
    __pythonOnly = True
    path = 'ttl/generated/parcellation/'
    imports = parcCore,
    _fixes = []
    _dupes = {}
    _merge = {}

    @property
    def fixes_abbrevs(self):
        fixes_abbrevs = set()
        for f in self._fixes:
            fixes_abbrevs.add(f[0])
        for dupe in self._dupes.values():
            fixes_abbrevs.add(dupe.alt_abbrevs[0])
        return fixes_abbrevs

    @property
    def fixes_prov(self):
        _fixes_prov = {}
        for f in self._fixes:
            for l in f[1][0]:
                _fixes_prov[l] = [Ont.wasGeneratedBy.format(line=getSourceLine(self.__class__))]  # FIXME per file
        return _fixes_prov

    @property
    def dupes_structs(self):
        ds = {'cerebellar lobules', 'cerebellar lobule'}
        for dupe in self._dupes.values():
            for struct in dupe.structures:
                ds.add(struct)
        return ds

    @property
    def fixes(self):
        _, _, collisions, _ = self.records()
        for a, (ss, f, arts) in self._fixes:
            if (a, ss[0]) in collisions:
                f.update(collisions[a, ss[1]])  # have to use 1 since we want "layer n" as the pref

            yield a, ([], ss, f, arts)

    def _prov(self, iri, abrv, struct, struct_prov, extras, alt_abbrevs, abbrev_prov):
        # TODO asssert that any triple for as ap at is actually in the graph...
        annotation_predicate = ilxtr.literalUsedBy
        definition_predicate = ilxtr.isDefinedBy  # TODO more like 'symbolization used in'
        for abbrev in [abrv] + alt_abbrevs:  # FIXME multiple annotations per triple...
            t = iri, Label.propertyMapping['abbrevs'], abbrev
            if t not in self._prov_dict:
                self._prov_dict[t] = []
            for s in [struct] + extras:
                if (abbrev, s) in abbrev_prov:
                    for artifact in abbrev_prov[abbrev, s]:
                        if 'github' in artifact:
                            continue
                        else:
                            predicate = annotation_predicate

                        self._prov_dict[t].append((predicate, artifact))

        if struct in struct_prov:
            t = iri, Label.propertyMapping['label'], struct
            if t not in self._prov_dict:
                self._prov_dict[t] = []
            for artifact in struct_prov[struct]:
                if 'github' in artifact:
                    predicate = definition_predicate
                else:
                    predicate = annotation_predicate

                self._prov_dict[t].append((predicate, artifact))

        for extra in extras:
            t = iri, Label.propertyMapping['synonyms'], extra
            if t not in self._prov_dict:
                self._prov_dict[t] = []
            for artifact in struct_prov[extra]:
                if 'github' in artifact:
                    predicate = definition_predicate
                else:
                    predicate = annotation_predicate

                self._prov_dict[t].append((predicate, artifact))

    def _makeIriLookup(self):
        # FIXME need to validate that we didn't write the graph first...
        g = Graph().parse(self._graph.filename, format='turtle')
        ids = [s for s in g.subjects(rdf.type, owl.Class) if self.namespace in s]
        index0 = Label.propertyMapping['abbrevs'],
        index1 = Label.propertyMapping['label'], Label.propertyMapping['synonyms']
        out = {}
        for i in ids:
            for p0 in index0:
                for o0 in g.objects(i, p0):
                    for p1 in index1:
                        for o1 in g.objects(i, p1):
                            key = o0, o1
                            value = i
                            if key in out:
                                raise KeyError(f'Key {key} already in output!')
                            out[key] = value
        return out

    def _triples(self):
        self._prov_dict = {}
        combined_record, struct_prov, _, abbrev_prov = self.records()
        for k, v in self.fixes_prov.items():
            if k in struct_prov:
                struct_prov[k].extend(v)
            else:
                struct_prov[k] = v
        for i, (abrv, (alts, (structure, *extras), figures, artifacts)) in enumerate(
                sorted(list(combined_record.items()) + list(self.fixes),
                       key=lambda d:natsort(d[1][1][0] if d[1][1][0] is not None else 'zzzzzzzzzzzzzzzzzzzz'))):  # sort by structure not abrev
            iri = self.namespace[str(i + 1)]  # TODO load from existing
            struct = structure if structure else 'zzzzzz'
            self._prov(iri, abrv, struct, struct_prov, extras, alts, abbrev_prov)
            yield from Label(labelRoot=self.root,
                             #ifail='i fail!',  # this indeed does fail
                             label=struct,
                             altLabel=None,
                             synonyms=extras,
                             abbrevs=(abrv, *alts),  # FIXME make sure to check that it is not a string
                             iri=iri,  # FIXME error reporint if you try to put in abrv is vbad
                             #extra_triples = str(processed_figures),  # TODO
                     )
            processed_figures = figures  # TODO these are handled in regions pass to PaxRegions
            if figures:
                for artifact in artifacts:
                    PaxRegion.addthing(iri, figures)  # artifact is baked into figures

        for t, pairs in self._prov_dict.items():
            if pairs:
                yield from annotations(pairs, *t)

    def validate(self):
        # check for duplicate labels
        labels = list(self.graph.objects(None, rdfs.label))
        assert len(labels) == len(set(labels)), f'There are classes with duplicate labels! {Counter(labels).most_common()[:5]}'

        # check for unexpected duplicate abbreviations
        abrevs = list(self.graph.objects(None, NIFRID.abbrev))
        # remove expected numeric/layer/lobule duplicates
        filt = [a for a in abrevs if not a.isdigit() and a.value not in ('6a', '6b')]
        assert len(filt) == len(set(filt)), f'DUPES! {Counter(filt).most_common()[:5]}'
        # check for abbreviations without corresponding structure ie 'zzzzzz'
        syns = list(self.graph.objects(None, NIFRID.synonym))
        for thing in labels + syns:
            trips = [(s, o) for s in self.graph.subjects(None, thing) for p, o in self.graph.predicate_objects(s)]
            assert 'zzzzzz' not in thing, f'{trips} has bad label/syn suggesting a problem with the source file'
        return self

    def records(self):
        combined_record = {}
        struct_prov = {}
        collisions = {}
        abbrev_prov = {}
        merge = {**self._merge, **{v:k for k, v in self._merge.items()}}
        fa = self.fixes_abbrevs
        ds = self.dupes_structs

        def do_struct_prov(structure, source=None, artiri=None):
            if artiri is None:
                artiri = source.artifact.iri
            if structure not in struct_prov:
                struct_prov[structure] = [artiri]
            elif artiri not in struct_prov[structure]:
                struct_prov[structure].append(artiri)

        def do_abbrev_prov(abbrev, primary_struct, source=None, artiri=None, overwrite=False):
            if artiri is None:
                artiri = source.artifact.iri
            if overwrite:
                abbrev_prov[abbrev, primary_struct] = artiri if isinstance(artiri, list) else [artiri]
            else:
                if (abbrev, primary_struct) not in abbrev_prov:
                    abbrev_prov[abbrev, primary_struct] = [artiri]
                elif artiri not in abbrev_prov[abbrev, primary_struct]:
                    abbrev_prov[abbrev, primary_struct].append(artiri)  # include all the prov we can

        for se in self.sources:
            source, errata = se
            for t in se.isVersionOf:
                self.addTrip(*t)
            for a, (ss, f, *_) in source.items():  # *_ eat the tree for now
                # TODO deal with overlapping layer names here
                if a in fa:  # XXX this is now just for dupes...
                    if ss[0] in ds:
                        print('TODO', a, ss, f)
                        collisions[a, ss[0]] = {se.artifact.iri:f}
                        continue  # skip the entries that we create manually TODO

                do_abbrev_prov(a, ss[0], se)
                for s in ss:
                    do_struct_prov(s, se)
                if a in combined_record:
                    _, structures, figures, artifacts = combined_record[a]
                    if f:
                        assert (se.artifact.iri not in figures or
                                figures[se.artifact.iri] == f), f'>1 figures {a} {figures} {bool(f)}'
                        figures[se.artifact.iri] = f
                    for s in ss:
                        if s is not None and s not in structures:
                            structures.append(s)
                    if se.artifact.iri not in artifacts:
                        artifacts.append(se.artifact.iri)
                elif a in merge and merge[a] in combined_record:
                    alt_abbrevs, structures, figures, artifacts = combined_record[merge[a]]
                    for struct in structures:  # allow merge of terms with non exact matching but warn
                        if struct not in ss:
                            if ss: print(tc.red('WARNING:'), f'adding structure {struct} in merge of {a}')
                            ss.append(struct)
                    for aa in alt_abbrevs:
                        do_abbrev_prov(aa, ss[0], se)

                    alt_abbrevs.append(a)
                    figures[se.artifact.iri] = f
                    if se.artifact.iri not in artifacts:
                        artifacts.append(se.artifact.iri)
                else:
                    ss = [s for s in ss if s is not None]
                    alt_abbrevs = self._dupes[a].alt_abbrevs if a in self._dupes else []
                    for aa in alt_abbrevs:
                        for artiri in self._dupes[a].artiris:  # TODO check if matches current source art iri?
                            do_abbrev_prov(aa, ss[0], artiri=artiri)
                    if ss:  # skip terms without structures
                        combined_record[a] = alt_abbrevs, ss, {se.artifact.iri:f}, [se.artifact.iri]
                    if alt_abbrevs:  # TODO will need this for some abbrevs too...
                        artiris = self._dupes[a].artiris
                        for s in self._dupes[a].structures:
                            if s not in ss:
                                ss.append(s)
                            for artiri in artiris:
                                artifacts = combined_record[a][-1]
                                if artiri not in artifacts:
                                    artifacts.append(artiri)

                                do_struct_prov(s, artiri=artiri)
                        #abbrev_prov[a, ss[0]] = [se.artifact.iri]  # FIXME overwritten?
                        do_abbrev_prov(a, ss[0], se)
                        for alt in alt_abbrevs:
                            if alt not in abbrev_prov:
                                for artiri in artiris:
                                    do_abbrev_prov(alt, ss[0], artiri=artiri)

                            # TODO elif...

        return combined_record, struct_prov, collisions, abbrev_prov


class PaxMouseLabels(PaxLabels):
    """ Compilation of all labels used to name mouse brain regions
        in atlases created using Paxinos and Franklin\'s methodology."""

    # TODO FIXME align indexes where possible to paxrat???

    filename = 'paxinos-mus-labels'
    name = 'Paxinos & Franklin Mouse Parcellation Labels'
    shortname = 'paxmus'
    namespace = PAXMUS

    prefixes = {**makePrefixes('NIFRID', 'ilxtr', 'prov', 'dcterms'),
                'PAXMUS':str(PAXMUS),
                'paxmusver':str(paxmusver),
    }
    sources = PaxMFix, PaxMSrAr_2, PaxMSrAr_3
    root = LabelRoot(iri=nsExact(namespace),  # PAXMUS['0'],
                     label='Paxinos mouse parcellation label root',
                     shortname=shortname,
                     definingArtifactsS=(Artifacts.PaxMouseAt.iri,),
    )

    _merge = {
        '4/5Cb':'4&5Cb',
        '5N':'Mo5',
        '12N':'12',
        'AngT':'Ang',
        'ANS':'Acc',
        'ASt':'AStr',
        'hif':'hf',
        'MnM':'MMn',
        'MoDG':'Mol',
        'och':'ox',
        'PHA':'PH',  # FIXME PH is reused in 3rd
        'ST':'BST',
        'STIA':'BSTIA',
        'STLD':'BSTLD',
        'STLI':'BSTLI',
        'STLJ':'BSTLJ',
        'STLP':'BSTLP',
        'STLV':'BSTLV',
        'STMA':'BSTMA',
        'STMP':'BSTMP',
        'STMPI':'BSTMPI',
        'STMPL':'BSTMPL',
        'STMPM':'BSTMPM',
        'STMV':'BSTMV',
        'STS':'BSTS',
    }


class PaxRatLabels(PaxLabels):
    """ Compilation of all labels used to name rat brain regions
        in atlases created using Paxinos and Watson\'s methodology."""

    filename = 'paxinos-rat-labels'
    name = 'Paxinos & Watson Rat Parcellation Labels'
    shortname = 'paxrat'
    namespace = PAXRAT

    prefixes = {**makePrefixes('NIFRID', 'ilxtr', 'prov', 'dcterms'),
                'PAXRAT':str(PAXRAT),
                'paxratver':str(paxratver),
    }
    # sources need to go in the order with which we want the labels to take precedence (ie in this case 6e > 4e)
    sources = PaxFix, PaxSrAr_6, PaxSr_6, PaxSrAr_4, PaxFix6, PaxFix4 #, PaxTree_6()  # tree has been successfully used for crossreferencing, additional terms need to be left out at the moment (see in_tree_not_in_six)
    root = LabelRoot(iri=nsExact(namespace),  # PAXRAT['0'],
                     label='Paxinos rat parcellation label root',
                     shortname=shortname,
                     #definingArtifactsS=None,#Artifacts.PaxRatAt.iri,
                     definingArtifactsS=(Artifacts.PaxRatAt.iri,),
    )

    _fixes = []

    _dupes = {
        # for 4e the numbers in the index are to the cranial nerve nuclei entries
        '3N':DupeRecord(alt_abbrevs=['3'], structures=['oculomotor nucleus'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '4N':DupeRecord(alt_abbrevs=['4'], structures=['trochlear nucleus'],  figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '6N':DupeRecord(alt_abbrevs=['6'], structures=['abducens nucleus'],   figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '7N':DupeRecord(alt_abbrevs=['7'], structures=['facial nucleus'],     figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '10N':DupeRecord(alt_abbrevs=['10'], structures=['dorsal motor nucleus of vagus'], figures={}, artiris=[Artifacts.PaxRat4.iri]),

        # FIXME need comments about the index entries
        '1Cb':DupeRecord(alt_abbrevs=['1'], structures=['cerebellar lobule 1'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '2Cb':DupeRecord(alt_abbrevs=['2'], structures=['cerebellar lobule 2'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '2/3Cb':DupeRecord(alt_abbrevs=['2&3'], structures=['cerebellar lobules 2&3'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '3Cb':DupeRecord(alt_abbrevs=['3'], structures=['cerebellar lobule 3'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '4Cb':DupeRecord(alt_abbrevs=['4'], structures=['cerebellar lobule 4'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '4/5Cb':DupeRecord(alt_abbrevs=['4&5'], structures=['cerebellar lobules 4&5'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '5Cb':DupeRecord(alt_abbrevs=['5'], structures=['cerebellar lobule 5'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '6Cb':DupeRecord(alt_abbrevs=['6'], structures=['cerebellar lobule 6'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '6aCb':DupeRecord(alt_abbrevs=['6a'], structures=['cerebellar lobule 6a'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '6bCb':DupeRecord(alt_abbrevs=['6b'], structures=['cerebellar lobule 6b'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '6cCb':DupeRecord(alt_abbrevs=['6c'], structures=['cerebellar lobule 6c'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '7Cb':DupeRecord(alt_abbrevs=['7'], structures=['cerebellar lobule 7'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '8Cb':DupeRecord(alt_abbrevs=['8'], structures=['cerebellar lobule 8'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '9Cb':DupeRecord(alt_abbrevs=['9'], structures=['cerebellar lobule 9'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
        '10Cb':DupeRecord(alt_abbrevs=['10'], structures=['cerebellar lobule 10'], figures={}, artiris=[Artifacts.PaxRat4.iri]),
    }

    _merge = {  # abbrevs that have identical structure names
        '5N':'Mo5',
        '12N':'12',
        'ANS':'Acc',
        'ASt':'AStr',
        'AngT':'Ang',
        'MnM':'MMn',
        'MoDG':'Mol',
        'PDPO':'PDP',
        'PTg':'PPTg',
        'STIA':'BSTIA',
        'STL':'BSTL',
        'STLD':'BSTLD',
        'STLI':'BSTLI',
        'STLJ':'BSTLJ',
        'STLP':'BSTLP',
        'STLV':'BSTLV',
        'STM':'BSTM',
        'STMA':'BSTMA',
        'STMP':'BSTMP',
        'STMPI':'BSTMPI',
        'STMPL':'BSTMPL',
        'STMPM':'BSTMPM',
        'STMV':'BSTMV',
        'hif':'hf',
        'och':'ox',
    }

    def curate(self):
        fr, err4 = PaxSrAr_4()
        sx, err6 = PaxSrAr_6()
        sx2, _ = PaxSr_6()
        tr, err6t = PaxTree_6()

        sfr = set(fr)
        ssx = set(sx)
        ssx2 = set(sx2)
        str_ = set(tr)
        in_four_not_in_six = sfr - ssx
        in_six_not_in_four = ssx - sfr
        in_tree_not_in_six = str_ - ssx
        in_six_not_in_tree = ssx - str_
        in_six2_not_in_six = ssx2 - ssx
        in_six_not_in_six2 = ssx - ssx2

        print(len(in_four_not_in_six), len(in_six_not_in_four),
              len(in_tree_not_in_six), len(in_six_not_in_tree),
              len(in_six2_not_in_six), len(in_six_not_in_six2),
        )
        tr_struct_abrv = {}
        for abrv, ((struct, *extra), _, parent) in tr.items():
            tr_struct_abrv[struct] = abrv
            if abrv in sx:
                #print(abrv, struct, parent)
                if struct and struct not in sx[abrv][0]:
                    print(f'Found new label from tr for {abrv}:\n{struct}\n{sx[abrv][0]}\n')

        # can't run these for tr yet
        #reduced = set(tr_struct_abrv.values())
        #print(sorted(_ for _ in tr if _ not in reduced))
        #assert len(tr_struct_abrv) == len(tr), 'mapping between abrvs and structs is not 1:1 for tr'

        sx2_struct_abrv = {}
        for abrv, ((struct, *extra), _) in sx2.items():
            sx2_struct_abrv[struct] = abrv
            if abrv in sx:
                if struct and struct not in sx[abrv][0]:
                    print(f'Found new label from sx2 for {abrv}:\n{struct}\n{sx[abrv][0]}\n')

        reduced = set(sx2_struct_abrv.values())
        print(sorted(_ for _ in reduced if _ not in sx2))  # ah inconsistent scoping rules in class defs...
        assert len(sx2_struct_abrv) == len(sx2), 'there is a duplicate struct'

        sx_struct_abrv = {}
        for abrv, ((struct, *extra), _) in sx.items():
            sx_struct_abrv[struct] = abrv

        reduced = set(sx_struct_abrv.values())
        print(sorted(_ for _ in reduced if _ not in sx))
        assert len(sx_struct_abrv) == len(sx), 'there is a duplicate struct'

        # TODO test whether any of the tree members that were are going to exclude have children that we are going to include

        names_match_not_abbervs = {}

        tree_no_name = {_:tr[_] for _ in sorted(in_tree_not_in_six) if not tr[_][0][0]}
        tree_with_name = {_:tr[_] for _ in sorted(in_tree_not_in_six) if tr[_][0][0]}
        not_in_tree_with_figures = {_:sx[_] for _ in sorted(in_six_not_in_tree) if sx[_][-1]}
        a = f'{"abv":<25} | {"structure name":<60} | parent abv\n' + '\n'.join(f'{k:<25} | {v[0][0]:<60} | {v[-1]}' for k, v in tree_with_name.items())
        b = f'{"abv":<25} | {"structure name":<15} | parent abv\n' + '\n'.join(f'{k:<25} | {"":<15} | {v[-1]}' for k, v in tree_no_name.items())
        c = f'abv    | {"structure name":<60} | figures (figure ranges are tuples)\n' + '\n'.join(f'{k:<6} | {v[0][0]:<60} | {v[-1]}' for k, v in not_in_tree_with_figures.items())
        with open(os.path.expanduser('~/ni/dev/nifstd/paxinos/tree-with-name.txt'), 'wt') as f: f.write(a)
        with open(os.path.expanduser('~/ni/dev/nifstd/paxinos/tree-no-name.txt'), 'wt') as f: f.write(b)
        with open(os.path.expanduser('~/ni/dev/nifstd/paxinos/not-in-tree-with-figures.txt'), 'wt') as f: f.write(c)
        #match_name_not_abrev = set(v[0][0] for v in tree_with_name.values()) & set(v[0][0] for v in sx.values())

        _match_name_not_abrev = {}
        for a, (alts, (s, *extra), f, *_) in PaxRatLabels().records()[0].items():
            if s not in _match_name_not_abrev:
                _match_name_not_abrev[s] = [a]
            elif a not in _match_name_not_abrev[s]:
                _match_name_not_abrev[s].append(a)

        match_name_not_abrev = {k:v for k, v in _match_name_not_abrev.items() if len(v) > 1}

        abrv_match_not_name = {k:v[0] for k, v in PaxRatLabels().records()[0].items() if len(v[0]) > 1}
        _ = [print(k, *v[0]) for k, v in PaxRatLabels().records()[0].items() if len(v[0]) > 1]
        embed()

        #self.in_tree_not_in_six = in_tree_not_in_six  # need for skipping things that were not actually named by paxinos


#
# regions

class PaxRecord:
    # TODO collisions
    def __init__(self, source, abbreviation, structure, artifacts,
                 figures=tuple(),
                 synonyms=tuple(),
                 altAbbrevs=tuple()):
        self.source = source
        self.abbreviation = abbreviation
        self.structure = structure
        self.artifacts = artifacts

    def __iter__(self):
        pass

    def __hash__(self):
        return hash(self.abbreviation)


class PaxRegion(RegionsBase):
    __pythonOnly = True  # TODO
    path = 'ttl/generated/parcellation/'
    filename = 'paxinos-rat-regions'
    name = 'Paxinos & Watson Rat Parcellation Regions'
    shortname = 'paxratr'
    comment = ('Intersection between labels and atlases for all regions '
               'delineated using Paxinos and Watson\'s methodology.')

    prefixes = {**makePrefixes('NIFRID', 'ilxtr', 'prov', 'ILXREPLACE')}
    # sources need to go in the order with which we want the labels to take precedence (ie in this case 6e > 4e)
    #sources = PaxSrAr_6(), PaxSr_6(), PaxSrAr_4(), PaxTree_6()  # tree has been successfully used for crossreferencing, additional terms need to be left out at the moment (see in_tree_not_in_six)
    root = RegionRoot(iri=TEMP['FIXME'],  # FIXME these should probably be EquivalentTo Parcellation Region HasLabel some label HasAtlas some atlas...
                      label='Paxinos rat parcellation region root',
                      shortname=shortname,
    )
    # atlas version
    # label identifier
    # figures

    things = {}

    @classmethod
    def addthing(cls, thing, value):
        cls.things[thing] = value


class FSL(LabelsBase):
    """ Ontology file containing labels from the FMRIB Software Library (FSL)
    atlases collection. All identifiers use the number of the index specified
    in the source xml file. """

    path = 'ttl/generated/parcellation/'
    filename = 'fsl'
    name = 'Terminologies from FSL atlases'
    shortname = 'fsl'
    imports = parcCore,
    prefixes = {**makePrefixes('ilxtr'), **Ont.prefixes,
                'FSLATS':str(FSLATS),
    }
    sources = tuple()  # set by prepare()
    roots = tuple()  # set by prepare()

    class Artifacts(Collector):
        """ Artifacts for FSL """
        collects = Artifact


    def _triples(self):
        for source in self.sources:
            for index, label in source:
                iri = source.root.namespace[str(index)]
                yield from Label(labelRoot=source.root,
                                 label=label,
                                 iri=iri)

    @classmethod
    def prepare(cls):
        ATLAS_PATH = '/usr/share/fsl/data/atlases/'

        shortnames = {
            'JHU White-Matter Tractography Atlas':'JHU WM',
            'Oxford-Imanova Striatal Structural Atlas':'OISS',
            'Talairach Daemon Labels':'Talairach',
            'Subthalamic Nucleus Atlas':'SNA',
            'JHU ICBM-DTI-81 White-Matter Labels':'JHU ICBM WM',
            'Juelich Histological Atlas':'Juelich',
            'MNI Structural Atlas':'MNI Struct',
        }

        prefixes = {
            'Cerebellar Atlas in MNI152 space after normalization with FLIRT':'CMNIfl',
            'Cerebellar Atlas in MNI152 space after normalization with FNIRT':'CMNIfn',
            'Sallet Dorsal Frontal connectivity-based parcellation':'DFCBP',
            'Neubert Ventral Frontal connectivity-based parcellation':'VFCBP',
            'Mars Parietal connectivity-based parcellation':'PCBP',
        }

        for xmlfile in glob.glob(ATLAS_PATH + '*.xml'):
            filename = os.path.splitext(os.path.basename(xmlfile))[0]

            tree = etree.parse(xmlfile)
            parcellation_name = tree.xpath('header//name')[0].text

            # namespace
            namespace = Namespace(FSLATS[filename + '/labels/'])

            # shortname
            shortname = tree.xpath('header//shortname')
            if shortname:
                shortname = shortname[0].text
            else:
                shortname = shortnames[parcellation_name]

            artifact_shortname = shortname
            shortname = shortname.replace(' ', '')

            # Artifact
            artifact = Terminology(iri=FSLATS[filename],
                                   label=parcellation_name,
                                   docUri='http://fsl.fmrib.ox.ac.uk/fsl/fslwiki/Atlases',
                                   species=NCBITaxon['9606'],
                                   devstage=UBERON['0000113'],  # FIXME mature vs adult vs when they actually did it...
                                   shortname=artifact_shortname)
            setattr(cls.Artifacts, shortname, artifact)

            # LabelRoot
            root = LabelRoot(iri=nsExact(namespace),
                             label=parcellation_name + ' label root',
                             shortname=shortname,
                             definingArtifacts=(artifact.iri,))
            root.namespace = namespace
            cls.roots += root,

            # prefix
            if parcellation_name in prefixes:
                prefix = 'fsl' + prefixes[parcellation_name]
            else:
                prefix = 'fsl' + shortname

            cls.prefixes[prefix] = root.iri

            # Source
            @classmethod
            def loadData(cls, _tree=tree):
                out = []
                for node in _tree.xpath('data//label'):
                    index, label = node.get('index'), node.text
                    out.append((index, label))
                return out

            source = type('FSLsource_' + shortname.replace(' ', '_'),
                          (Source,),
                          dict(iri=URIRef('file://' + xmlfile),
                               source=xmlfile,
                               source_original=True,
                               artifact=artifact,
                               root=root,  # used locally since we have more than one root per ontology here
                               loadData=loadData))
            cls.sources += source,

        super().prepare()


def main():
    from docopt import docopt
    args = docopt(__doc__, version='parcellation 0.0.1')
    # import all ye submodules we have it sorted! LabelBase will find everything for us. :D
    if not args['--local']:
        from parc_aba import Artifacts as abaArts
    from parc_freesurfer import Artifacts as fsArts
    from parc_whs import Artifacts as whsArts
    onts = tuple(l for l in subclasses(Ont)
                 if l.__name__ != 'parcBridge' and
                 l.__module__ != 'pyontutils.parcellation' and
                 not hasattr(l, f'_{l.__name__}__pythonOnly'))
    _ = *(print(ont) for ont in onts),
    out = build(*onts,
                parcBridge,
                fail=args['--fail'],
                n_jobs=int(args['--jobs']))
    embed()

if __name__ == '__main__':
    main()

