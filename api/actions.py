import itertools
import json
import re
import traceback
from datetime import datetime

import psycopg2
import sqlalchemy as sqla
from django.core.exceptions import PermissionDenied
from django.http import Http404
from sqlalchemy import func, MetaData, Table
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.session import sessionmaker

import geoalchemy2  # Although this import seems unused is has to be here

import api
from api import references
from api.parser import quote, read_pgid, read_bool, get_or_403
from api.error import APIError
from shapely import wkb, wkt
from sqlalchemy.sql import column
from api.connection import _get_engine
from sqlalchemy import exc
pgsql_qualifier = re.compile(r"^[\w\d_\.]+$")

import logging

logger = logging.getLogger('oeplatform')

def get_table_name(schema, table, restrict_schemas=True):
    if not has_schema(dict(schema=schema)):
        raise Http404
    if not has_table(dict(schema=schema, table=table)):
        raise Http404
    if schema.startswith('_') or schema == 'public' or schema is None:
        raise PermissionDenied
    if restrict_schemas:
        if schema not in ['model_draft', 'sandbox', 'test']:
            raise PermissionDenied
    return schema, table

__CONNECTIONS = {}
__CURSORS = {}


Base = declarative_base()


class ResponsiveException(Exception):
    pass


def load_cursor(f):
    def wrapper(*args, **kwargs):
        fetch_all = 'cursor_id' not in args[1].data
        if fetch_all:
            engine = _get_engine()
            connection = engine.connect()
            cursor = connection.connection.cursor()
            cursor_id = cursor.__hash__()
            __CURSORS[cursor_id] = cursor

            # django_restframework passes different data dictionaries depending
            # on the request type: PUT -> Mutable, POST -> Immutable
            # Thus, we have to replace the data dictionary by one we can mutate.
            args[1]._full_data = dict(args[1].data)
            args[1].data['cursor_id'] = cursor_id
        try:
            result = f(*args, **kwargs)

            if fetch_all:
                if not result:
                    result = {}
                if cursor.description:
                    result['description'] = cursor.description
                    result['data'] = [list(map(__translate_fetched_cell, row)) for row in cursor.fetchall()]
        finally:
            if fetch_all:
                close_cursor({}, {'cursor_id': cursor_id})
                connection.connection.commit()
                connection.close()

        return result
    return wrapper

def __translate_fetched_cell(cell):
    if isinstance(cell, geoalchemy2.WKBElement):
        return _get_engine().execute(cell.ST_AsText()).scalar()
    elif isinstance(cell, memoryview):
        return wkb.dumps(wkb.loads(cell.tobytes()), hex=True)
    else:
        return cell

def __response_success():
    return {'success': True}


def _response_error(message):
    return {'success': False, 'message':message}


class InvalidRequest(Exception):
    pass


def _translate_sqla_type(t, el):
    if t.lower() == 'array':
        return el + '[]'
    else:
        return t


def describe_columns(schema, table):
    """
    Loads the description of all columns of the specified table and return their
    description as a dictionary. Each column is identified by its name and
    points to a dictionary containing the information specified in https://www.postgresql.org/docs/9.3/static/infoschema-columns.html:

    * ordinal_position
    * column_default
    * is_nullable
    * data_type
    * character_maximum_length
    * character_octet_length
    * numeric_precision
    * numeric_precision_radix
    * numeric_scale
    * datetime_precision
    * interval_type
    * interval_precision
    * dtd_identifier
    * is_updatable

    :param schema: Schema name

    :param table: Table name

    :return: A dictionary of describing dictionaries representing the columns
    identified by their column names
    """

    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = 'select column_name, ' \
            'c.ordinal_position, c.column_default, c.is_nullable, c.data_type, ' \
            'c.character_maximum_length, c.character_octet_length, ' \
            'c.numeric_precision, c.numeric_precision_radix, c.numeric_scale, ' \
            'c.datetime_precision, c.interval_type, c.interval_precision, ' \
            'c.maximum_cardinality, c.dtd_identifier, c.is_updatable, e.data_type as element_type ' \
            'from INFORMATION_SCHEMA.COLUMNS  c ' \
            'LEFT JOIN information_schema.element_types e ' \
            'ON ((c.table_catalog, c.table_schema, c.table_name, \'TABLE\', c.dtd_identifier) ' \
            '= (e.object_catalog, e.object_schema, e.object_name, e.object_type, e.collection_type_identifier)) where table_name = ' \
            '\'{table}\' and table_schema=\'{schema}\';'.format(
        table=table, schema=schema)
    response = session.execute(query)
    session.close()
    return {column.column_name: {
        'ordinal_position': column.ordinal_position,
        'column_default': column.column_default,
        'is_nullable': column.is_nullable == 'YES',
        'data_type': _translate_sqla_type(column.data_type, column.element_type),
        'character_maximum_length': column.character_maximum_length,
        'character_octet_length': column.character_octet_length,
        'numeric_precision': column.numeric_precision,
        'numeric_precision_radix': column.numeric_precision_radix,
        'numeric_scale': column.numeric_scale,
        'datetime_precision': column.datetime_precision,
        'interval_type': column.interval_type,
        'interval_precision': column.interval_precision,
        'maximum_cardinality': column.maximum_cardinality,
        'dtd_identifier': column.dtd_identifier,
        'is_updatable': column.is_updatable == 'YES'
    } for column in response}


def describe_indexes(schema, table):
    """
    Loads the description of all indexes of the specified table and return their
    description as a dictionary. Each index is identified by its name and
    points to a dictionary containing the following information:

    * indexname: The name of the index
    * indexdef: The SQL-Statement used to create this index

    :param schema: Schema name

    :param table: Table name

    :return: A dictionary of describing dictionaries representing the indexed
    identified by their column names
    """
    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = 'select indexname, indexdef from pg_indexes where tablename = ' \
            '\'{table}\' and schemaname=\'{schema}\';'.format(
        table=table, schema=schema)
    response = session.execute(query)
    session.close()

    # Use a single-value dictionary to allow future extension with downward
    # compatibility
    return {column.indexname: {
        'indexdef': column.indexdef,
    } for column in response}


