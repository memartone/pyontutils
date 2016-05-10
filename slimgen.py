#!/usr/bin/env python3
"""
    Build lightweight slims from curie lists.
    Used for sources that don't have an owl ontology floating.
"""
#TODO consider using some of the code from scr_sync.py???

import json
from datetime import date
import rdflib
from rdflib.extras import infixowl
import requests
from utils import makeGraph, add_hierarchy
from IPython import embed

#
PARC_REGION_SUPER = rdflib.URIRef('http://uri.interlex.org/base/ilx_brain_parcellation_scheme_concept')

# TODO source this from somewhere?
curie_mapping = {
    'ilx':'http://uri.interlex.org/base/',
    'obo':'http://purl.obolibrary.org/obo/',
    'OBOANN':'http://ontology.neuinfo.org/NIF/Backend/OBO_annotation_properties.owl#',  # FIXME needs to die a swift death
    'MBA':'http://api.brain-map.org/api/v2/data/Structure/',
    'owl':'http://www.w3.org/2002/07/owl#',  # this should autoadd for prefixes but doesnt!?
}

g = makeGraph('mbaslim', curie_mapping)  # FIXME ANNOYING :/ this shouldn't need to go out here :/

#edge_types = {
    #namespaces['OBOANN']['acronym']:str,
    #namespaces['ABA'],
    #rdflib.RDFS['label']:str,
    #rdflib.RDFS['subClassOf']:rdflib.URIRef,
    #namespaces['OBOANN']['synonym']:str,
#}

aba_map = {
    'acronym':g.namespaces['OBOANN']['acronym'],  # FIXME all this is BAD WAY
    #'id':namespaces['ABA'],
    'name':rdflib.RDFS.label,
    #'parent_structure_id':rdflib.RDFS['subClassOf'],
    'safe_name':g.namespaces['OBOANN']['synonym'],
}

def aba_trips(node_d):
    output = []
    parent = 'MBA:' + str(node_d['id'])  # FIXME HRM what happens if we want to change ABA:  OH LOOK
    for key, edge in sorted(aba_map.items()):
        value = node_d[key]
        if not value:
            continue
        elif key == 'safe_name' and value == node_d['name']:
            continue  # don't duplicate labels as synonyms
        output.append( (parent, edge, value) )
    return output

def aba_make():
    root = 997  # for actual parts of the brain
    url = 'http://api.brain-map.org/api/v2/tree_search/Structure/997.json?descendants=true'
    superclass = rdflib.URIRef('http://uri.interlex.org/base/ilx_allen_brain_parc_region')
    resp = requests.get(url).json()
    for node_d in resp['msg']:
        if node_d['id'] == 997:  # FIXME need a better place to document this :/
            node_d['name'] = 'allen mouse brain atlas parcellation root'
            node_d['safe_name'] = 'allen mouse brain atlas parcellation root'
            node_d['acronym'] = 'mbaroot'
        ident = g.namespaces['MBA'][str(node_d['id'])]
        cls = infixowl.Class(ident, graph=g.g)
        cls.subClassOf = [superclass]
        parent = node_d['parent_structure_id']
        if parent:
            parent = g.namespaces['MBA'][str(parent)]
            #add_hierarchy(g.g, parent, rdflib.URIRef('http://uri.interlex.org/base/proper_part_of'), cls)
            add_hierarchy(g.g, parent, rdflib.URIRef('http://purl.obolibrary.org/obo/BFO_0000050'), cls)

        for t in aba_trips(node_d):
            g.add_node(*t)

    g.add_node(superclass, rdflib.RDFS.label, 'Allen Mouse Brain Atlas brain region')
    g.add_node(superclass, rdflib.RDFS.subClassOf, PARC_REGION_SUPER)

    ontid = 'http://ontology.neuinfo.org/NIF/ttl/generated/mbaslim.ttl'
    g.add_node(ontid, rdflib.RDF.type, rdflib.OWL.Ontology)
    g.add_node(ontid, rdflib.RDFS.label, 'Allen Mouse Brain Atlas Ontology')
    g.add_node(ontid, rdflib.RDFS.comment, 'This file is automatically generated from the Allen Brain Atlas API')
    g.add_node(ontid, rdflib.OWL.versionInfo, date.isoformat(date.today()))
    g.write()


