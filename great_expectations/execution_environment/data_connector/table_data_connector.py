import logging
from string import Template

from marshmallow import Schema, ValidationError, fields, post_load

from great_expectations.exceptions import (BatchKwargsError,
                                           GreatExpectationsError)
from great_expectations.execution_environment.types import \
    SqlAlchemyDatasourceTableBatchKwargs

from .data_connector import DataConnector

logger = logging.getLogger(__name__)

try:
    import sqlalchemy
    from sqlalchemy import create_engine
    from sqlalchemy.engine import reflection
except ImportError:
    sqlalchemy = None
    create_engine = None
    reflection = None
    logger.debug("Unable to import sqlalchemy.")


class AssetConfigurationSchema(Schema):
    table = fields.Str()
    schema = fields.Str()

    @post_load
    def make_asset_configuration(self, data, **kwargs):
        return AssetConfiguration(**data)


class AssetConfiguration(object):
    def __init__(self, table, schema=None):
        self.__table = table
        self.__schema = schema

    @property
    def table(self):
        return self.__table

    @property
    def schema(self):
        return self.__schema


assetConfigurationSchema = AssetConfigurationSchema()


class TableDataConnector(DataConnector):
    """Provide access to already materialized tables or views in a database.

    TableDataConnector can be used to define specific data asset names that take and substitute parameters,
    for example to support referring to the same data asset but with different schemas depending on provided
    batch_kwargs.

    The python template language is used to substitute table name portions. For example, consider the
    following configurations::

        my_generator:
          class_name: TableDataConnector
          assets:
            my_table:
              schema: $schema
              table: my_table


    In that case, the asset my_datasource/my_generator/my_asset will refer to a table called my_table in a schema
    defined in batch_kwargs.

    """

    recognized_batch_definition_keys = {
        "data_asset_name",
        "limit",
        "offset",
        "query_parameters",
    }

    def __init__(self, name="default", execution_environment=None, assets=None):
        super().__init__(name=name, execution_environment=execution_environment)
        if not assets:
            assets = {}
        try:
            self._assets = {
                asset_name: assetConfigurationSchema.load(asset_config)
                for (asset_name, asset_config) in assets.items()
            }
        except ValidationError as err:
            raise GreatExpectationsError(
                "Unable to load asset configuration in TableDataConnector '%s': "
                "validation error: %s." % (name, str(err))
            )

        if execution_environment is not None:
            self.engine = execution_environment.execution_engine.engine
            try:
                self.inspector = sqlalchemy.inspect(self.engine)

            except sqlalchemy.exc.OperationalError:
                logger.warning(
                    "Unable to create inspector from engine in batch kwargs generator '%s'"
                    % name
                )
                self.inspector = None

    def _get_iterator(
        self,
        data_asset_name,
        query_parameters=None,
        limit=None,
        offset=None,
        partition_id=None,
    ):
        batch_kwargs = None
        # First, we check if we have a configured asset
        if data_asset_name in self._assets:
            asset_config = self._assets[data_asset_name]
            try:
                if query_parameters is None:
                    query_parameters = {}
                table_name = Template(asset_config.table).substitute(query_parameters)
                schema_name = None
                if asset_config.schema is not None:
                    schema_name = Template(asset_config.schema).substitute(
                        query_parameters
                    )
            except KeyError:
                raise BatchKwargsError(
                    "Unable to generate batch kwargs for asset '"
                    + data_asset_name
                    + "': "
                    "missing template key",
                    {
                        "data_asset_name": data_asset_name,
                        "table_template": asset_config.table,
                        "schema_template": asset_config.schema,
                    },
                )
            batch_kwargs = SqlAlchemyDatasourceTableBatchKwargs(
                table=table_name, schema=schema_name
            )

        # If this is not a manually configured asset, we fall back to inspection of the database
        elif self.engine is not None and self.inspector is not None:
            split_data_asset_name = data_asset_name.split(".")
            if len(split_data_asset_name) == 2:
                schema_name = split_data_asset_name[0]
                if self.engine.dialect.name.lower() == "bigquery":
                    table_name = data_asset_name
                else:
                    table_name = split_data_asset_name[1]
            elif len(split_data_asset_name) == 1:
                schema_name = self.inspector.default_schema_name
                table_name = split_data_asset_name[0]
            else:
                raise ValueError(
                    "Table name must be of shape '[SCHEMA.]TABLE'. Passed: "
                    + split_data_asset_name
                )
            tables = self.inspector.get_table_names(schema=schema_name)
            try:
                tables.extend(self.inspector.get_view_names(schema=schema_name))
            except NotImplementedError:
                # Not implemented by bigquery dialect
                pass

            if table_name in tables:
                batch_kwargs = SqlAlchemyDatasourceTableBatchKwargs(
                    table=table_name, schema=schema_name
                )

        if batch_kwargs is not None:
            if partition_id is not None:
                logger.warning(
                    "table_generator cannot identify partitions; provided partition id will be recorded "
                    "only"
                )
                batch_kwargs["partition_id"] = partition_id
            if limit is not None:
                batch_kwargs["limit"] = limit
            if offset is not None:
                batch_kwargs["offset"] = offset
            return iter([batch_kwargs])

        # Otherwise, we return None
        return

    def get_available_data_asset_names(self):
        # TODO: limit and is_complete_list logic
        is_complete_list = True
        defined_assets = list(self._assets.keys())
        tables = []
        if self.engine is not None and self.inspector is not None:
            for schema_name in self.inspector.get_schema_names():
                known_information_schemas = [
                    "INFORMATION_SCHEMA",  # snowflake, mssql, mysql, oracle
                    "information_schema",  # postgres, redshift, mysql
                    "performance_schema",  # mysql
                    "sys",  # mysql
                    "mysql",  # mysql
                ]
                known_system_tables = ["sqlite_master"]  # sqlite
                if schema_name in known_information_schemas:
                    continue

                if self.engine.dialect.name.lower() == "bigquery":
                    tables.extend(
                        [
                            (table_name, "table")
                            for table_name in self.inspector.get_table_names(
                                schema=schema_name
                            )
                            if table_name not in known_system_tables
                        ]
                    )
                else:
                    tables.extend(
                        [
                            (table_name, "table")
                            if self.inspector.default_schema_name == schema_name
                            else (schema_name + "." + table_name, "table")
                            for table_name in self.inspector.get_table_names(
                                schema=schema_name
                            )
                            if table_name not in known_system_tables
                        ]
                    )
                try:
                    tables.extend(
                        [
                            (table_name, "view")
                            if self.inspector.default_schema_name == schema_name
                            else (schema_name + "." + table_name, "view")
                            for table_name in self.inspector.get_view_names(
                                schema=schema_name
                            )
                            if table_name not in known_system_tables
                        ]
                    )
                except NotImplementedError:
                    # Not implemented by bigquery dialect
                    pass

        return {"names": defined_assets + tables, "is_complete_list": is_complete_list}

    def _build_batch_kwargs(self, batch_parameters):
        return next(
            self._get_iterator(
                data_asset_name=batch_parameters.get("data_asset_name"),
                query_parameters=batch_parameters.get("query_parameters", {}),
                limit=batch_parameters.get("limit"),
                offset=batch_parameters.get("offset"),
            )
        )

    def get_available_partition_ids(self, data_asset_name=None):
        if not data_asset_name:
            raise ValueError("Please provide a data_asset_name.")

        raise BatchKwargsError(
            "TableDataConnector cannot identify partitions, however any existing table may"
            "already be referenced by accessing a data_asset with the name of the "
            "table or of the form SCHEMA.TABLE",
            {},
        )