def describe_constraints(schema, table):
    """
    Loads the description of all constraints of the specified table and return their
    description as a dictionary. Each constraints is identified by its name and
    points to a dictionary containing the following information specified in https://www.postgresql.org/docs/9.3/static/infoschema-table-constraints.html:

    * constraint_typ
    * is_deferrable
    * initially_deferred
    * definition: This additional entry contains the SQL-query used to create
        this constraints

    :param schema: Schema name

    :param table: Table name

    :return: A dictionary of describing dictionaries representing the columns
    identified by their column names
    """

    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = 'select constraint_name, constraint_type, is_deferrable, initially_deferred, pg_get_constraintdef(c.oid) as definition from information_schema.table_constraints JOIN pg_constraint AS c  ON c.conname=constraint_name where table_name=\'{table}\' AND constraint_schema=\'{schema}\';'.format(
        table=table, schema=schema)
    response = session.execute(query)
    session.close()
    return {column.constraint_name: {
        'constraint_type': column.constraint_type,
        'is_deferrable': column.is_deferrable,
        'initially_deferred': column.initially_deferred,
        'definition': column.definition
    } for column in response}




def perform_sql(sql_statement, parameter=None):
    """
    Performs a sql command on standard database.
    :param sql_statement: SQL-Command
    :return: Dictionary with results
    """

    if not parameter:
        parameter = {}

    engine = _get_engine()
    session = sessionmaker(bind=engine)()

    # Statement built and no changes required, so statement is empty.
    logger.debug("SQL STATEMENT: |" + sql_statement + "| \t " + str(parameter))
    if not sql_statement or sql_statement.isspace():
        return get_response_dict(success=True)

    try:
        result = session.execute(sql_statement, parameter)
    except Exception as e:
        print("SQL Action failed. \n Error:\n" + str(e))
        session.rollback()
        raise APIError(str(e))
    else:
        # Why is commit() not part of close() ?
        # I have to commit the changes before closing session. Otherwise the changes are not persistent.
        session.commit()
    finally:
        session.close()

    return get_response_dict(success=True, result=result)


def remove_queued_column(id):
    """
    Remove a requested change.
    :param id: id of Change
    :return: Nothing
    """

    sql = "UPDATE api_columns SET reviewed=True WHERE id='{id}'".format(id=id)
    perform_sql(sql)


def apply_queued_column(id):
    """
    Apply a requested change
    :param id: id of Change
    :return: Result of Database Operation
    """

    column_description = get_column_change(id)
    res = table_change_column(column_description)

    if res.get('success') is True:
        sql = "UPDATE api_columns SET reviewed=True, changed=True WHERE id='{id}'".format(id=id)
    else:
        ex_str = str(res.get('exception'))
        sql = "UPDATE api_columns SET reviewed=False, changed=False, exception={ex_str} WHERE id='{id}'".format(id=id,
                                                                                                                ex_str=ex_str)

    perform_sql(sql)
    return res


def apply_queued_constraint(id):
    """
    Apply a requested change to constraints
    :param id: id of Change
    :return: Result of Database Operation
    """

    constraint_description = get_constraint_change(id)
    res = table_change_constraint(constraint_description)

    if res.get('success') is True:
        sql = "UPDATE api_constraints SET reviewed=True, changed=True WHERE id='{id}'".format(id=id)
    else:
        ex_str = str(res.get('exception'))
        sql = "UPDATE api_constraints SET reviewed=False, changed=False, exception={ex_str} WHERE id='{id}'".format(
            id=id, ex_str=ex_str)
    perform_sql(sql)
    return res


def remove_queued_constraint(id):
    """
    Remove a requested change to constraints
    :param id:
    :return:
    """

    sql = "UPDATE api_constraints SET reviewed=True WHERE id='{id}'".format(id=id)
    perform_sql(sql)


def get_response_dict(success, http_status_code=200, reason=None, exception=None, result=None):
    """
    Unified error description
    :param success: Task was successful or unsuccessful
    :param http_status_code: HTTP status code for indication
    :param reason: reason, why task failed, humanreadable
    :param exception exception, if available
    :return: Dictionary with results
    """
    dict = {'success': success,
            'error': str(reason).replace('\n', ' ').replace('\r', ' ') if reason is not None else None,
            'http_status': http_status_code,
            'exception': exception,
            'result': result
            }
    return dict


def queue_constraint_change(schema, table, constraint_def):
    """
    Queue a change to a constraint
    :param schema: Schema
    :param table: Table
    :param constraint_def: Dict with constraint definition
    :return: Result of database command
    """

    cd = api.parser.replace_None_with_NULL(constraint_def)

    sql_string = "INSERT INTO public.api_constraints (action, constraint_type" \
                 ", constraint_name, constraint_parameter, reference_table, reference_column, c_schema, c_table) " \
                 "VALUES ('{action}', '{c_type}', '{c_name}', '{c_parameter}', '{r_table}', '{r_column}' , '{c_schema}' " \
                 ", '{c_table}');".format(action=get_or_403(cd, 'action'),
                                          c_type=get_or_403(cd, 'constraint_type'),
                                          c_name=get_or_403(cd, 'constraint_name'),
                                          c_parameter=get_or_403(cd, 'constraint_parameter'),
                                          r_table=get_or_403(cd, 'reference_table'),
                                          r_column=get_or_403(cd, 'reference_column'),
                                          c_schema=schema,
                                          c_table=table).replace('\'NULL\'', 'NULL')

    return perform_sql(sql_string)


def queue_column_change(schema, table, column_definition):
    """
    Quere a change to a column
    :param schema: Schema
    :param table: Table
    :param column_definition: Dict with column definition
    :return: Result of database command
    """

    column_definition = api.parser.replace_None_with_NULL(column_definition)

    sql_string = "INSERT INTO public.api_columns (column_name, not_null, data_type, new_name, c_schema, c_table) " \
                 "VALUES ('{name}','{not_null}','{data_type}','{new_name}','{c_schema}','{c_table}');" \
        .format(name=get_or_403(column_definition, 'column_name'),
                not_null=get_or_403(column_definition, 'not_null'),
                data_type=get_or_403(column_definition, 'data_type'),
                new_name=get_or_403(column_definition, 'new_name'),
                c_schema=schema,
                c_table=table).replace('\'NULL\'', 'NULL')

    return perform_sql(sql_string)


