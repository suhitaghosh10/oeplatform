from . import APITestCase
from api import actions
import json

from shapely import wkt, wkb

class TestPut(APITestCase):

    def setUp(self):
        self.test_table = 'test_table_column'
        self.test_schema = 'test'
        self.structure_data = {
            "constraints": [
                {
                    "constraint_type": "PRIMARY KEY",
                    "constraint_parameter": "id",
                    "reference_table": None,
                    "reference_column": None
                }
            ],
            "columns": [
                {
                    "name": "id",
                    "data_type": "integer",
                    "is_nullable": False,
                    "character_maximum_length": None
                },
                {
                    "name": "name",
                    "data_type": "character varying",
                    "is_nullable": True,
                    "character_maximum_length": 50
                }, {
                    "name": "address",
                    "data_type": "character varying",
                    "is_nullable": True,
                    "character_maximum_length": 150
                }, {
                    "name": "geom",
                    "data_type": "Geometry (Point)",
                    "is_nullable": True,
                }
            ]
        }

        c_basic_resp = self.__class__.client.put(
            '/api/v0/schema/{schema}/tables/{table}/'.format(
                schema=self.test_schema, table=self.test_table),
            data=json.dumps({'query': self.structure_data}),
            HTTP_AUTHORIZATION='Token %s' % self.__class__.token,
            content_type='application/json')

        assert c_basic_resp.status_code==201, c_basic_resp.json().get('reason','No reason returned')

    def tearDown(self):
        meta_schema = actions.get_meta_schema_name(self.test_schema)
        if actions.has_table(
                dict(table=self.test_table, schema=self.test_schema)):
            actions.perform_sql(
                "DROP TABLE IF EXISTS {schema}.{table} CASCADE".format(
                    schema=meta_schema,
                    table=actions.get_insert_table_name(self.test_schema,
                                                        self.test_table)
                ))
            actions.perform_sql(
                "DROP TABLE IF EXISTS {schema}.{table} CASCADE".format(
                    schema=meta_schema,
                    table=actions.get_edit_table_name(self.test_schema,
                                                      self.test_table)
                ))
            actions.perform_sql(
                "DROP TABLE IF EXISTS {schema}.{table} CASCADE".format(
                    schema=meta_schema,
                    table=actions.get_delete_table_name(self.test_schema,
                                                        self.test_table)
                ))
            actions.perform_sql(
                "DROP TABLE IF EXISTS {schema}.{table} CASCADE".format(
                    schema=self.test_schema,
                    table=self.test_table
                ))


    def test_simple(self):
        structure_data = {'data_type': 'varchar', 'character_maximum_length': 30}
        response = self.__class__.client.put(
            '/api/v0/schema/{schema}/tables/{table}/columns/new_column'.format(
                schema=self.test_schema, table=self.test_table),
            data=json.dumps({'query': structure_data}),
            HTTP_AUTHORIZATION='Token %s' % self.__class__.token,
            content_type='application/json')

        self.assertEqual(response.status_code, 201,
                         response.json().get('reason', 'No reason returned'))

        response = self.__class__.client.get(
            '/api/v0/schema/{schema}/tables/{table}/'.format(
                schema=self.test_schema, table=self.test_table))
        new_structure = {
            'character_maximum_length': 30, 'numeric_scale': None,
            'dtd_identifier': '5', 'is_nullable': True,
            'datetime_precision': None, 'ordinal_position': 5,
            'data_type': 'character varying',
            'maximum_cardinality': None, 'is_updatable': True,
            'numeric_precision_radix': None,
            'interval_precision': None,
            'character_octet_length': 120,
            'numeric_precision': None, 'column_default': None,
            'interval_type': None
         }
        self.assertEqual(response.json()['columns']['new_column'], new_structure)