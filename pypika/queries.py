from copy import copy
from functools import reduce

from pypika.enums import (
    JoinType,
    UnionType,
)
from pypika.terms import (
    ArithmeticExpression,
    EmptyCriterion,
    Field,
    Function,
    Index,
    Rollup,
    Star,
    Term,
    Tuple,
    ValueWrapper,
)
from pypika.utils import (
    JoinException,
    QueryException,
    RollupException,
    UnionException,
    builder,
    format_alias_sql,
    format_quotes,
    ignore_copy,
)

__author__ = "Timothy Heys"
__email__ = "theys@kayak.com"


class Selectable:
    def __init__(self, alias):
        self.alias = alias

    @builder
    def as_(self, alias):
        self.alias = alias

    def field(self, name):
        return Field(name, table=self)

    @property
    def star(self):
        return Star(self)

    @ignore_copy
    def __getattr__(self, name):
        return self.field(name)

    @ignore_copy
    def __getitem__(self, name):
        return self.field(name)


class AliasedQuery(Selectable):
    def __init__(self, name, query=None):
        super(AliasedQuery, self).__init__(alias=name)
        self.name = name
        self.query = query

    def get_sql(self, **kwargs):
        if self.query is None:
            return self.name
        return self.query.get_sql(**kwargs)

    def __eq__(self, other):
        return isinstance(other, AliasedQuery) \
               and self.name == other.name

    def __hash__(self):
        return hash(str(self.name))


class Schema:
    def __init__(self, name, parent=None):
        self._name = name
        self._parent = parent

    def __eq__(self, other):
        return isinstance(other, Schema) \
               and self._name == other._name \
               and self._parent == other._parent

    def __ne__(self, other):
        return not self.__eq__(other)

    @ignore_copy
    def __getattr__(self, item):
        return Table(item, schema=self)

    def get_sql(self, quote_char=None, **kwargs):
        # FIXME escape
        schema_sql = format_quotes(self._name, quote_char)

        if self._parent is not None:
            return '{parent}.{schema}' \
                .format(parent=self._parent.get_sql(quote_char=quote_char, **kwargs),
                        schema=schema_sql)

        return schema_sql


class Database(Schema):
    @ignore_copy
    def __getattr__(self, item):
        return Schema(item, parent=self)


class Table(Selectable):
    @staticmethod
    def _init_schema(schema):
        # This is a bit complicated in order to support backwards compatibility. It should probably be cleaned up for
        # the next major release. Schema is accepted as a string, list/tuple, Schema instance, or None
        if isinstance(schema, Schema):
            return schema
        if isinstance(schema, (list, tuple)):
            return reduce(lambda obj, s: Schema(s, parent=obj), schema[1:], Schema(schema[0]))
        if schema is not None:
            return Schema(schema)
        return None

    def __init__(self, name, schema=None, alias=None):
        super(Table, self).__init__(alias)
        self._table_name = name
        self._schema = self._init_schema(schema)

    def get_sql(self, **kwargs):
        quote_char = kwargs.get('quote_char')
        # FIXME escape
        table_sql = format_quotes(self._table_name, quote_char)

        if self._schema is not None:
            table_sql = '{schema}.{table}' \
                .format(schema=self._schema.get_sql(**kwargs),
                        table=table_sql)

        return format_alias_sql(table_sql, self.alias, **kwargs)

    def __str__(self):
        return self.get_sql(quote_char='"')

    def __eq__(self, other):
        if not isinstance(other, Table):
            return False

        if self._table_name != other._table_name:
            return False

        if self._schema != other._schema:
            return False

        if self.alias != other.alias:
            return False

        return True

    def __repr__(self):
        if self._schema:
            return "Table('{}', schema='{}')".format(self._table_name, self._schema)
        return "Table('{}')".format(self._table_name)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(str(self))

    def select(self, *terms):
        """
        Perform a SELECT operation on the current table

        :param terms:
            Type:  list[expression]

            A list of terms to select. These can be any type of int, float, str, bool or Term or a Field.

        :return:  QueryBuilder
        """
        return Query.from_(self).select(*terms)

    def update(self):
        """
        Perform an UPDATE operation on the current table

        :return: QueryBuilder
        """
        return Query.update(self)

    def insert(self, *terms):
        """
        Perform an INSERT operation on the current table

        :param terms:
            Type: list[expression]

            A list of terms to select. These can be any type of int, float, str, bool or  any other valid data

        :return: QueryBuilder
        """
        return Query.into(self).insert(*terms)


def make_tables(*names, **kwargs):
    """
    Shortcut to create many tables. If `names` param is a tuple, the first
    position will refer to the `_table_name` while the second will be its `alias`.
    Any other data structure will be treated as a whole as the `_table_name`.
    """
    tables = []
    for name in names:
        if isinstance(name, tuple) and len(name) == 2:
            t = Table(name=name[0], alias=name[1], schema=kwargs.get('schema'))
        else:
            t = Table(name=name, schema=kwargs.get('schema'))
        tables.append(t)
    return tables