class rowParse:
    def __init__(self, rows, header, order=[]):
        self.lookup = {index:name for index, name in enumerate(header)}
        self._next_rows(rows)

    def _next_rows(self, rows):
        for row in rows:
            for i, value in enumerate(row):
                func = getattr(self, self.lookup[i], None)
                if func:
                    func(value)
            
class cocomac(rowParse):
    superclass = rdflib.URIRef('http://uri.interlex.org/base/ilx_cocomac_parc_region')
    def __init__(self, graph, rows, header):
        self.g = graph
        super().__init__(rows, header)#, order=[0])

    def ID(self, value):
        self.identifier = 'cocomac:' + value  # safe because reset every row (ish)
        self.g.add_node(self.identifier, rdflib.RDF.type, rdflib.OWL.Class)
        self.g.add_node(self.identifier, rdflib.RDFS.subClassOf, self.superclass)

    def Key(self, value):
        pass

    def Summary(self, value):
        pass

    def Acronym(self, value):
        self.g.add_node(self.identifier, 'OBOANN:acronym', value)

    def FullName(self, value):
        self.g.add_node(self.identifier, rdflib.RDFS.label, value)

    def LegacyID(self, value):
        if value:  # FIXME should fix in add_node
            self.g.add_node(self.identifier, 'OBOANN:acronym', value)

    def BrainInfoID(self, value):
        pass

def cocomac_make():
    #url = 'http://cocomac.g-node.org/services/search_wizard.php?T=BrainMaps_BrainSiteAcronyms&x0=&limit=3000&page=1&format=json'
    #resp = json.loads(requests.get(url).json())  # somehow everything is double escaped :x
    base_format = 'http://cocomac.g-node.org/services/custom_sql_query.php?sql=SELECT%20*%20from%20BrainMaps_BrainSiteAcronyms%20where%20ID='
    url = 'http://cocomac.g-node.org/services/custom_sql_query.php?sql=SELECT * from BrainMaps_BrainSiteAcronyms;&format=json'
    #url = 'http://cocomac.g-node.org/services/custom_sql_query.php?sql=SELECT%20*%20from%20BrainMaps_BrainSiteAcronyms;&format=json'
    #tab_name = resp['resultTable']
    #table = resp['tables'][tab_name]
    table = requests.get(url).json()
    fields = table['fields']
    data = table['data']
    #rows = sorted(data.values())
    prefixes = {
        'ilx':'http://uri.interlex.org/base/',
        'OBOANN':'http://ontology.neuinfo.org/NIF/Backend/OBO_annotation_properties.owl#',  # FIXME needs to die a swift death
        'cocomac':base_format,
    }
    ccgraph = makeGraph('cocomacslim',prefixes)
    cocomac(ccgraph, data.values(), fields)
    ccgraph.add_node(cocomac.superclass, rdflib.RDFS.label, 'CoCoMac terminology brain region')
    ccgraph.add_node(cocomac.superclass, rdflib.RDFS.subClassOf, PARC_REGION_SUPER)

    ontid = 'http://ontology.neuinfo.org/NIF/ttl/generated/cocomacslim.ttl'
    ccgraph.add_node(ontid, rdflib.RDF.type, rdflib.OWL.Ontology)
    ccgraph.add_node(ontid, rdflib.RDFS.label, 'CoCoMac terminology')
    ccgraph.add_node(ontid, rdflib.RDFS.comment, 'This file is automatically generated from the CoCoMac database on the terms from BrainMaps_BrainSiteAcronyms.')
    ccgraph.add_node(ontid, rdflib.OWL.versionInfo, date.isoformat(date.today()))
    ccgraph.write()


def chunk_list(list_, size):  # from dumpnlx :/
    ll = len(list_)
    chunks = []
    for start, stop in zip(range(0, ll, size), range(size, ll, size)):
        chunks.append(list_[start:stop])
    chunks.append(list_[stop:])  # snag unaligned chunks from last stop
    return chunks

#ncbi_map = {
    #'name':,
    #'description':,
    #'uid':,
    #'organism':{''},
    #'otheraliases':,
    #'otherdesignations':,