def get_column_change(i_id):
    """
    Get one explicit change
    :param i_id: ID of Change
    :return: Change or None, if no change found
    """
    all_changes = get_column_changes()
    for change in all_changes:
        if int(change.get('id')) == int(i_id):
            return change

    return None


def get_constraint_change(i_id):
    """
    Get one explicit change
    :param i_id: ID of Change
    :return: Change or None, if no change found
    """
    all_changes = get_constraints_changes()

    for change in all_changes:
        if int(change.get('id')) == int(i_id):
            return change

    return None


def get_column_changes(reviewed=None, changed=None, schema=None, table=None):
    """
    Get all column changes
    :param reviewed: Reviewed Changes
    :param changed: Applied Changes
    :return: List with Column Definitions
    """

    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = ["SELECT * FROM public.api_columns"]

    if reviewed is not None or changed is not None or schema is not None or table is not None:
        query.append(" WHERE ")

        where = []

        if reviewed is not None:
            where.append("reviewed = " + str(reviewed))

        if changed is not None:
            where.append("changed = " + str(changed))

        if schema is not None:
            where.append("c_schema = '{schema}'".format(schema=schema))

        if table is not None:
            where.append("c_table = '{table}'".format(table=table))

        query.append(" AND ".join(where))

    query.append(";")

    sql = ''.join(query)

    response = session.execute(sql)
    session.close()

    return [{'column_name': column.column_name,
             'not_null': column.not_null,
             'data_type': column.data_type,
             'new_name': column.new_name,
             'reviewed': column.reviewed,
             'changed': column.changed,
             'c_schema': column.c_schema,
             'c_table': column.c_table,
             'id': column.id,
             'exception': column.exception
             } for column in response]


def get_constraints_changes(reviewed=None, changed=None, schema=None, table=None):
    """
    Get all constraint changes
    :param reviewed: Reviewed Changes
    :param changed: Applied Changes
    :return: List with Column Definitons
    """
    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = ["SELECT * FROM public.api_constraints"]

    if reviewed is not None or changed is not None or schema is not None or table is not None:
        query.append(" WHERE ")

        where = []

        if reviewed is not None:
            where.append("reviewed = " + str(reviewed))

        if changed is not None:
            where.append("changed = " + str(changed))

        if schema is not None:
            where.append("c_schema = '{schema}'".format(schema=schema))

        if table is not None:
            where.append("c_table = '{table}'".format(table=table))

        query.append(" AND ".join(where))

    query.append(";")

    sql = ''.join(query)

    response = session.execute(sql)
    session.close()

    return [{'action': column.action,
             'constraint_type': column.constraint_type,
             'constraint_name': column.constraint_name,
             'constraint_parameter': column.constraint_parameter,
             'reference_table': column.reference_table,
             'reference_column': column.reference_column,
             'reviewed': column.reviewed,
             'changed': column.changed,
             'c_schema': column.c_schema,
             'c_table': column.c_table,
             'id': column.id,
             'exception': column.exception
             } for column in response]


def get_column_definition_query(c):
    return "{name} {data_type} {length} {not_null} {default}".format(
        name=get_or_403(c, 'name'),
        data_type=get_or_403(c, 'data_type'),
        length=('(' + str(c['character_maximum_length']) + ')')
                    if c.get('character_maximum_length', False)
                    else '',
        not_null="NOT NULL"
                    if not c.get('is_nullable', True)
                    else "",
        default= 'DEFAULT ' + api.parser.read_pgvalue(c['column_default'])
                    if 'column_default' in c
                    else ""
        )


def column_alter(query, context, schema, table, column):
    if column == 'id':
        raise APIError('You cannot alter the id column')
    alter_preamble = "ALTER TABLE {schema}.{table} ALTER COLUMN {column} ".format(
        schema=schema,
        table=table,
        column=column
    )
    if "data_type" in query:
        sql = alter_preamble + "SET DATA TYPE " + read_pgid(query['data_type'])
        if 'character_maximum_length' in query:
            sql += '(' +  api.parser.read_pgvalue(query['character_maximum_length']) + ')'
        perform_sql(sql)
    if "is_nullable" in query:
        if read_bool(query['is_nullable']):
            sql = alter_preamble + ' DROP NOT NULL'
        else:
            sql = alter_preamble + ' SET NOT NULL'
        perform_sql(sql)
    if 'column_default' in query:
        value = api.parser.read_pgvalue(query['column_default'])
        sql = alter_preamble + 'SET DEFAULT ' + value
        perform_sql(sql)
    if 'name' in query:
        sql = ("ALTER TABLE {schema}.{table} RENAME COLUMN {column} TO " + read_pgid(query['name'])).format(
            schema=schema,
            table=table,
            column=column
        )
        perform_sql(sql)
    return get_response_dict(success=True)

def column_add(schema, table, column, description):
    description['name'] = column
    settings = get_column_definition_query(description)
    s = 'ALTER TABLE {schema}.{table} ADD COLUMN ' + settings
    edit_table = get_edit_table_name(schema, table)
    insert_table = get_insert_table_name(schema, table)
    perform_sql(s.format(schema=schema, table=table))
    # Do the same for update and insert tables.
    meta_schema = get_meta_schema_name(schema)
    perform_sql(s.format(schema=meta_schema,
                         table=edit_table))

    meta_schema = get_meta_schema_name(schema)
    perform_sql(s.format(schema=meta_schema,
                         table=insert_table))
    return get_response_dict(success=True)