class Column:
    def __init__(self, column_name, column_type=None):
        self.name = column_name
        self.type = column_type

    def get_sql(self, **kwargs):
        quote_char = kwargs.get('quote_char')

        column_sql = '{name}{type}'.format(
              name=format_quotes(self.name, quote_char),
              type=' {}'.format(self.type) if self.type else '',
        )

        return column_sql

    def __str__(self):
        return self.get_sql(quote_char='"')


def make_columns(*names):
    """
    Shortcut to create many columns. If `names` param is a tuple, the first
    position will refer to the `name` while the second will be its `type`.
    Any other data structure will be treated as a whole as the `name`.
    """
    columns = []
    for name in names:
        if isinstance(name, tuple) and len(name) == 2:
            column = Column(column_name=name[0], column_type=name[1])
        else:
            column = Column(column_name=name)
        columns.append(column)

    return columns


class Query:
    """
    Query is the primary class and entry point in pypika. It is used to build queries iteratively using the builder
    design
    pattern.

    This class is immutable.
    """

    @classmethod
    def _builder(cls):
        return QueryBuilder()

    @classmethod
    def from_(cls, table):
        """
        Query builder entry point.  Initializes query building and sets the table to select from.  When using this
        function, the query becomes a SELECT query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder().from_(table)

    @classmethod
    def create_table(cls, table):
        """
        Query builder entry point. Initializes query building and sets the table name to be created. When using this
        function, the query becomes a CREATE statement.

        :param table: An instance of a Table object or a string table name.

        :return: CreateQueryBuilder
        """
        return CreateQueryBuilder().create_table(table)

    @classmethod
    def into(cls, table):
        """
        Query builder entry point.  Initializes query building and sets the table to insert into.  When using this
        function, the query becomes an INSERT query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder().into(table)

    @classmethod
    def with_(cls, table, name):
        return cls._builder().with_(table, name)

    @classmethod
    def select(cls, *terms):
        """
        Query builder entry point.  Initializes query building without a table and selects fields.  Useful when testing
        SQL functions.

        :param terms:
            Type: list[expression]

            A list of terms to select.  These can be any type of int, float, str, bool, or Term.  They cannot be a Field
            unless the function ``Query.from_`` is called first.

        :returns QueryBuilder
        """
        return cls._builder().select(*terms)

    @classmethod
    def update(cls, table):
        """
        Query builder entry point.  Initializes query building and sets the table to update.  When using this
        function, the query becomes an UPDATE query.

        :param table:
            Type: Table or str

            An instance of a Table object or a string table name.

        :returns QueryBuilder
        """
        return cls._builder().update(table)


