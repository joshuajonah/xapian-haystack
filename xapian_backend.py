# Copyright (C) 2009 David Sauve
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

import datetime
import cPickle as pickle
import os
import re
import shutil
import sys
import warnings

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.encoding import smart_unicode, force_unicode

from haystack.backends import BaseSearchBackend, BaseSearchQuery
from haystack.exceptions import MissingDependency
from haystack.fields import DateField, DateTimeField, IntegerField, FloatField, BooleanField, MultiValueField
from haystack.models import SearchResult

try:
    import xapian
except ImportError:
    raise MissingDependency("The 'xapian' backend requires the installation of 'xapian'. Please refer to the documentation.")


DEFAULT_MAX_RESULTS = 100000

DOCUMENT_ID_TERM_PREFIX = 'Q'
DOCUMENT_CUSTOM_TERM_PREFIX = 'X'
DOCUMENT_CT_TERM_PREFIX = DOCUMENT_CUSTOM_TERM_PREFIX + 'CONTENTTYPE'


class XHValueRangeProcessor(xapian.ValueRangeProcessor):
    def __init__(self, sb):
        self.sb = sb
        xapian.ValueRangeProcessor.__init__(self)
    
    def __call__(self, begin, end):
        """
        Construct a tuple for value range processing.
        
        `begin` -- a string in the format '<field_name>:[low_range]'
                   If 'low_range' is omitted, assume the smallest possible value.
        `end` -- a string in the the format '[high_range|*]'.  If '*', assume
                 the highest possible value.
        
        Return a tuple of three strings: (column, low, high)
        """
        colon = begin.find(':')
        field_name = begin[:colon]
        begin = begin[colon + 1:len(begin)]
        for field_dict in self.sb.schema:
            if field_dict['field_name'] == field_name:
                if not begin:
                    if field_dict['type'] == 'text':
                        begin = u'a' # TODO: A better way of getting a min text value?
                    elif field_dict['type'] == 'long':
                        begin = -sys.maxint - 1
                    elif field_dict['type'] == 'float':
                        begin = float('-inf')
                    elif field_dict['type'] == 'date' or field_dict['type'] == 'datetime':
                        begin = u'00010101000000'
                elif end == '*':
                    if field_dict['type'] == 'text':
                        end = u'z' * 100 # TODO: A better way of getting a max text value?
                    elif field_dict['type'] == 'long':
                        end = sys.maxint
                    elif field_dict['type'] == 'float':
                        end = float('inf')
                    elif field_dict['type'] == 'date' or field_dict['type'] == 'datetime':
                        end = u'99990101000000'
                if field_dict['type'] == 'float':
                    begin = self.sb._marshal_value(float(begin))
                    end = self.sb._marshal_value(float(end))
                elif field_dict['type'] == 'long':
                    begin = self.sb._marshal_value(long(begin))
                    end = self.sb._marshal_value(long(end))
                return field_dict['column'], str(begin), str(end)


class XHExpandDecider(xapian.ExpandDecider):
    def __call__(self, term):
        """
        Return True if the term should be used for expanding the search
        query, False otherwise.
        
        Currently, we only want to ignore terms beginning with `DOCUMENT_CT_TERM_PREFIX`
        """
        if term.startswith(DOCUMENT_CT_TERM_PREFIX):
            return False
        return True