def table_create(schema, table, columns, constraints):
    """
    Creates a new table.
    :param schema: schema
    :param table: table
    :param columns: Description of columns
    :param constraints: Description of constraints
    :return: Dictionary with results
    """

    # Building and joining a string array seems to be more efficient than native string concats.
    # https://waymoot.org/home/python_string/

    id_columns = [c for c in columns if c['name'] == 'id']
    if not id_columns:
        raise APIError('Your table must have one column "id" of type "bigserial"')
    cid = id_columns[0]
    if not get_or_403(cid, 'data_type').lower() == 'bigserial':
        raise APIError('Your column "id" must have type "bigserial"')
    str_list = []
    str_list.append("CREATE TABLE {schema}.\"{table}\" (".format(schema=schema, table=table))

    str_list.append(', '.join(get_column_definition_query(c) for c in columns))

    str_list.append(");")
    sql_string = ''.join(str_list)


    results = [perform_sql(sql_string)]

    for constraint_definition in constraints:
        results.append(table_change_constraint(constraint_definition))

    for res in results:
        if not res.get('success', True):
            return res

    return get_response_dict(success=True)


def table_change_column(column_definition):
    """
    Changes a table column.
    :param schema: schema
    :param table: table
    :param column_definition: column definition according to Issue #184
    :return: Dictionary with results
    """

    schema = get_or_403(column_definition,'c_schema')
    table = get_or_403(column_definition, 'c_table')

    # Check if column exists
    existing_column_description = describe_columns(schema, table)

    if len(existing_column_description) <= 0:
        return get_response_dict(False, 400, 'table is not defined.')

    # There is a table named schema.table.
    sql = []

    start_name = get_or_403(column_definition, 'column_name')
    current_name = get_or_403(column_definition, 'column_name')

    if current_name in existing_column_description:
        # Column exists and want to be changed

        # Figure out, which column should be changed and constraint or datatype or name should be changed

        if get_or_403(column_definition, 'new_name') is not None:
            # Rename table
            sql.append(
                "ALTER TABLE {schema}.{table} RENAME COLUMN {name} TO {new_name};".format(schema=schema, table=table,
                                                                                          name=current_name,
                                                                                          new_name=column_definition[
                                                                                              'new_name']))
            # All other operations should work with new name
            current_name = column_definition['new_name']

        cdef_datatype = column_definition.get('data_type')
        # TODO: Fix rudimentary handling of datatypes
        if cdef_datatype is not None and cdef_datatype != existing_column_description[column_definition['name']][
            'data_type']:
            sql.append("ALTER TABLE {schema}.{table} ALTER COLUMN {c_name} TYPE {c_datatype};".format(schema=schema,
                                                                                                      table=table,
                                                                                                      c_name=current_name,
                                                                                                      c_datatype=
                                                                                                      column_definition[
                                                                                                          'data_type']))

        c_null = 'NO' in existing_column_description[start_name]['is_nullable']
        cdef_null = column_definition.get('not_null');
        if cdef_null is not None and c_null != cdef_null:
            if c_null:
                # Change to nullable
                sql.append('ALTER TABLE {schema}.{table} ALTER COLUMN {c_name} DROP NOT NULL;'.format(schema=schema,
                                                                                                      table=table,
                                                                                                      c_name=current_name))
            else:
                # Change to not null
                sql.append('ALTER TABLE {schema}.{table} ALTER COLUMN {c_name} SET NOT NULL;'.format(schema=schema,
                                                                                                     table=table,
                                                                                                     c_name=current_name))
    else:
        # Column does not exist and should be created
        # Request will end in 500, if an argument is missing.
        sql.append(
            "ALTER TABLE {schema}.{table} ADD {c_name} {c_datatype} {c_notnull};".format(schema=schema, table=table,
                                                                                         c_name=current_name,
                                                                                         c_datatype=get_or_403(column_definition,
                                                                                                               'data_type'),
                                                                                         c_notnull="NOT NULL" if column_definition.get('notnull', False) else ""))

    sql_string = ''.join(sql)

    return perform_sql(sql_string)


def table_change_constraint(constraint_definition):
    """
    Changes constraint of table
    :param schema: schema
    :param table: table
    :param constraint_definition: constraint definition according to Issue #184
    :return: Dictionary with results
    """

    table = get_or_403(constraint_definition, 'c_table')
    schema = get_or_403(constraint_definition, 'c_schema')

    existing_column_description = describe_columns(schema, table)

    if len(existing_column_description) <= 0:
        raise APIError('Table does not exist')

    # There is a table named schema.table.
    sql = []

    if 'ADD' in get_or_403(constraint_definition, 'action'):
        sql.append(
            'ALTER TABLE {schema}.{table} {action} {constraint_name} {constraint_type} ({constraint_parameter})'.format(
                schema=schema, table=table,
                action=get_or_403(constraint_definition, 'action'),
                constraint_name = 'CONSTRAINT ' + constraint_definition['constraint_name'] if 'constraint_name' in constraint_definition else '',
                constraint_parameter = get_or_403(constraint_definition, 'constraint_parameter'),
                constraint_type = get_or_403(constraint_definition, 'constraint_type')))

        if 'FOREIGN KEY' in get_or_403(constraint_definition, 'constraint_type'):
            if get_or_403(constraint_definition, 'reference_table') is None or get_or_403(constraint_definition, 'reference_column') is None:
                raise APIError('references are not defined correctly')
            sql.append(' REFERENCES {reference_table}({reference_column})'.format(
                reference_column=constraint_definition['reference_column'],
                reference_table=constraint_definition['reference_table']))

        sql.append(';')
    elif 'DROP' in constraint_definition['action']:
        sql.append('ALTER TABLE {schema}.{table} DROP CONSTRAINT {constraint_name}'.format(schema=schema, table=table,
                                                                                           constraint_name=
                                                                                           constraint_definition[
                                                                                               'constraint_name']))

    sql_string = ''.join(sql)

    return perform_sql(sql_string)


def put_rows(schema, table, column_data):
    keys = list(column_data.keys())
    values = list(column_data.values())

    values = ["'{0}'".format(value) for value in values]

    sql = "INSERT INTO {schema}.{table} ({keys}) VALUES({values})".format(schema=schema, table=table,
                                                                          keys=','.join(keys),
                                                                          values=','.join(values))

    return perform_sql(sql)


"""
ACTIONS FROM OLD API
"""


def _get_table(schema, table):
    engine = _get_engine()
    metadata = MetaData(bind=_get_engine())

    return Table(table, metadata, autoload=True, autoload_with=engine, schema=schema)