#}

class dictParse:
    def __init__(self, thing, order=[]):
        if type(thing) == dict:
            if order:
                for key in order:
                    func = getattr(self, key, None)
                    if func:
                        func(thing.pop(key))
            self._next_dict(thing)

        #elif type(thing) == list:
            #self._next_list(thing)
        else:
            print('NOPE')

    def _next_dict(self, dict_):
        for key, value in dict_.items():
            func = getattr(self, key, None)
            if func:
                func(value)

    def _next_list(self, list_):
        for value in list_:
            if type(value) == dict:
                self._next_dict(value)

    def _terminal(self, value):
        print(value)
        pass
        
class ncbi(dictParse):
    superclass = rdflib.URIRef('http://uri.interlex.org/base/ilx_gene_concept')
    def __init__(self, thing, graph):
        self.g = graph
        super().__init__(thing, order=['uid'])

    def name(self, value):
        self.g.add_node(self.identifier, rdflib.RDFS.label, value)

    def description(self, value):
        #if value:
        self.g.add_node(self.identifier, 'ilx:display_label', value)

    def uid(self, value):
        self.identifier = 'NCBIGene:' + str(value)
        self.g.add_node(self.identifier, rdflib.RDF.type, rdflib.OWL.Class)
        self.g.add_node(self.identifier, rdflib.RDFS.subClassOf, self.superclass)

    def organism(self, value):
        self._next_dict(value)

    def taxid(self, value):
        tax = 'NCBITaxon:' + str(value)
        self.g.add_node(self.identifier, 'ilx:has_taxon', tax)

    def otheraliases(self, value):
        if value:
            for synonym in value.split(','):
                self.g.add_node(self.identifier, 'OBOANN:synonym', synonym.strip())

    def otherdesignations(self, value):
        if value:
            for synonym in value.split('|'):
                self.g.add_node(self.identifier, 'OBOANN:synonym', synonym)

def ncbigene_make():
    with open('gene-subset-ids.txt', 'rt') as f:  # this came from neuroNER
        ids = [l.split(':')[1].strip() for l in f.readlines()]
    
    #url = 'http://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?retmode=json&retmax=5000&db=gene&id='
    #for id_ in ids:
        #data = requests.get(url + id_).json()['result'][id_]
    url = 'http://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
    data = {
        'db':'gene',
        'retmode':'json',
        'retmax':5000,
        'id':None,
    }
    chunks = []
    for i, idset in enumerate(chunk_list(ids, 100)):
        print(i, len(idset))
        data['id'] = ','.join(idset),
        resp = requests.post(url, data=data).json()
        chunks.append(resp)
    
    base = chunks[0]['result']
    uids = base['uids']
    for more in chunks[1:]:
        data = more['result']
        uids.extend(data['uids'])
        base.update(data)
    #base['uids'] = uids  # i mean... its just the keys
    base.pop('uids')
 
    prefixes = {
        'ilx':'http://uri.interlex.org/base/',
        'OBOANN':'http://ontology.neuinfo.org/NIF/Backend/OBO_annotation_properties.owl#',  # FIXME needs to die a swift death
        'NCBIGene':'http://www.ncbi.nlm.nih.gov/gene/',
        'NCBITaxon':'http://purl.obolibrary.org/obo/NCBITaxon_',
    }
    ng = makeGraph('ncbigeneslim', prefixes)

    for k, v in base.items():
        #if k != 'uids':
        ncbi(v, ng)

    ontid = 'http://ontology.neuinfo.org/NIF/ttl/generated/ncbigeneslim.ttl'
    ng.add_node(ontid, rdflib.RDF.type, rdflib.OWL.Ontology)
    ng.add_node(ontid, rdflib.RDFS.label, 'NIF NCBI Gene subset')
    ng.add_node(ontid, rdflib.RDFS.comment, 'This subset is automatically generated from the NCBI Gene database on a subset of terms.')
    ng.add_node(ontid, rdflib.OWL.versionInfo, date.isoformat(date.today()))
    ng.write()
    #embed()

def main():
    aba_make()
    cocomac_make()
    #ncbigene_make()

if __name__ == '__main__':
    main()