class SearchBackend(BaseSearchBackend):
    """
    `SearchBackend` defines the Xapian search backend for use with the Haystack
    API for Django search.
    
    It uses the Xapian Python bindings to interface with Xapian, and as
    such is subject to this bug: <http://trac.xapian.org/ticket/364> when
    Django is running with mod_python or mod_wsgi under Apache.
    
    Until this issue has been fixed by Xapian, it is neccessary to set
    `WSGIApplicationGroup to %{GLOBAL}` when using mod_wsgi, or
    `PythonInterpreter main_interpreter` when using mod_python.
    
    In order to use this backend, `HAYSTACK_XAPIAN_PATH` must be set in
    your settings.  This should point to a location where you would your
    indexes to reside.
    """
    RESERVED_WORDS = (
        'AND',
        'NOT',
        'OR',
        'XOR',
        'NEAR',
        'ADJ',
    )
    
    RESERVED_CHARACTERS = (
        '\\', '+', '-', '&&', '||', '!', '(', ')', '{', '}',
        '[', ']', '^', '"', '~', '*', '?', ':',
    )
    
    def __init__(self, site=None, stemming_language='english'):
        """
        Instantiates an instance of `SearchBackend`.
        
        Optional arguments:
            `site` -- The site to associate the backend with (default = None)
            `stemming_language` -- The stemming language (default = 'english')
        
        Also sets the stemming language to be used to `stemming_language`.
        """
        super(SearchBackend, self).__init__(site)
        
        if not hasattr(settings, 'HAYSTACK_XAPIAN_PATH'):
            raise ImproperlyConfigured('You must specify a HAYSTACK_XAPIAN_PATH in your settings.')
        
        if not os.path.exists(settings.HAYSTACK_XAPIAN_PATH):
            os.makedirs(settings.HAYSTACK_XAPIAN_PATH)
        
        self.stemmer = xapian.Stem(stemming_language)
    
    def get_identifier(self, obj_or_string):
        return DOCUMENT_ID_TERM_PREFIX + super(SearchBackend, self).get_identifier(obj_or_string)
    
    def update(self, index, iterable):
        """
        Updates the `index` with any objects in `iterable` by adding/updating
        the database as needed.
        
        Required arguments:
            `index` -- The `SearchIndex` to process
            `iterable` -- An iterable of model instances to index
        
        For each object in `iterable`, a document is created containing all
        of the terms extracted from `index.prepare(obj)` with stemming prefixes,
        field prefixes, and 'as-is'.
        
        eg. `content:Testing` ==> `testing, Ztest, ZXCONTENTtest`
        
        Each document also contains an extra term in the format:
        
        `XCONTENTTYPE<app_name>.<model_name>`
        
        As well as a unique identifier in the the format:
        
        `Q<app_name>.<model_name>.<pk>`
        
        eg.: foo.bar (pk=1) ==> `Qfoo.bar.1`, `XCONTENTTYPEfoo.bar`
        
        This is useful for querying for a specific document corresponding to
        a model instance.
        
        The document also contains a pickled version of the object itself and
        the document ID in the document data field.
        
        Finally, we also store field values to be used for sorting data.  We
        store these in the document value slots (position zero is reserver
        for the document ID).  All values are stored as unicode strings with
        conversion of float, int, double, values being done by Xapian itself
        through the use of the :method:xapian.sortable_serialise method.
        """
        database = self._database(writable=True)
        try:
            for obj in iterable:
                document = xapian.Document()
                term_generator = self._term_generator(database, document)
                document_id = self.get_identifier(obj)
                model_data = index.prepare(obj)
                
                for field in self.schema:
                    if field['field_name'] in model_data.keys():
                        prefix = DOCUMENT_CUSTOM_TERM_PREFIX + field['field_name'].upper()
                        value = model_data[field['field_name']]
                        term_generator.index_text(force_unicode(value))
                        term_generator.index_text(force_unicode(value), 1, prefix)
                        document.add_value(field['column'], self._marshal_value(value))
                
                document.set_data(pickle.dumps(
                    (obj._meta.app_label, obj._meta.module_name, obj.pk, model_data),
                    pickle.HIGHEST_PROTOCOL
                ))
                document.add_term(document_id)
                document.add_term(
                    DOCUMENT_CT_TERM_PREFIX + u'%s.%s' %
                    (obj._meta.app_label, obj._meta.module_name)
                )
                database.replace_document(document_id, document)
        
        except UnicodeDecodeError:
            sys.stderr.write('Chunk failed.\n')
            pass
    
    def remove(self, obj):
        """
        Remove indexes for `obj` from the database.
        
        We delete all instances of `Q<app_name>.<model_name>.<pk>` which
        should be unique to this object.
        """
        database = self._database(writable=True)
        database.delete_document(self.get_identifier(obj))
    
    def clear(self, models=[]):
        """
        Clear all instances of `models` from the database or all models, if
        not specified.
        
        Optional Arguments:
            `models` -- Models to clear from the database (default = [])
        
        If `models` is empty, an empty query is executed which matches all
        documents in the database.  Afterwards, each match is deleted.
        
        Otherwise, for each model, a `delete_document` call is issued with
        the term `XCONTENTTYPE<app_name>.<model_name>`.  This will delete
        all documents with the specified model type.
        """
        database = self._database(writable=True)
        if not models:
            query, __unused__ = self._query(database, '*')
            enquire = self._enquire(database, query)
            for match in enquire.get_mset(0, DEFAULT_MAX_RESULTS):
                database.delete_document(match.docid)
        else:
            for model in models:
                database.delete_document(
                    DOCUMENT_CT_TERM_PREFIX + '%s.%s' %
                    (model._meta.app_label, model._meta.module_name)
                )
    
    def search(self, query_string, sort_by=None, start_offset=0, end_offset=DEFAULT_MAX_RESULTS,
               fields='', highlight=False, facets=None, date_facets=None, query_facets=None,
               narrow_queries=None, boost=None, **kwargs):
        """
        Executes the search as defined in `query_string`.
        
        Required arguments:
            `query_string` -- Search query to execute
        
        Optional arguments:
            `sort_by` -- Sort results by specified field (default = None)
            `start_offset` -- Slice results from `start_offset` (default = 0)
            `end_offset` -- Slice results at `end_offset` (default = 10,000)
            `fields` -- Filter results on `fields` (default = '')
            `highlight` -- Highlight terms in results (default = False)
            `facets` -- Facet results on fields (default = None)
            `date_facets` -- Facet results on date ranges (default = None)
            `query_facets` -- Facet results on queries (default = None)
            `narrow_queries` -- Narrow queries (default = None)
            `boost` -- Dictionary of terms and weights to boost results
        
        Returns:
            A dictionary with the following keys:
                `results` -- A list of `SearchResult`
                `hits` -- The total available results
                `facets` - A dictionary of facets with the following keys:
                    `fields` -- A list of field facets
                    `dates` -- A list of date facets
                    `queries` -- A list of query facets
            If faceting was not used, the `facets` key will not be present
        
        If `query_string` is empty, returns no results.
        
        Otherwise, loads the available fields from the database meta data schema
        and sets up prefixes for each one along with a prefix for `django_ct`,
        used to filter by model, and loads the current stemmer instance.
        
        Afterwards, executes the Xapian query parser to create a query from
        `query_string` that is then passed to a new `enquire` instance.
        
        The resulting match set is passed to :method:`_process_results` for
        further processing prior to returning a dictionary with the results.
        
        If `HAYSTACK_INCLUDE_SPELLING` was enabled in `settings.py`, the
        extra flag `FLAG_SPELLING_CORRECTION` will be passed to the query parser
        and any suggestions for spell correction will be returned as well as
        the results.
        """
        if not query_string:
            return {
                'results': [],
                'hits': 0,
            }
        
        if query_facets is not None:
            warnings.warn("Query faceting has not been implemented yet.", Warning, stacklevel=2)
        
        database = self._database()
        query, spelling_suggestion = self._query(
            database, query_string, narrow_queries, boost
        )
        enquire = self._enquire(database, query)
        
        if sort_by:
            sorter = self._sorter(sort_by)
            enquire.set_sort_by_key_then_relevance(sorter, True)
        
        results = []
        facets_dict = {
            'fields': {},
            'dates': {},
            'queries': {},
        }
        matches = enquire.get_mset(start_offset, end_offset)
        
        for match in matches:
            app_label, module_name, pk, model_data = pickle.loads(match.document.get_data())
            if highlight and (len(query_string) > 0):
                model_data['highlighted'] = {
                    self.content_field_name: self._do_highlight(
                        model_data.get(self.content_field_name), query_string
                    )
                }
            results.append(
                SearchResult(app_label, module_name, pk, match.weight, **model_data)
            )
        
        if facets:
            facets_dict['fields'] = self._do_field_facets(results, facets)
        if date_facets:
            facets_dict['dates'] = self._do_date_facets(results, date_facets)
        if query_facets:
            facets_dict['queries'] = self._do_query_facets(results, query_facets)
        
        return {
            'results': results,
            'hits': matches.get_matches_estimated(),
            'facets': facets_dict,
            'spelling_suggestion': spelling_suggestion,
        }
    
    def delete_index(self):
        """
        Delete the index.
        
        This removes all indexes files and the `HAYSTACK_XAPIAN_PATH` folder.
        """
        if os.path.exists(settings.HAYSTACK_XAPIAN_PATH):
            shutil.rmtree(settings.HAYSTACK_XAPIAN_PATH)
    
    def document_count(self):
        """
        Retrieves the total document count for the search index.
        """
        try:
            database = self._database()
        except xapian.DatabaseOpeningError:
            return 0
        return database.get_doccount()
    
    def more_like_this(self, model_instance, additional_query_string=None,
                       start_offset=0, end_offset=DEFAULT_MAX_RESULTS, **kwargs):
        """
        Given a model instance, returns a result set of similar documents.
        
        Required arguments:
            `model_instance` -- The model instance to use as a basis for
                                retrieving similar documents.
        
        Optional arguments:
            `additional_query_string` -- An additional query string to narrow
                                         results
            `start_offset` -- The starting offset (default=0)
            `end_offset` -- The ending offset (default=None)
        
        Returns:
            A dictionary with the following keys:
                `results` -- A list of `SearchResult`
                `hits` -- The total available results
        
        Opens a database connection, then builds a simple query using the
        `model_instance` to build the unique identifier.
        
        For each document retrieved(should always be one), adds an entry into
        an RSet (relevance set) with the document id, then, uses the RSet
        to query for an ESet (A set of terms that can be used to suggest
        expansions to the original query), omitting any document that was in
        the original query.
        
        Finally, processes the resulting matches and returns.
        """
        database = self._database()
        query = xapian.Query(self.get_identifier(model_instance))
        enquire = self._enquire(database, query)
        rset = xapian.RSet()
        for match in enquire.get_mset(0, DEFAULT_MAX_RESULTS):
            rset.add_document(match.docid)
        query = xapian.Query(xapian.Query.OP_OR,
            [expand.term for expand in enquire.get_eset(DEFAULT_MAX_RESULTS, rset, XHExpandDecider())]
        )
        query = xapian.Query(
            xapian.Query.OP_AND_NOT, [query, self.get_identifier(model_instance)]
        )
        if additional_query_string:
            additional_query, __unused__ = self._query(
                database, additional_query_string
            )
            query = xapian.Query(
                xapian.Query.OP_AND, query, additional_query
            )
        enquire.set_query(query)
        
        results = []
        matches = enquire.get_mset(start_offset, end_offset)
        
        for match in matches:
            document = match.get_document()
            app_label, module_name, pk, model_data = pickle.loads(document.get_data())
            results.append(
                SearchResult(app_label, module_name, pk, match.weight, **model_data)
            )
        
        return {
            'results': results,
            'hits': matches.get_matches_estimated(),
            'facets': {
                'fields': {},
                'dates': {},
                'queries': {},
            },
            'spelling_suggestion': None,
        }
    
    def build_schema(self, fields):
        """
        Build the schema from fields.
        
        Required arguments:
            ``fields`` -- A list of fields in the index
        
        Returns a list of fields in dictionary format ready for inclusion in
        an indexed meta-data.
        """
        content_field_name = ''
        schema_fields = []
        column = 0
        
        for field_name, field_class in fields.items():
            if field_class.document is True:
                content_field_name = field_name
            
            if field_class.indexed is True:
                field_data = {
                    'field_name': field_name,
                    'type': 'text',
                    'multi_valued': 'false',
                    'column': column,
                }
                
                if isinstance(field_class, (DateField, DateTimeField)):
                    field_data['type'] = 'date'
                elif isinstance(field_class, IntegerField):
                    field_data['type'] = 'long'
                elif isinstance(field_class, FloatField):
                    field_data['type'] = 'float'
                elif isinstance(field_class, BooleanField):
                    field_data['type'] = 'boolean'
                elif isinstance(field_class, MultiValueField):
                    field_data['multi_valued'] = 'true'
                
                schema_fields.append(field_data)
                column += 1
        
        return (content_field_name, schema_fields)
    
    def _do_highlight(self, content, text, tag='em'):
        """
        Highlight `text` in `content` with html `tag`.
        
        This method assumes that the input text (`content`) does not contain
        any special formatting.  That is, it does not contain any html tags
        or similar markup that could be screwed up by the highlighting.
        
        Required arguments:
            `content` -- Content to search for instances of `text`
            `text` -- The text to be highlighted
        """
        for term in [term.replace('*', '') for term in text.split()]:
            if term not in ('AND','OR'):
                term_re = re.compile(re.escape(term), re.IGNORECASE)
                content = term_re.sub('<%s>%s</%s>' % (tag, term, tag), content)
        return content
    
    def _do_field_facets(self, results, field_facets):
        """
        Private method that facets a document by field name.
        
        Required arguments:
            `results` -- A list SearchResults to facet
            `field_facets` -- A list of fields to facet on
        """
        facet_dict = {}
        
        for field in field_facets:
            facet_list = {}
            
            for result in results:
                field_value = getattr(result, field)
                facet_list[field_value] = facet_list.get(field_value, 0) + 1
            
            facet_dict[field] = facet_list.items()
        
        return facet_dict
    
    def _do_date_facets(self, results, date_facets):
        """
        Private method that facets a document by date ranges
        
        Required arguments:
            `results` -- A list SearchResults to facet
            `date_facets` -- A dictionary containing facet parameters:
                {'field': {'start_date': ..., 'end_date': ...: 'gap_by': '...', 'gap_amount': n}}
                nb., gap must be one of the following:
                    year|month|day|hour|minute|second
        
        For each date facet field in `date_facets`, generates a list
        of date ranges (from `start_date` to `end_date` by `gap_by`) then
        iterates through `results` and tallies the count for each date_facet.
        
        Returns a dictionary of date facets (fields) containing a list with
        entries for each range and a count of documents matching the range.
        
        eg. {
            'pub_date': [
                ('2009-01-01T00:00:00Z', 5),
                ('2009-02-01T00:00:00Z', 0),
                ('2009-03-01T00:00:00Z', 0),
                ('2009-04-01T00:00:00Z', 1),
                ('2009-05-01T00:00:00Z', 2),
            ],
        }
        """
        facet_dict = {}
        
        for date_facet, facet_params in date_facets.iteritems():
            gap_type = facet_params.get('gap_by')
            gap_value = facet_params.get('gap_amount', 1)
            date_range = facet_params['start_date']
            facet_list = []
            while date_range < facet_params['end_date']:
                facet_list.append((date_range.isoformat(), 0))
                if gap_type == 'year':
                    date_range = date_range.replace(
                        year=date_range.year + int(gap_value)
                    )
                elif gap_type == 'month':
                    if date_range.month == 12:
                        date_range = date_range.replace(
                            month=1, year=date_range.year + int(gap_value)
                        )
                    else:
                        date_range = date_range.replace(
                            month=date_range.month + int(gap_value)
                        )
                elif gap_type == 'day':
                    date_range += datetime.timedelta(days=int(gap_value))
                elif gap_type == 'hour':
                    date_range += datetime.timedelta(hours=int(gap_value))
                elif gap_type == 'minute':
                    date_range += datetime.timedelta(minutes=int(gap_value))
                elif gap_type == 'second':
                    date_range += datetime.timedelta(seconds=int(gap_value))
            
            facet_list = sorted(facet_list, key=lambda n:n[0], reverse=True)
            
            for result in results:
                result_date = getattr(result, date_facet)
                if result_date:
                    if not isinstance(result_date, datetime.datetime):
                        result_date = datetime.datetime(
                            year=result_date.year,
                            month=result_date.month,
                            day=result_date.day,
                        )
                    for n, facet_date in enumerate(facet_list):
                        if result_date > datetime.datetime.strptime(facet_date[0], '%Y-%m-%dT%H:%M:%S'):
                            facet_list[n] = (facet_list[n][0], (facet_list[n][1] + 1))
                            break
            
            facet_dict[date_facet] = facet_list
        
        return facet_dict
    
    def _do_query_facets(self, results, query_facets):
        """
        Private method that facets a document by query
        
        Required arguments:
            `results` -- A list SearchResults to facet
            `query_facets` -- A dictionary containing facet parameters:
                {'field': 'query', [...]}
        
        For each query in `query_facets`, generates a dictionary entry with
        the field name as the key and a tuple with the query and result count
        as the value.
        
        eg. {'name': ('a*', 5)}
        """
        facet_dict = {}
        
        for field, query in query_facets.iteritems():
            facet_dict[field] = (query, self.search(query)['hits'])
        
        return facet_dict
    
    def _marshal_value(self, value):
        """
        Private method that converts Python values to a string for Xapian values.
        """
        if isinstance(value, datetime.datetime):
            if value.microsecond:
                value = u'%04d%02d%02d%02d%02d%02d%06d' % (
                    value.year, value.month, value.day, value.hour,
                    value.minute, value.second, value.microsecond
                )
            else:
                value = u'%04d%02d%02d%02d%02d%02d' % (
                    value.year, value.month, value.day, value.hour,
                    value.minute, value.second
                )
        elif isinstance(value, datetime.date):
            value = u'%04d%02d%02d000000' % (value.year, value.month, value.day)
        elif isinstance(value, bool):
            if value:
                value = u't'
            else:
                value = u'f'
        elif isinstance(value, float):
            value = xapian.sortable_serialise(value)
        elif isinstance(value, (int, long)):
            value = u'%012d' % value
        else:
            value = force_unicode(value)
        return value
    
    def _database(self, writable=False):
        """
        Private method that returns a xapian.Database for use and sets up
        schema and content_field definitions.
        
        Optional arguments:
            ``writable`` -- Open the database in read/write mode (default=False)
        
        Returns an instance of a xapian.Database or xapian.WritableDatabase
        """
        if writable:
            self.content_field_name, self.schema = self.build_schema(self.site.all_searchfields())
            
            database = xapian.WritableDatabase(settings.HAYSTACK_XAPIAN_PATH, xapian.DB_CREATE_OR_OPEN)
            database.set_metadata('schema', pickle.dumps(self.schema, pickle.HIGHEST_PROTOCOL))
            database.set_metadata('content', pickle.dumps(self.content_field_name, pickle.HIGHEST_PROTOCOL))
        else:
            database = xapian.Database(settings.HAYSTACK_XAPIAN_PATH)
            
            self.schema = pickle.loads(database.get_metadata('schema'))
            self.content_field_name = pickle.loads(database.get_metadata('content'))
        
        return database
    
    def _term_generator(self, database, document):
        """
        Private method that returns a Xapian.TermGenerator
        
        Required Argument:
            `document` -- The document to be indexed
        
        Returns a Xapian.TermGenerator instance.  If `HAYSTACK_INCLUDE_SPELLING`
        is True, then the term generator will have spell-checking enabled.
        """
        term_generator = xapian.TermGenerator()
        term_generator.set_database(database)
        term_generator.set_stemmer(self.stemmer)
        if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
            term_generator.set_flags(xapian.TermGenerator.FLAG_SPELLING)
        term_generator.set_document(document)
        return term_generator
    
    def _query(self, database, query_string, narrow_queries=None, boost=None):
        """
        Private method that takes a query string and returns a xapian.Query.
        
        Required arguments:
            `query_string` -- The query string to parse
        
        Optional arguments:
            `narrow_queries` -- A list of queries to narrow the query with
            `boost` -- A dictionary of terms to boost with values
        
        Returns a xapian.Query instance with prefixes and ranges properly
        setup as pulled from the `query_string`.
        """
        spelling_suggestion = None
        
        if query_string == '*':
            query = xapian.Query('') # Make '*' match everything
        else:
            qp = self._query_parser(database)
            vrp = XHValueRangeProcessor(self)
            qp.add_valuerangeprocessor(vrp)
            query = qp.parse_query(query_string, self._flags(query_string))
            if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
                spelling_suggestion = qp.get_corrected_query_string()
        
        if narrow_queries:
            subqueries = [
                qp.parse_query(
                    narrow_query, self._flags(narrow_query)
                ) for narrow_query in narrow_queries
            ]
            query = xapian.Query(
                xapian.Query.OP_FILTER,
                query, xapian.Query(xapian.Query.OP_AND, subqueries)
            )
        if boost:
            subqueries = [
                xapian.Query(
                    xapian.Query.OP_SCALE_WEIGHT, xapian.Query(term), value
                ) for term, value in boost.iteritems()
            ]
            query = xapian.Query(
                xapian.Query.OP_OR, query,
                xapian.Query(xapian.Query.OP_AND, subqueries)
            )
        
        return query, spelling_suggestion
    
    def _flags(self, query_string):
        """
        Private method that returns an appropriate xapian.QueryParser flags
        set given a `query_string`.
        
        Required Arguments:
            `query_string` -- The query string to be parsed.
        
        Returns a xapian.QueryParser flag set (an integer)
        """
        flags = xapian.QueryParser.FLAG_PARTIAL \
              | xapian.QueryParser.FLAG_PHRASE \
              | xapian.QueryParser.FLAG_BOOLEAN \
              | xapian.QueryParser.FLAG_LOVEHATE
        if '*' in query_string:
            flags = flags | xapian.QueryParser.FLAG_WILDCARD
        if 'NOT' in query_string.upper():
            flags = flags | xapian.QueryParser.FLAG_PURE_NOT
        if getattr(settings, 'HAYSTACK_INCLUDE_SPELLING', False) is True:
            flags = flags | xapian.QueryParser.FLAG_SPELLING_CORRECTION
        return flags
    
    def _sorter(self, sort_by):
        """
        Private method that takes a list of fields to sort by and returns a
        xapian.MultiValueSorter
        
        Required Arguments:
            `sort_by` -- A list of fields to sort by
        
        Returns a xapian.MultiValueSorter instance
        """
        sorter = xapian.MultiValueSorter()
        
        for sort_field in sort_by:
            if sort_field.startswith('-'):
                reverse = True
                sort_field = sort_field[1:] # Strip the '-'
            else:
                reverse = False # Reverse is inverted in Xapian -- http://trac.xapian.org/ticket/311
            sorter.add(self._value_column(sort_field), reverse)
        
        return sorter
    
    def _query_parser(self, database):
        """
        Private method that returns a Xapian.QueryParser instance.
        
        Required arguments:
            `database` -- The database to be queried
        
        The query parser returned will have stemming enabled, a boolean prefix
        for `django_ct`, and prefixes for all of the fields in the `self.schema`.
        """
        qp = xapian.QueryParser()
        qp.set_database(database)
        qp.set_stemmer(self.stemmer)
        qp.set_stemming_strategy(xapian.QueryParser.STEM_SOME)
        qp.add_boolean_prefix('django_ct', DOCUMENT_CT_TERM_PREFIX)
        for field_dict in self.schema:
            qp.add_prefix(
                field_dict['field_name'],
                DOCUMENT_CUSTOM_TERM_PREFIX + field_dict['field_name'].upper()
            )
        return qp
    
    def _enquire(self, database, query):
        """
        Private method that that returns a Xapian.Enquire instance for use with
        the specifed `query`.
        
        Required Arguments:
            `query` -- The query to run
        
        Returns a xapian.Enquire instance
        """
        enquire = xapian.Enquire(database)
        enquire.set_query(query)
        enquire.set_docid_order(enquire.ASCENDING)
        
        return enquire
    
    def _value_column(self, field):
        """
        Private method that returns the column value slot in the database
        for a given field.
        
        Required arguemnts:
            `field` -- The field to lookup
        
        Returns an integer with the column location (0 indexed).
        """
        for field_dict in self.schema:
            if field_dict['field_name'] == field:
                return field_dict['column']
        return 0