def __internal_select(query, context):
    engine = _get_engine()
    conn = engine.connect()
    try:
        cursor = conn.connection.cursor()
        cursor_id = cursor.__hash__
        __CURSORS[cursor_id] = cursor
        context2 = dict(context)
        context2['cursor_id'] = cursor_id
        rows = data_search(query, context2)
        rows['data'] = [x for x in cursor.fetchall()]
        cursor.close()
    finally:
        conn.close()
    return rows


def __change_rows(request, context, target_table, setter, fields=None):
    query = {
        'from': {
            'type': 'table',
            'schema': request['schema'],
            'table': request['table']
        },
        'where': request['where']
    }
    if fields:
        query['fields'] = [{'type': 'column',
                             'column': f} for f in fields]

    user = context['user'].name

    rows = __internal_select(query, context)

    message = request.get('message', None)
    meta_fields = list(api.parser.set_meta_info('update', user, message).items())
    if fields is None:
        fields = [field[0] for field in rows['description']]
    fields += [f[0] for f in meta_fields]

    table_name = request['table']
    meta = MetaData(bind=_get_engine())
    table = Table(table_name, meta, autoload=True, schema=request['schema'])
    pks = [c for c in table.columns if c.primary_key]

    inserts = []
    cursor = _load_cursor(context['cursor_id'])
    if rows['data']:
        for row in rows['data']:
            insert = []
            for (key, value) in list(zip(fields, row)) + meta_fields:
                if not api.parser.is_pg_qual(key):
                    raise APIError('%s is not a PostgreSQL identifier'%key)
                if key in setter:
                    if not (key in pks and value != setter[key]):
                        value = setter[key]
                    else:
                        raise InvalidRequest(
                            "Primary keys must remain unchanged.")
                insert.append((key, value))

            inserts.append(dict(insert))
        # Add metadata for insertions
        schema = request['schema']
        meta_schema = get_meta_schema_name(schema) if not schema.startswith(
            '_') else schema

        insert_table = _get_table(meta_schema, target_table)
        query = insert_table.insert(values=inserts)
        _execute_sqla(query, cursor)
    return {'affected':len(rows['data'])}


def data_delete(request, context=None):
    target_table = get_delete_table_name(request['schema'],request['table'])
    setter = []
    return __change_rows(request, context, target_table, setter, ['id'])


def data_update(request, context=None):
    target_table = get_edit_table_name(request['schema'],request['table'])
    setter = get_or_403(request, 'values')
    return __change_rows(request, context, target_table, setter)

def data_insert_check(schema, table, values, context):

    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    query = 'SELECT array_agg(column_name::text) as columns, conname, ' \
            '   contype AS type ' \
            'FROM pg_constraint AS conkeys ' \
            'JOIN information_schema.constraint_column_usage AS ccu ' \
            '   ON ccu.constraint_name = conname ' \
            'WHERE table_name=\'{table}\' ' \
            '   AND table_schema=\'{schema}\' ' \
            '   AND conrelid=\'{schema}.{table}\'::regclass::oid ' \
            'GROUP BY conname, contype;'.format(
        table=table, schema=schema)
    response = session.execute(query)
    session.close()

    for constraint in response:
        columns = constraint.columns
        if constraint.type.lower() == 'c':
            pass
        elif constraint.type.lower() == 'f':
            pass
        elif constraint.type.lower() in ['u', 'p']:
            for row in values:
                query = {
                    'from':
                        {
                            'type': 'table',
                            'schema': schema,
                            'table': table
                        },
                    'where':
                        {
                            'type': 'operator',
                            'operator': 'AND',
                            'operands':
                                [{
                                    'operands': [
                                        {
                                            'type': 'column',
                                            'column': c
                                        },
                                        {
                                            'type': 'value',
                                            'value': _load_value(row[c])
                                        } if c in row else {'type': 'value'}],
                                    'operator': '=',
                                    'type': 'operator'
                                } for c in columns]
                        },
                    'fields': [{'type': 'column',
                                    'column': f} for f in columns]
                }
                rows =__internal_select(query, context)
                if rows['data']:
                    raise APIError('Action violates constraint {cn}. Failing row was {row}'.format(cn=constraint.conname, row='(' + (', '.join(str(row[c]) for c in row if not c.startswith('_')))) + ')')

    for column_name, column in describe_columns(schema, table).items():
        if not column.get('is_nullable', True):
            for row in values:
                val = row.get(column_name, None)
                if (val is None
                    or (isinstance(val, str)
                        and val.lower() == 'null')):
                    if column_name in row or not column.get('column_default', None):
                        raise APIError(
                            'Action violates not-null constraint on {col}. Failing row was {row}'.format(
                                col=column_name, row='(' + (', '.join(
                                    str(row[c]) for c in row if
                                    not c.startswith('_')))) + ')')

def _load_value(v):
    if isinstance(v,str):
        if v.isdigit():
            return int(v)
    return v


def data_insert(request, context=None):
    cursor = _load_cursor(context['cursor_id'])
    # If the insert request is not for a meta table, change the request to do so
    orig_table = get_or_403(request, 'table')
    if orig_table.startswith('_') or orig_table.endswith('_cor'):
        raise APIError("Insertions on meta tables is not allowed", status=403)
    orig_schema = get_or_403(request, 'schema')

    request['table'] = get_insert_table_name(orig_schema,
                                             orig_table)
    if not orig_schema.startswith('_'):
        request['schema'] = '_' + orig_schema

    query, values = api.parser.parse_insert(request, context)
    data_insert_check(orig_schema, orig_table, values, context)
    _execute_sqla(query, cursor)
    description = cursor.description
    response = {}
    if description:
        response['description'] = [[col.name, col.type_code, col.display_size,
            col.internal_size, col.precision, col.scale,
            col.null_ok] for col in description]
    return response