class _UnionQuery(Selectable, Term):
    """
    A Query class wrapper for a Union query, whether DISTINCT or ALL.

    Created via the functionds `Query.union` or `Query.union_all`, this class should not be instantiated directly.
    """

    def __init__(self, base_query, union_query, union_type, alias=None, wrapper_cls=ValueWrapper):
        super(_UnionQuery, self).__init__(alias)
        self.base_query = base_query
        self._unions = [(union_type, union_query)]
        self._orderbys = []

        self._limit = None
        self._offset = None

        self._wrapper_cls = wrapper_cls

    @builder
    def orderby(self, *fields, **kwargs):
        for field in fields:
            field = (Field(field, table=self.base_query._from[0])
                     if isinstance(field, str)
                     else self.base_query.wrap_constant(field))

            self._orderbys.append((field, kwargs.get('order')))

    @builder
    def limit(self, limit):
        self._limit = limit

    @builder
    def offset(self, offset):
        self._offset = offset

    @builder
    def union(self, other):
        self._unions.append((UnionType.distinct, other))

    @builder
    def union_all(self, other):
        self._unions.append((UnionType.all, other))

    def __add__(self, other):
        return self.union(other)

    def __mul__(self, other):
        return self.union_all(other)

    def __str__(self):
        return self.get_sql()

    def get_sql(self, with_alias=False, subquery=False, **kwargs):
        union_template = ' UNION{type} {union}'

        kwargs.setdefault('dialect', self.base_query.dialect)
        # This initializes the quote char based on the base query, which could be a dialect specific query class
        # This might be overridden if quote_char is set explicitly in kwargs
        kwargs.setdefault('quote_char', self.base_query.QUOTE_CHAR)

        base_querystring = self.base_query.get_sql(subquery=self.base_query.wrap_union_queries, **kwargs)

        querystring = base_querystring
        for union_type, union_query in self._unions:
            union_querystring = union_query.get_sql(subquery=self.base_query.wrap_union_queries, **kwargs)

            if len(self.base_query._selects) != len(union_query._selects):
                raise UnionException("Queries must have an equal number of select statements in a union."
                                     "\n\nMain Query:\n{query1}\n\nUnion Query:\n{query2}"
                                     .format(query1=base_querystring, query2=union_querystring))

            querystring += union_template.format(type=union_type.value,
                                                 union=union_querystring)

        if self._orderbys:
            querystring += self._orderby_sql(**kwargs)

        if self._limit:
            querystring += self._limit_sql()

        if self._offset:
            querystring += self._offset_sql()

        if subquery:
            querystring = '({query})'.format(query=querystring, **kwargs)

        if with_alias:
            return format_alias_sql(querystring, self.alias or self._table_name, **kwargs)

        return querystring

    def _orderby_sql(self, quote_char=None, **kwargs):
        """
        Produces the ORDER BY part of the query.  This is a list of fields and possibly their directionality, ASC or
        DESC. The clauses are stored in the query under self._orderbys as a list of tuples containing the field and
        directionality (which can be None).

        If an order by field is used in the select clause, determined by a matching , then the ORDER BY clause will use
        the alias, otherwise the field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self.base_query._selects}
        for field, directionality in self._orderbys:
            term = format_quotes(field.alias, quote_char) \
                if field.alias and field.alias in selected_aliases \
                else field.get_sql(quote_char=quote_char, **kwargs)

            clauses.append('{term} {orient}'.format(term=term, orient=directionality.value)
                           if directionality is not None else term)

        return ' ORDER BY {orderby}'.format(orderby=','.join(clauses))

    def _offset_sql(self):
        return " OFFSET {offset}".format(offset=self._offset)

    def _limit_sql(self):
        return " LIMIT {limit}".format(limit=self._limit)


class QueryBuilder(Selectable, Term):
    """
    Query Builder is the main class in pypika which stores the state of a query and offers functions which allow the
    state to be branched immutably.
    """
    QUOTE_CHAR = '"'
    SECONDARY_QUOTE_CHAR = "'"
    ALIAS_QUOTE_CHAR = None

    def __init__(self,
                 dialect=None,
                 wrap_union_queries=True,
                 wrapper_cls=ValueWrapper):
        super(QueryBuilder, self).__init__(None)

        self._from = []
        self._insert_table = None
        self._update_table = None
        self._delete_from = False
        self._replace = False

        self._with = []
        self._selects = []
        self._force_indexes = []
        self._columns = []
        self._values = []
        self._distinct = False
        self._ignore = False

        self._wheres = None
        self._prewheres = None
        self._groupbys = []
        self._with_totals = False
        self._havings = None
        self._orderbys = []
        self._joins = []
        self._unions = []

        self._limit = None
        self._offset = None

        self._updates = []

        self._select_star = False
        self._select_star_tables = set()
        self._mysql_rollup = False
        self._select_into = False

        self._subquery_count = 0
        self._foreign_table = False

        self.dialect = dialect
        self.wrap_union_queries = wrap_union_queries

        self._wrapper_cls = wrapper_cls

    def __copy__(self):
        newone = type(self).__new__(type(self))
        newone.__dict__.update(self.__dict__)
        newone._select_star_tables = copy(self._select_star_tables)
        newone._from = copy(self._from)
        newone._with = copy(self._with)
        newone._selects = copy(self._selects)
        newone._columns = copy(self._columns)
        newone._values = copy(self._values)
        newone._groupbys = copy(self._groupbys)
        newone._orderbys = copy(self._orderbys)
        newone._joins = copy(self._joins)
        newone._unions = copy(self._unions)
        newone._updates = copy(self._updates)
        return newone

    @builder
    def from_(self, selectable):
        """
        Adds a table to the query. This function can only be called once and will raise an AttributeError if called a
        second time.

        :param selectable:
            Type: ``Table``, ``Query``, or ``str``

            When a ``str`` is passed, a table with the name matching the ``str`` value is used.

        :returns
            A copy of the query with the table added.
        """

        self._from.append(Table(selectable) if isinstance(selectable, str) else selectable)

        if isinstance(selectable, (QueryBuilder, _UnionQuery)) and selectable.alias is None:
            if isinstance(selectable, QueryBuilder):
                sub_query_count = selectable._subquery_count
            else:
                sub_query_count = 0

            sub_query_count = max(self._subquery_count, sub_query_count)
            selectable.alias = 'sq%d' % sub_query_count
            self._subquery_count = sub_query_count + 1

    @builder
    def replace_table(self, current_table, new_table):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing fields across
        queries.

        :param current_table:
            The table instance to be replaces.
        :param new_table:
            The table instance to replace with.
        :return:
            A copy of the query with the tables replaced.
        """
        self._from = [new_table if table == current_table else table for table in self._from]
        self._insert_table = new_table if self._insert_table else None
        self._update_table = new_table if self._update_table else None

        self._with = [alias_query.replace_table(current_table, new_table) for alias_query in self._with]
        self._selects = [select.replace_table(current_table, new_table) for select in self._selects]
        self._columns = [column.replace_table(current_table, new_table) for column in self._columns]
        self._values = [[value.replace_table(current_table, new_table) for value in value_list]
                        for value_list
                        in self._values]

        self._wheres = self._wheres.replace_table(current_table, new_table) if self._wheres else None
        self._prewheres = self._prewheres.replace_table(current_table, new_table) if self._prewheres else None
        self._groupbys = [groupby.replace_table(current_table, new_table) for groupby in self._groupbys]
        self._havings = self._havings.replace_table(current_table, new_table) if self._havings else None
        self._orderbys = [(orderby[0].replace_table(current_table, new_table), orderby[1])
                          for orderby in self._orderbys]
        self._joins = [join.replace_table(current_table, new_table) for join in self._joins]

        if current_table in self._select_star_tables:
            self._select_star_tables.remove(current_table)
            self._select_star_tables.add(new_table)

    @builder
    def with_(self, selectable, name):
        t = AliasedQuery(name, selectable)
        self._with.append(t)

    @builder
    def into(self, table):
        if self._insert_table is not None:
            raise AttributeError("'Query' object has no attribute '%s'" % 'into')

        if self._selects:
            self._select_into = True

        self._insert_table = table if isinstance(table, Table) else Table(table)

    @builder
    def select(self, *terms):
        for term in terms:
            if isinstance(term, Field):
                self._select_field(term)
            elif isinstance(term, str):
                self._select_field_str(term)
            elif isinstance(term, (Function, ArithmeticExpression)):
                self._select_other(term)
            else:
                self._select_other(self.wrap_constant(term, wrapper_cls=self._wrapper_cls))

    @builder
    def delete(self):
        if self._delete_from or self._selects or self._update_table:
            raise AttributeError("'Query' object has no attribute '%s'" % 'delete')

        self._delete_from = True

    @builder
    def update(self, table):
        if self._update_table is not None or self._selects or self._delete_from:
            raise AttributeError("'Query' object has no attribute '%s'" % 'update')

        self._update_table = table if isinstance(table, Table) else Table(table)

    @builder
    def columns(self, *terms):
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute '%s'" % 'insert')

        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._insert_table)
            self._columns.append(term)

    @builder
    def insert(self, *terms):
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute '%s'" % 'insert')

        if not terms:
            return
        else:
            self._validate_terms_and_append(*terms)
        self._replace = False

    @builder
    def replace(self, *terms):
        if self._insert_table is None:
            raise AttributeError("'Query' object has no attribute '%s'" % 'insert')

        if not terms:
            return
        else:
            self._validate_terms_and_append(*terms)
        self._replace = True

    @builder
    def force_index(self, term, *terms):
        for t in (term, *terms):
            if isinstance(t, Index):
                self._force_indexes.append(t)
            elif isinstance(t, str):
                self._force_indexes.append(Index(t))

    @builder
    def distinct(self):
        self._distinct = True

    @builder
    def ignore(self):
        self._ignore = True

    @builder
    def prewhere(self, criterion):
        if not self._validate_table(criterion):
            self._foreign_table = True

        if self._prewheres:
            self._prewheres &= criterion
        else:
            self._prewheres = criterion

    @builder
    def where(self, criterion):
        if isinstance(criterion, EmptyCriterion):
            return

        if not self._validate_table(criterion):
            self._foreign_table = True

        if self._wheres:
            self._wheres &= criterion
        else:
            self._wheres = criterion

    @builder
    def having(self, criterion):
        if self._havings:
            self._havings &= criterion
        else:
            self._havings = criterion

    @builder
    def groupby(self, *terms):
        for term in terms:
            if isinstance(term, str):
                term = Field(term, table=self._from[0])
            elif isinstance(term, int):
                term = Field(str(term), table=self._from[0]).wrap_constant(term)

            self._groupbys.append(term)

    @builder
    def with_totals(self):
        self._with_totals = True

    @builder
    def rollup(self, *terms, **kwargs):
        for_mysql = 'mysql' == kwargs.get('vendor')

        if self._mysql_rollup:
            raise AttributeError("'Query' object has no attribute '%s'" % 'rollup')

        terms = [Tuple(*term) if isinstance(term, (list, tuple, set))
                 else term
                 for term in terms]

        if for_mysql:
            # MySQL rolls up all of the dimensions always
            if not terms and not self._groupbys:
                raise RollupException('At least one group is required. Call Query.groupby(term) or pass'
                                      'as parameter to rollup.')

            self._mysql_rollup = True
            self._groupbys += terms

        elif 0 < len(self._groupbys) and isinstance(self._groupbys[-1], Rollup):
            # If a rollup was added last, then append the new terms to the previous rollup
            self._groupbys[-1].args += terms

        else:
            self._groupbys.append(Rollup(*terms))

    @builder
    def orderby(self, *fields, **kwargs):
        for field in fields:
            field = (Field(field, table=self._from[0])
                     if isinstance(field, str)
                     else self.wrap_constant(field))

            self._orderbys.append((field, kwargs.get('order')))

    @builder
    def join(self, item, how=JoinType.inner):
        if isinstance(item, Table):
            return Joiner(self, item, how, type_label='table')

        elif isinstance(item, QueryBuilder):
            return Joiner(self, item, how, type_label='subquery')

        elif isinstance(item, AliasedQuery):
            return Joiner(self, item, how, type_label='table')

        raise ValueError("Cannot join on type '%s'" % type(item))

    @builder
    def limit(self, limit):
        self._limit = limit

    @builder
    def offset(self, offset):
        self._offset = offset

    @builder
    def union(self, other):
        return _UnionQuery(self, other, UnionType.distinct, wrapper_cls=self._wrapper_cls)

    @builder
    def union_all(self, other):
        return _UnionQuery(self, other, UnionType.all, wrapper_cls=self._wrapper_cls)

    @builder
    def set(self, field, value):
        field = Field(field) if not isinstance(field, Field) else field
        self._updates.append((field, ValueWrapper(value)))

    def __add__(self, other):
        return self.union(other)

    def __mul__(self, other):
        return self.union_all(other)

    @builder
    def __getitem__(self, item):
        if not isinstance(item, slice):
            raise TypeError("Query' object is not subscriptable")
        self._offset = item.start
        self._limit = item.stop

    @staticmethod
    def _list_aliases(field_set, quote_char=None):
        return [field.alias or field.get_sql(quote_char=quote_char)
                for field in field_set]

    def _select_field_str(self, term):
        if 0 == len(self._from):
            raise QueryException('Cannot select {term}, no FROM table specified.'
                                 .format(term=term))

        if term == '*':
            self._select_star = True
            self._selects = [Star()]
            return

        self._select_field(Field(term, table=self._from[0]))

    def _select_field(self, term):
        if self._select_star:
            # Do not add select terms after a star is selected
            return

        if term.table in self._select_star_tables:
            # Do not add select terms for table after a table star is selected
            return

        if isinstance(term, Star):
            self._selects = [select
                             for select in self._selects
                             if not hasattr(select, 'table') or term.table != select.table]
            self._select_star_tables.add(term.table)

        self._selects.append(term)

    def _select_other(self, function):
        self._selects.append(function)

    def fields(self):
        # Don't return anything here. Subqueries have their own fields.
        return []

    def do_join(self, join):
        base_tables = self._from + [self._update_table] + self._with

        join.validate(base_tables, self._joins)

        if isinstance(join.item, QueryBuilder) and join.item.alias is None:
            self._tag_subquery(join.item)

        table_in_query = any(isinstance(clause, Table)
                             and join.item in base_tables
                             for clause in base_tables)
        if isinstance(join.item, Table) and join.item.alias is None and table_in_query:
            # On the odd chance that we join the same table as the FROM table and don't set an alias
            # FIXME only works once
            join.item.alias = join.item._table_name + '2'

        self._joins.append(join)

    def is_joined(self, table):
        return any(table == join.item for join in self._joins)

    def _validate_table(self, term):
        """
        Returns False if the term references a table not already part of the
        FROM clause or JOINS and True otherwise.
        """
        base_tables = self._from + [self._update_table]

        for field in term.fields():
            table_in_base_tables = field.table in base_tables
            table_in_joins = field.table in [join.item for join in self._joins]
            if field.table is not None \
                  and not table_in_base_tables \
                  and not table_in_joins \
                  and field.table != self._update_table:
                return False
        return True

    def _tag_subquery(self, subquery):
        subquery.alias = 'sq%d' % self._subquery_count
        self._subquery_count += 1

    def _validate_terms_and_append(self, *terms):
        """
        Handy function for INSERT and REPLACE statements in order to check if
        terms are introduced and how append them to `self._values`
        """
        if not isinstance(terms[0], (list, tuple, set)):
            terms = [terms]

        for values in terms:
            self._values.append([value
                                 if isinstance(value, Term)
                                 else self.wrap_constant(value)
                                 for value in values])

    def __str__(self):
        return self.get_sql(dialect=self.dialect)

    def __repr__(self):
        return self.__str__()

    def __eq__(self, other):
        if not isinstance(other, QueryBuilder):
            return False

        if not self.alias == other.alias:
            return False

        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.alias) + sum(hash(clause) for clause in self._from)

    def _set_kwargs_defaults(self, kwargs):
        kwargs.setdefault('quote_char', self.QUOTE_CHAR)
        kwargs.setdefault('secondary_quote_char', self.SECONDARY_QUOTE_CHAR)
        kwargs.setdefault('alias_quote_char', self.ALIAS_QUOTE_CHAR)
        kwargs.setdefault('dialect', self.dialect)

    def get_sql(self, with_alias=False, subquery=False, **kwargs):
        self._set_kwargs_defaults(kwargs)

        if not (self._selects or self._insert_table or self._delete_from or self._update_table):
            return ''
        if self._insert_table and not (self._selects or self._values):
            return ''
        if self._update_table and not self._updates:
            return ''

        has_joins = bool(self._joins)
        has_multiple_from_clauses = 1 < len(self._from)
        has_subquery_from_clause = 0 < len(self._from) and isinstance(self._from[0], QueryBuilder)
        has_reference_to_foreign_table = self._foreign_table

        kwargs['with_namespace'] = any([
            has_joins,
            has_multiple_from_clauses,
            has_subquery_from_clause,
            has_reference_to_foreign_table
        ])

        if self._update_table:
            querystring = self._update_sql(**kwargs)

            if self._joins:
                querystring += " " + " ".join(join.get_sql(**kwargs)
                                              for join in self._joins)

            querystring += self._set_sql(**kwargs)

            if self._wheres:
                querystring += self._where_sql(**kwargs)

            if self._limit:
                querystring += self._limit_sql()

            return querystring

        if self._delete_from:
            querystring = self._delete_sql(**kwargs)

        elif not self._select_into and self._insert_table:
            if self._replace:
                querystring = self._replace_sql(**kwargs)
            else:
                querystring = self._insert_sql(**kwargs)

            if self._columns:
                querystring += self._columns_sql(**kwargs)

            if self._values:
                querystring += self._values_sql(**kwargs)
                return querystring
            else:
                querystring += ' ' + self._select_sql(**kwargs)

        else:
            if self._with:
                querystring = self._with_sql(**kwargs)
            else:
                querystring = ''

            querystring += self._select_sql(**kwargs)

            if self._insert_table:
                querystring += self._into_sql(**kwargs)

        if self._from:
            querystring += self._from_sql(**kwargs)

        if self._force_indexes:
            querystring += self._force_index_sql(**kwargs)

        if self._joins:
            querystring += " " + " ".join(join.get_sql(**kwargs)
                                          for join in self._joins)

        if self._prewheres:
            querystring += self._prewhere_sql(**kwargs)

        if self._wheres:
            querystring += self._where_sql(**kwargs)

        if self._groupbys:
            querystring += self._group_sql(**kwargs)
            if self._mysql_rollup:
                querystring += self._rollup_sql()

        if self._havings:
            querystring += self._having_sql(**kwargs)

        if self._orderbys:
            querystring += self._orderby_sql(**kwargs)

        if self._limit:
            querystring += self._limit_sql()

        if self._offset:
            querystring += self._offset_sql()

        if subquery:
            querystring = '({query})'.format(query=querystring)

        if with_alias:
            return format_alias_sql(querystring, self.alias, **kwargs)

        return querystring

    def _with_sql(self, **kwargs):
        return 'WITH ' + ','.join(
              clause.name + ' AS (' + clause.get_sql(
                    subquery=False,
                    with_alias=False,
                    **kwargs) +
              ') '
              for clause in self._with)

    def _select_sql(self, **kwargs):
        return 'SELECT {distinct}{select}'.format(
              distinct='DISTINCT ' if self._distinct else '',
              select=','.join(term.get_sql(with_alias=True, subquery=True, **kwargs)
                              for term in self._selects),
        )

    def _insert_sql(self, **kwargs):
        return 'INSERT {ignore}INTO {table}'.format(
              table=self._insert_table.get_sql(**kwargs),
              ignore='IGNORE ' if self._ignore else ''
        )

    def _replace_sql(self, **kwargs):
        return 'REPLACE INTO {table}'.format(
              table=self._insert_table.get_sql(**kwargs),
        )

    @staticmethod
    def _delete_sql(**kwargs):
        return 'DELETE'

    def _update_sql(self, **kwargs):
        return 'UPDATE {table}'.format(
              table=self._update_table.get_sql(**kwargs)
        )

    def _columns_sql(self, with_namespace=False, **kwargs):
        """
        SQL for Columns clause for INSERT queries
        :param with_namespace:
            Remove from kwargs, never format the column terms with namespaces since only one table can be inserted into
        """
        return ' ({columns})'.format(
              columns=','.join(term.get_sql(with_namespace=False, **kwargs)
                               for term in self._columns)
        )

    def _values_sql(self, **kwargs):
        return ' VALUES ({values})' \
            .format(values='),('
                    .join(','
                          .join(term.get_sql(with_alias=True, subquery=True, **kwargs)
                                for term in row)
                          for row in self._values))

    def _into_sql(self, **kwargs):
        return ' INTO {table}'.format(
              table=self._insert_table.get_sql(with_alias=False, **kwargs),
        )

    def _from_sql(self, with_namespace=False, **kwargs):
        return ' FROM {selectable}'.format(selectable=','.join(
              clause.get_sql(subquery=True, with_alias=True, **kwargs)
              for clause in self._from
        ))

    def _force_index_sql(self, **kwargs):
        return ' FORCE INDEX ({indexes})'.format(indexes=','.join(
            index.get_sql(**kwargs)
            for index in self._force_indexes),
        )

    def _prewhere_sql(self, quote_char=None, **kwargs):
        return ' PREWHERE {prewhere}'.format(
              prewhere=self._prewheres.get_sql(quote_char=quote_char, subquery=True, **kwargs))

    def _where_sql(self, quote_char=None, **kwargs):
        return ' WHERE {where}'.format(where=self._wheres.get_sql(quote_char=quote_char, subquery=True, **kwargs))

    def _group_sql(self, quote_char=None, alias_quote_char=None, groupby_alias=True, **kwargs):
        """
        Produces the GROUP BY part of the query.  This is a list of fields. The clauses are stored in the query under
        self._groupbys as a list fields.

        If an groupby field is used in the select clause,
        determined by a matching alias, and the groupby_alias is set True
        then the GROUP BY clause will use the alias,
        otherwise the entire field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self._selects}
        for field in self._groupbys:
            if groupby_alias and field.alias and field.alias in selected_aliases:
                clauses.append(format_quotes(field.alias, alias_quote_char or quote_char))
            else:
                clauses.append(field.get_sql(quote_char=quote_char, alias_quote_char=alias_quote_char, **kwargs))

        sql = ' GROUP BY {groupby}'.format(groupby=','.join(clauses))

        if self._with_totals:
            return sql + ' WITH TOTALS'

        return sql

    def _orderby_sql(self, quote_char=None, alias_quote_char=None, orderby_alias=True, **kwargs):
        """
        Produces the ORDER BY part of the query.  This is a list of fields and possibly their directionality, ASC or
        DESC. The clauses are stored in the query under self._orderbys as a list of tuples containing the field and
        directionality (which can be None).

        If an order by field is used in the select clause,
        determined by a matching, and the orderby_alias
        is set True then the ORDER BY clause will use
        the alias, otherwise the field will be rendered as SQL.
        """
        clauses = []
        selected_aliases = {s.alias for s in self._selects}
        for field, directionality in self._orderbys:
            term = format_quotes(field.alias, alias_quote_char or quote_char) \
                if orderby_alias and field.alias and field.alias in selected_aliases \
                else field.get_sql(quote_char=quote_char, alias_quote_char=alias_quote_char, **kwargs)

            clauses.append('{term} {orient}'.format(term=term, orient=directionality.value)
                           if directionality is not None else term)

        return ' ORDER BY {orderby}'.format(orderby=','.join(clauses))

    def _rollup_sql(self):
        return ' WITH ROLLUP'

    def _having_sql(self, quote_char=None, **kwargs):
        return ' HAVING {having}'.format(having=self._havings.get_sql(quote_char=quote_char, **kwargs))

    def _offset_sql(self):
        return " OFFSET {offset}".format(offset=self._offset)

    def _limit_sql(self):
        return " LIMIT {limit}".format(limit=self._limit)

    def _set_sql(self, **kwargs):
        return ' SET {set}'.format(
              set=','.join(
                    '{field}={value}'.format(
                          field=field.get_sql(**kwargs),
                          value=value.get_sql(**kwargs)) for field, value in self._updates
              )
        )


class Joiner:
    def __init__(self, query, item, how, type_label):
        self.query = query
        self.item = item
        self.how = how
        self.type_label = type_label

    def on(self, criterion, collate=None):
        if criterion is None:
            raise JoinException("Parameter 'criterion' is required for a "
                                "{type} JOIN but was not supplied.".format(type=self.type_label))

        self.query.do_join(JoinOn(self.item, self.how, criterion, collate))
        return self.query

    def on_field(self, *fields):
        if not fields:
            raise JoinException("Parameter 'fields' is required for a "
                                "{type} JOIN but was not supplied.".format(type=self.type_label))

        criterion = None
        for field in fields:
            consituent = Field(field, table=self.query._from[0]) == Field(field, table=self.item)
            criterion = consituent if criterion is None else criterion & consituent

        self.query.do_join(JoinOn(self.item, self.how, criterion))
        return self.query

    def using(self, *fields):
        if not fields:
            raise JoinException("Parameter 'fields' is required when joining with "
                                "a using clause but was not supplied.".format(type=self.type_label))

        self.query.do_join(JoinUsing(self.item, self.how, [Field(field) for field in fields]))
        return self.query

    def cross(self):
        """Return cross join"""
        self.query.do_join(Join(self.item, JoinType.cross))

        return self.query


class Join:
    def __init__(self, item, how):
        self.item = item
        self.how = how

    def get_sql(self, **kwargs):
        sql = 'JOIN {table}'.format(
              table=self.item.get_sql(subquery=True, with_alias=True, **kwargs),
        )

        if self.how.value:
            return '{type} {join}'.format(join=sql, type=self.how.value)
        return sql

    def validate(self, _from, _joins):
        pass

    @builder
    def replace_table(self, current_table, new_table):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        self.item = self.item.replace_table(current_table, new_table)


class JoinOn(Join):
    def __init__(self, item, how, criteria, collate=None):
        super(JoinOn, self).__init__(item, how)
        self.criterion = criteria
        self.collate = collate

    def get_sql(self, **kwargs):
        join_sql = super(JoinOn, self).get_sql(**kwargs)
        return '{join} ON {criterion}{collate}'.format(
              join=join_sql,
              criterion=self.criterion.get_sql(subquery=True, **kwargs),
              collate=" COLLATE {}".format(self.collate) if self.collate else ""
        )

    def validate(self, _from, _joins):
        criterion_tables = set([f.table for f in self.criterion.fields()])
        available_tables = (set(_from) | {join.item for join in _joins} | {self.item})
        missing_tables = criterion_tables - available_tables
        if missing_tables:
            raise JoinException('Invalid join criterion. One field is required from the joined item and '
                                'another from the selected table or an existing join.  Found [{tables}]'.format(
                  tables=', '.join(map(str, missing_tables))
            ))

    @builder
    def replace_table(self, current_table, new_table):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        self.item = new_table if self.item == current_table else self.item
        self.criterion = self.criterion.replace_table(current_table, new_table)


class JoinUsing(Join):
    def __init__(self, item, how, fields):
        super(JoinUsing, self).__init__(item, how)
        self.fields = fields

    def get_sql(self, **kwargs):
        join_sql = super(JoinUsing, self).get_sql(**kwargs)
        return '{join} USING ({fields})'.format(
              join=join_sql,
              fields=','.join(str(field) for field in self.fields)
        )

    def validate(self, _from, _joins):
        pass

    @builder
    def replace_table(self, current_table, new_table):
        """
        Replaces all occurrences of the specified table with the new table. Useful when reusing
        fields across queries.

        :param current_table:
            The table to be replaced.
        :param new_table:
            The table to replace with.
        :return:
            A copy of the join with the tables replaced.
        """
        self.item = new_table if self.item == current_table else self.item
        self.fields = [field.replace_table(current_table, new_table) for field in self.fields]


class CreateQueryBuilder:
    """
    Query builder used to build CREATE queries.
    """
    QUOTE_CHAR = '"'
    SECONDARY_QUOTE_CHAR = "'"
    ALIAS_QUOTE_CHAR = None

    def __init__(self, dialect=None):
        self._create_table = None
        self._temporary = False
        self._as_select = None
        self._columns = []
        self.dialect = dialect

    def _set_kwargs_defaults(self, kwargs):
        kwargs.setdefault('quote_char', self.QUOTE_CHAR)
        kwargs.setdefault('secondary_quote_char', self.SECONDARY_QUOTE_CHAR)
        kwargs.setdefault('dialect', self.dialect)

    def get_sql(self, **kwargs):
        self._set_kwargs_defaults(kwargs)

        if not self._create_table:
            return ''

        if not self._columns and not self._as_select:
            return ''

        querystring = self._create_table_sql(**kwargs)

        if self._columns:
            querystring += self._columns_sql(**kwargs)
        else:
            querystring += self._as_select_sql(**kwargs)

        return querystring

    @builder
    def create_table(self, table):
        if self._create_table:
            raise AttributeError("'Query' object already has attribute create_table")

        self._create_table = table if isinstance(table, Table) else Table(table)

    @builder
    def temporary(self):
        self._temporary = True

    @builder
    def columns(self, *columns):
        if self._as_select:
            raise AttributeError("'Query' object already has attribute as_select")

        for column in columns:
            if isinstance(column, str):
                column = Column(column)
            elif isinstance(column, tuple):
                column = Column(column_name=column[0], column_type=column[1])
            self._columns.append(column)

    @builder
    def as_select(self, query_builder):
        if self._columns:
            raise AttributeError("'Query' object already has attribute columns")

        if not isinstance(query_builder, QueryBuilder):
            raise TypeError("Expected 'item' to be instance of QueryBuilder")

        self._as_select = query_builder

    def _create_table_sql(self, **kwargs):
        return 'CREATE {temporary}TABLE {table}'.format(
              temporary='TEMPORARY ' if self._temporary else '',
              table=self._create_table.get_sql(**kwargs),
        )

    def _columns_sql(self, **kwargs):
        return ' ({columns})'.format(
              columns=','.join(column.get_sql(**kwargs)
                               for column in self._columns)
        )

    def _as_select_sql(self, **kwargs):
        return ' AS ({query})'.format(
              query=self._as_select.get_sql(**kwargs),
        )

    def __str__(self):
        return self.get_sql()

    def __repr__(self):
        return self.__str__()