class SearchQuery(BaseSearchQuery):
    """
    `SearchQuery` is responsible for converting search queries into a format
    that Xapian can understand.
    
    Most of the work is done by the :method:`build_query`.
    """
    def __init__(self, backend=None):
        """
        Create a new instance of the SearchQuery setting the backend as
        specified.  If no backend is set, will use the Xapian `SearchBackend`.
        
        Optional arguments:
            `backend` -- The `SearchBackend` to use (default = None)
        """
        super(SearchQuery, self).__init__(backend=backend)
        self.backend = backend or SearchBackend()
    
    def build_query(self):
        """
        Builds a search query from previously set values, returning a query
        string in a format ready for use by the Xapian `SearchBackend`.
        
        Returns:
            A query string suitable for parsing by Xapian.
        """
        query = ''
        
        if not self.query_filters:
            query = '*'
        else:
            query_chunks = []
            
            for the_filter in self.query_filters:
                if the_filter.is_and():
                    query_chunks.append('AND')

                if the_filter.is_or():
                    query_chunks.append('OR')

                if the_filter.is_not() and the_filter.field == 'content':
                    query_chunks.append('NOT')

                value = the_filter.value
                
                if not isinstance(value, (list, tuple)):
                    # Convert whatever we find to what xapian wants.
                    value = self.backend._marshal_value(value)
                
                # Check to see if it's a phrase for an exact match.
                if ' ' in value:
                    value = '"%s"' % value
                
                # 'content' is a special reserved word, much like 'pk' in
                # Django's ORM layer. It indicates 'no special field'.
                if the_filter.field == 'content':
                    query_chunks.append(value)
                else:
                    if the_filter.is_not():
                        query_chunks.append('AND')
                        filter_types = {
                            'exact': 'NOT %s:%s',
                            'gte': 'NOT %s:%s..*',
                            'gt': '%s:..%s',
                            'lte': 'NOT %s:..%s',
                            'lt': '%s:%s..*',
                            'startswith': 'NOT %s:%s*',
                        }
                    else:
                        filter_types = {
                            'exact': '%s:%s',
                            'gte': '%s:%s..*',
                            'gt': 'NOT %s:..%s',
                            'lte': '%s:..%s',
                            'lt': 'NOT %s:%s..*',
                            'startswith': '%s:%s*',
                        }

                    if the_filter.filter_type != 'in':
                        query_chunks.append(filter_types[the_filter.filter_type] % (the_filter.field, value))
                    else:
                        in_options = []
                        if the_filter.is_not():                        
                            for possible_value in value:
                                in_options.append('%s:%s' % (the_filter.field, possible_value))
                            query_chunks.append('NOT %s' % ' NOT '.join(in_options))
                        else:
                            for possible_value in value:
                                in_options.append('%s:%s' % (the_filter.field, possible_value))
                            query_chunks.append('(%s)' % ' OR '.join(in_options))
            
            if query_chunks[0] in ('AND', 'OR'):
                # Pull off an undesirable leading "AND" or "OR".
                del(query_chunks[0])
            
            query = ' '.join(query_chunks)
        
        if len(self.models):
            models = ['django_ct:%s.%s' % (model._meta.app_label, model._meta.module_name) for model in self.models]
            models_clause = ' '.join(models)
            final_query = '(%s) %s' % (query, models_clause)
        
        else:
            final_query = query
        
        return final_query
    
    def run(self):
        """
        Builds and executes the query. Returns a list of search results.
        """
        final_query = self.build_query()
        kwargs = {
            'start_offset': self.start_offset,
        }
        
        if self.order_by:
            kwargs['sort_by'] = self.order_by
        
        if self.end_offset is not None:
            kwargs['end_offset'] = self.end_offset - self.start_offset
        
        if self.highlight:
            kwargs['highlight'] = self.highlight
        
        if self.facets:
            kwargs['facets'] = list(self.facets)
        
        if self.date_facets:
            kwargs['date_facets'] = self.date_facets
        
        if self.query_facets:
            kwargs['query_facets'] = self.query_facets
        
        if self.narrow_queries:
            kwargs['narrow_queries'] = self.narrow_queries
        
        if self.boost:
            kwargs['boost'] = self.boost
        
        results = self.backend.search(final_query, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)
        self._facet_counts = results.get('facets', {})
        self._spelling_suggestion = results.get('spelling_suggestion', None)
    
    def run_mlt(self):
        """
        Builds and executes the query. Returns a list of search results.
        """
        if self._more_like_this is False or self._mlt_instance is None:
            raise MoreLikeThisError("No instance was provided to determine 'More Like This' results.")
        
        additional_query_string = self.build_query()
        kwargs = {
            'start_offset': self.start_offset,
        }
        
        if self.end_offset is not None:
            kwargs['end_offset'] = self.end_offset - self.start_offset
        
        results = self.backend.more_like_this(self._mlt_instance, additional_query_string, **kwargs)
        self._results = results.get('results', [])
        self._hit_count = results.get('hits', 0)
        