def _execute_sqla(query, cursor):
    try:
        compiled = query.compile()
    except exc.SQLAlchemyError as e:
        raise APIError(repr(e))
    try:
        params = dict(compiled.params)
        logger.debug('Executed %s with parameters %s'%(str(compiled), params))
        cursor.execute(str(compiled), params)
    except (psycopg2.DataError, exc.IdentifierError, psycopg2.IntegrityError) as e:
        raise APIError(repr(e))
    except psycopg2.InternalError as e:
        if re.match(r'Input geometry has unknown \(\d+\) SRID', repr(e)):
            # Return only SRID errors
            raise APIError(repr(e))
        else:
            raise e
    except psycopg2.ProgrammingError as e:
        if e.pgcode in [
            '42703',    # undefined_column
            '42883',    # undefined_function
            '42P01',    # undefined_table
            '42P02',    # undefined_parameter
            '42704',    # undefined_object
        ]:
            # Return only `function does not exists` errors
            raise APIError(e.diag.message_primary)
        else:
            raise e
    except psycopg2.DatabaseError as e:
        # Other DBAPIErrors should not be reflected to the client.
        raise e

def process_value(val):
    if isinstance(val, str):
        return "'" + val.replace('\'', '\\\'') + "'"
    if isinstance(val, datetime):
        return "'" + str(val) + "'"
    if val is None:
        return 'null'
    else:
        return str(val)


def table_drop(request, context=None):
    raise PermissionDenied
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])

    # load schema name and check for sanity
    schema = request.pop("schema", "public")
    if not api.parser.is_pg_qual(schema):
        return {'success': False, 'reason': 'Invalid schema name: %s' % schema}
        # Check whether schema exists

    # load table name and check for sanity
    table = request.pop("table", None)

    if not api.parser.is_pg_qual(table):
        return {'success': False, 'reason': 'Invalid table name: %s' % table}

    try:
        exists = bool(request.pop("exists", False))
    except:
        return {'success': False,
                'reason': 'Invalid exists clause: %s' % exists}

    option = request.pop("option", None)
    if option and option.lower() not in ["cascade", "restrict"]:
        return {'success': False, 'reason': 'Invalid option clause name: %s' % option}

    sql_string = "drop table {exists} {schema}.{table} {option} ".format(
        schema=schema,
        table=table,
        option=option if option else "",
        exists="IF EXISTS" if exists else "")

    session = sessionmaker(bind=engine)()
    try:
        session.execute(sql_string.replace('%', '%%'))
    except Exception as e:
        traceback.print_exc()
        session.rollback()
        raise e
    else:
        session.commit()
    finally:
        session.close()
    return {}


def data_search(request, context=None):
    query = api.parser.parse_select(request)
    cursor = _load_cursor(context['cursor_id'])
    _execute_sqla(query, cursor)
    description = [[col.name, col.type_code, col.display_size,
                                 col.internal_size, col.precision, col.scale,
                                 col.null_ok] for col in cursor.description]
    result = {'description': description}
    return result



def _get_count(q):
    count_q = q.statement.with_only_columns([func.count()]).order_by(None)
    count = q.session.execute(count_q).scalar()
    return count


def count_all(request, context=None):
    table = get_or_403(request, 'table')
    schema = get_or_403(request, 'schema')
    engine = _get_engine()
    session = sessionmaker(bind=engine)()
    t = _get_table(schema, table)
    return session.query(t).count()  # _get_count(session.query(t))


def _get_header(results):
    header = []
    for field in results.cursor.description:
        header.append({
            'id': field[0],  # .decode('utf-8'),
            'type': field[1]
        })
    return header


def analyze_columns(schema, table):
    engine = _get_engine()
    result = engine.execute(
        "select column_name as id, data_type as type from information_schema.columns where table_name = '{table}' and table_schema='{schema}';".format(
            schema=schema, table=table))
    return [{'id': get_or_403(r, 'id'), 'type': get_or_403(r, 'type')} for r in result]


def search(db, schema, table, fields=None, pk = None, offset = 0, limit = 100):

    if not fields:
        fields = '*'
    else:
        fields = ', '.join(map(quote(fields)))
    engine = _get_engine()
    refs = engine.execute(references.Entry.__table__.select())

    sql_string = "select {fields} from {schema}.{table}".format(
        schema=schema, table=table, fields=fields)

    if pk:
        sql_string += " where {} = {}".format(pk[0], pk[1])

    sql_string += " limit {}".format(limit)
    sql_string += " offset {}".format(offset)
    return engine.execute(sql_string, ), [dict(refs.first()).items()]


def clear_dict(d):
    return {
        k.replace(" ", "_"): d[k] if not isinstance(d[k], dict) else clear_dict(
            d[k]) for k in d}


def create_meta(schema, table):
    meta_schema = get_meta_schema_name(schema)

    if not has_schema({'schema': '_' + schema}):
        create_meta_schema(schema)

    get_edit_table_name(schema, table)
    # Table for inserts
    get_insert_table_name(schema, table)


def get_comment_table(schema, table):
    engine = _get_engine()

    sql_string = "select obj_description('{schema}.{table}'::regclass::oid, 'pg_class');".format(
        schema=schema, table=table)
    res = engine.execute(sql_string)
    if res:
        jsn = res.first().obj_description
        if jsn:
            jsn = jsn.replace('\n', '')
        else:
            return {}
        try:
            return json.loads(jsn)
        except ValueError:
            return {'error': 'No json format', 'description': jsn}
    else:
        return {}


def data_info(request, context=None):
    return request


def connect():
    engine = _get_engine()
    insp = sqla.inspect(engine)
    return insp


def has_schema(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.has_schema(conn, get_or_403(request, 'schema'))
    finally:
        conn.close()
    return result


def has_table(request, context=None):
    engine = _get_engine()
    schema = request.pop('schema', None)
    table = get_or_403(request, 'table')
    conn = engine.connect()
    try:
        result = engine.dialect.has_table(conn, table,
                                          schema=schema)
    finally:
        conn.close()
    return result


def has_sequence(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.has_sequence(conn,
                                             get_or_403(request, 'sequence_name'),
                                             schema=request.get('schema', None))
    finally:
        conn.close()
    return result


def has_type(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.has_schema(conn,
                                           get_or_403(request, 'sequence_name'),
                                           schema=request.get('schema', None))
    finally:
        conn.close()
    return result


def get_table_oid(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_table_oid(conn,
                                              get_or_403(request, 'table'),
                                              schema=request.get('schema', None),
                                              **request)
    finally:
        conn.close()
    return result


def get_schema_names(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_schema_names(engine.connect(), **request)
    finally:
        conn.close()
    return result


def get_table_names(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_table_names(conn,
                                                schema=request.pop('schema',
                                                                   None),
                                                **request)
    finally:
        conn.close()
    return result


def get_view_names(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_view_names(conn,
                                               schema=request.pop('schema', None),
                                               **request)
    finally:
        conn.close()
    return result


def get_view_definition(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_schema_names(conn,
                                                 get_or_403(request, 'view_name'),
                                                 schema=request.pop('schema',
                                                                    None),
                                                 **request)
    finally:
        conn.close()
    return result


def get_columns(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_columns(conn,
                                            get_or_403(request, 'table'),
                                            schema=request.pop('schema', None),
                                            **request)
    finally:
        conn.close()
    return result


def get_pk_constraint(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_pk_constraint(conn,
                                                  get_or_403(request, 'table'),
                                                  schema=request.pop('schema',
                                                                     None),
                                                  **request)
    finally:
        conn.close()
    return result


def get_foreign_keys(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_foreign_keys(conn,
                                                 get_or_403(request, 'table'),
                                                 schema=request.pop('schema',
                                                                    None),
                                                 postgresql_ignore_search_path=request.pop(
                                                     'postgresql_ignore_search_path',
                                                     False),
                                                 **request)
    finally:
        conn.close()
    return result


def get_indexes(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_indexes(conn,
                                            get_or_403(request, 'table'),
                                            get_or_403(request, 'schema'),
                                            **request)
    finally:
        conn.close()
    return result


def get_unique_constraints(request, context=None):
    engine = _get_engine()
    conn = engine.connect()
    try:
        result = engine.dialect.get_foreign_keys(conn,
                                                 get_or_403(request, 'table'),
                                                 schema=request.pop('schema',
                                                                    None),
                                                 **request)
    finally:
        conn.close()
    return result


def __get_connection(request):
    # TODO: Implement session-based connection handler
    engine = _get_engine()
    return engine.connect()


def get_isolation_level(request, context):
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    result = engine.dialect.get_isolation_level(cursor)
    return result


def set_isolation_level(request, context):
    level = request.get('level', None)
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    try:
        engine.dialect.set_isolation_level(cursor, level)
    except exc.ArgumentError as ae:
        return _response_error(ae.message)
    return __response_success()


def do_begin_twophase(request, context):
    xid = request.get('xid', None)
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    engine.dialect.do_begin_twophase(cursor, xid)
    return __response_success()


def do_prepare_twophase(request, context):
    xid = request.get('xid', None)
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    engine.dialect.do_prepare_twophase(cursor, xid)
    return __response_success()


def do_rollback_twophase(request, context):
    xid = request.get('xid', None)
    is_prepared = request.get('is_prepared', True)
    recover = request.get('recover', False)
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    engine.dialect.do_rollback_twophase(cursor, xid,
                                        is_prepared=is_prepared,
                                        recover=recover)
    return __response_success()


def do_commit_twophase(request, context):
    xid = request.get('xid', None)
    is_prepared = request.get('is_prepared', True)
    recover = request.get('recover', False)
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    engine.dialect.do_commit_twophase(cursor, xid, is_prepared=is_prepared,
                                      recover=recover)
    return __response_success()


def do_recover_twophase(request, context):
    engine = _get_engine()
    cursor = _load_cursor(context['cursor_id'])
    return engine.dialect.do_commit_twophase(cursor)


def _get_default_schema_name(self, connection):
    return connection.scalar("select current_schema()")


def open_raw_connection(request, context):
    engine = _get_engine()
    connection = engine.connect().connection
    connection_id = connection.__hash__()
    if connection_id not in __CONNECTIONS:
        __CONNECTIONS[connection_id]=connection
    return {'connection_id': connection_id}


def close_raw_connection(request, context):
    connection_id = request['connection_id']
    if connection_id in __CONNECTIONS:
        connection = __CONNECTIONS[connection_id]
        connection.close()
        return __response_success()
    else:
        return _response_error("Connection (%s) not found" % connection_id)


def open_cursor(request, context):
    connection_id = context['connection_id']
    if connection_id in __CONNECTIONS:
        connection = __CONNECTIONS[connection_id]
        cursor = connection.cursor()
        cursor_id = cursor.__hash__()
        __CURSORS[cursor_id] = cursor
        return {'cursor_id': cursor_id}
    else:
        return _response_error("Connection (%s) not found" % connection_id)


def _load_cursor(cursor_id):
    try:
        return __CURSORS[cursor_id]
    except KeyError:
        raise ResponsiveException("Cursor (%s) not found" % cursor_id)


def close_cursor(request, context):
    cursor_id = context['cursor_id']
    if cursor_id in __CURSORS:
        cursor = __CURSORS[cursor_id]
        del __CURSORS[cursor_id]
        cursor.close()
        return {'cursor_id': cursor_id}
    else:
        return _response_error("Cursor (%s) not found" % cursor_id)


def fetchone(request, context):
    cursor = _load_cursor(context['cursor_id'])
    return cursor.fetchone()


def fetchall(request, context):
    cursor = _load_cursor(context['cursor_id'])
    return cursor.fetchall()


def fetchmany(request, context):
    cursor = _load_cursor(context['cursor_id'])
    return cursor.fetchmany(request['size'])


def get_comment_table_name(schema, table, create=True):
    table_name = '_' + table + '_cor'
    if create and not has_table({'schema': get_meta_schema_name(schema),
                                 'table': table_name}):
        create_edit_table(schema, table)
    return table_name


def get_delete_table_name(schema, table, create=True):
    table_name = '_' + table + '_delete'
    if create and not has_table({'schema': get_meta_schema_name(schema),
                                 'table': table_name}):
        create_delete_table(schema, table)
    return table_name


def get_edit_table_name(schema, table, create=True):
    table_name = '_' + table + '_edit'
    if create and not has_table({'schema': get_meta_schema_name(schema),
                                 'table': table_name}):
        create_edit_table(schema, table)
    return table_name


def get_insert_table_name(schema, table, create=True):
    table_name = '_' + table + '_insert'
    if create and not has_table({'schema': get_meta_schema_name(schema),
                                 'table': table_name}):
        create_insert_table(schema, table)
    return table_name

def get_meta_schema_name(schema):
    return '_' + schema


def create_meta_schema(schema):
    engine = _get_engine()
    query = 'CREATE SCHEMA {schema}'.format(schema=get_meta_schema_name(schema))
    connection = engine.connect()
    connection.execute(query)


def create_edit_table(schema, table, meta_schema=None):
    if not meta_schema:
        meta_schema = get_meta_schema_name(schema)
    engine = _get_engine()
    query = 'CREATE TABLE "{meta_schema}"."{edit_table}" ' \
            '(LIKE "{schema}"."{table}" INCLUDING ALL EXCLUDING INDEXES, PRIMARY KEY (_id)) ' \
            'INHERITS (_edit_base);'.format(
        meta_schema=meta_schema,
        edit_table=get_edit_table_name(schema, table, create=False),
        schema=schema,
        table=table)
    engine.execute(query)


def create_delete_table(schema, table, meta_schema=None):
    if not meta_schema:
        meta_schema = get_meta_schema_name(schema)
    engine = _get_engine()
    query = 'CREATE TABLE {meta_schema}.{edit_table} ' \
            '(id bigint) ' \
            'INHERITS (_edit_base);'.format(
        meta_schema=meta_schema,
        edit_table=get_delete_table_name(schema, table, create=False),
        schema=schema,
        table=table)
    engine.execute(query)

def create_insert_table(schema, table, meta_schema=None):
    if not meta_schema:
        meta_schema = get_meta_schema_name(schema)
    engine = _get_engine()
    query = 'CREATE TABLE {meta_schema}.{edit_table} ' \
            '(LIKE {schema}.{table} INCLUDING ALL EXCLUDING INDEXES, PRIMARY KEY (_id)) ' \
            'INHERITS (_edit_base);'.format(
        meta_schema=meta_schema,
        edit_table=get_insert_table_name(schema, table, create=False),
        schema=schema,
        table=table)
    engine.execute(query)


def create_comment_table(schema, table, meta_schema=None):
    if not meta_schema:
        meta_schema = get_meta_schema_name(schema)
    engine = _get_engine()
    query = 'CREATE TABLE {schema}.{table} (PRIMARY KEY (_id)) ' \
            'INHERITS (_comment_base); '.format(
        schema=meta_schema,
        table=get_comment_table_name(table))
    engine.execute(query)


def getValue(schema, table, column, id):
    sql = "SELECT {column} FROM {schema}.{table} WHERE id={id}".format(column=column, schema=schema, table=table, id=id)

    engine = _get_engine()
    session = sessionmaker(bind=engine)()


    try:
        result = session.execute(sql)

        returnValue = None
        for row in result:
            returnValue = row[column]

        return returnValue
    except Exception as e:
        print("SQL Action failed. \n Error:\n" + str(e))
        session.rollback()
    finally:
        session.close()
    return None


def apply_changes(schema, table):
    def add_type(d, type):
        d['_type'] = type
        return d
    engine = _get_engine()
    conn = engine.connect()
    try:
        meta_schema = get_meta_schema_name(schema)

        insert_table = get_insert_table_name(schema, table)
        columns = list(describe_columns(schema, table).keys()) + ['_submitted', '_id']
        changes = (add_type({c: getattr(row, c) for c in columns}, 'insert') for row in conn.execute('select * '
                                'from {schema}.{table} '
                                'where _applied = FALSE;'.format(schema=meta_schema,
                                                                 table=insert_table)))


        update_table = get_edit_table_name(schema, table)
        changes = itertools.chain(changes, (add_type({c: getattr(row, c) for c in columns}, 'update') for row in conn.execute('select * '
                                'from {schema}.{table} '
                                'where _applied = FALSE;'.format(schema=meta_schema,
                                                                 table=update_table))))

        delete_table = get_delete_table_name(schema, table)
        changes = itertools.chain(changes, (
            add_type({c: getattr(row, c) for c in ['_id', 'id', '_submitted']}, 'delete') for row in
            conn.execute('select * '
                         'from {schema}.{table} '
                         'where _applied = FALSE;'.format(schema=meta_schema,
                                                          table=delete_table))))
    finally:
        conn.close()

    changes = list(changes)
    table_obj = Table(table, MetaData(bind=engine), autoload=True, schema=schema)
    session = sessionmaker(bind=engine)()
    try:
        for change in sorted(changes, key=lambda x: x['_submitted']):
            if change['_type'] == 'insert':
                apply_insert(session, table_obj, change)
            elif change['_type'] == 'update':
                apply_update(session, table_obj, change)
            elif change['_type'] == 'delete':
                apply_deletion(session, table_obj, change)
    except Exception as e:
        raise e
    else:
        session.commit()
    finally:
        session.close()


def apply_insert(session, table, row):
    logger.info("apply insert", row)
    session.execute(table.insert(), row)
    session.execute('UPDATE {schema}.{table} SET _applied=TRUE WHERE id={id};'.format(
        schema=get_meta_schema_name(table.schema),
        table=get_insert_table_name(table.schema, table.name),
        id=row['id']
    ))


def apply_update(session, table, row):
    logger.info("apply update", row)
    session.execute(table.update(table.c.id==row['id']), row)
    session.execute(
        'UPDATE {schema}.{table} SET _applied=TRUE WHERE _id={id};'.format(
            schema=get_meta_schema_name(table.schema),
            table=get_edit_table_name(table.schema, table.name),
            id=row['_id']
        ))

def apply_deletion(session, table, row):
    logger.info("apply deletion", row)
    session.execute(table.delete(table.c.id==row['id']), row)
    session.execute(
        'UPDATE {schema}.{table} SET _applied=TRUE WHERE _id={id};'.format(
            schema=get_meta_schema_name(table.schema),
            table=get_delete_table_name(table.schema, table.name),
            id=row['_id']
        ))
