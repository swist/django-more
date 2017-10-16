
from enum import Enum
from contextlib import suppress
from collections import namedtuple

from django.db import IntegrityError
from django.db import models
from django.db.models.deletion import Collector
from django.db.migrations.operations.fields import Operation, FieldOperation, AlterField

from django_types import CustomTypeOperation
from .fields import EnumField

"""
    Use a symbol = value style as per Enum expectations.
    Where a value is the human readable or sensible value, and the symbol is the
     constant or programming flag to use.
    For readbility of the database values, the human readable values are used.
"""


class EnumState:
    @classmethod
    def values(cls):
        return [em.value for em in cls]

    @classmethod
    def values_set(cls):
        return set(cls.values())


def enum_state(values, name=None, app_label=None):
    """ Create an EnumState representing the values or Enum """
    if isinstance(values, type) and issubclass(values, Enum):
        if not name:
            name = values.__name__
        values = (em.value for em in values)
    elif not name:
        name = 'Unnamed Enum'
    e = Enum(name, [(v, v) for v in values], type=EnumState)
    e.Meta = type('Meta', (object,), {})
    e.Meta.app_label = app_label
    return e


class EnumOperation(CustomTypeOperation):
    # Override get fields to restrict to EnumFields
    @staticmethod
    def get_fields(state, db_type=None, field_type=EnumField):
        return CustomTypeOperation.get_fields(state, db_type, field_type)


class CreateEnum(EnumOperation):
    def __init__(self, db_type, values):
        # Values follow Enum functional API options to specify
        self.db_type = db_type
        self.values = values

    def describe(self):
        return 'Create enum type {db_type}'.format(db_type=self.db_type)

    def state_forwards(self, app_label, state):
        enum = enum_state(self.values, name=self.db_type, app_label=app_label)
        state.add_type(self.db_type, enum)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.features.requires_enum_declaration:
            enum = to_state.db_types[self.db_type]
            sql = schema_editor.sql_create_enum % {
                'enum_type': self.db_type,
                'values': ', '.join(['%s'] * len(enum))}
            schema_editor.execute(sql, enum.values())

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.features.requires_enum_declaration:
            sql = schema_editor.sql_drop_enum % {
                'enum_type': self.db_type}
            schema_editor.execute(sql)


class RemoveEnum(EnumOperation):
    def __init__(self, db_type):
        self.db_type = db_type

    def describe(self):
        return 'Remove enum type {db_type}'.format(db_type=self.db_type)

    def state_forwards(self, app_label, state):
        # TODO Add dependency checking and cascades
        state.remove_type(db_type)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.features.requires_enum_declaration:
            sql = schema_editor.sql_delete_enum % {
                'enum_type': self.db_type}
            schema_editor.execute(sql)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.features.requires_enum_declaration:
            enum = to_state.db_types[self.db_type]
            sql = schema_editor.sql_create_enum % {
                'enum_type': self.db_type,
                'values': ', '.join(['%s'] * len(enum))}
            schema_editor.execute(sql, enum.values())


class RenameEnum(EnumOperation):
    def __init__(self, old_type, new_type):
        self.old_db_type = old_type
        self.db_type = new_type

    def describe(self):
        return 'Rename enum type {old} to {new}'.format(
            old=self.old_db_type,
            new=self.db_type)

    def state_forwards(self, app_label, state):
        old_enum = state.db_types[self.old_db_type]
        enum = enum_state(old_enum, name=self.db_type, app_label=app_label)
        state.remove_type(self.old_db_type)
        state.add_type(self.db_type, enum)

        # Alter all fields using this enum
        for info in self.get_fields(state, self.old_db_type):
            changed_field = info.field.clone()
            changed_field.type_name = self.db_type
            info.model_state.fields[info.field_index] = (info.field_name, changed_field)
            state.reload_model(info.model_app_label, info.model_name)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.features.requires_enum_declaration:
            sql = schema_editor.sql_rename_enum % {
                'old_type': self.old_db_type,
                'enum_type': self.new_db_type}
            schema_editor.execute(sql)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self.old_db_type, self.new_db_type = self.new_db_type, self.old_db_type

        self.database_forwards(app_label, schema_editor, from_state, to_state)

        self.old_db_type, self.new_db_type = self.new_db_type, self.old_db_type


class AlterEnum(EnumOperation):
    temp_db_type = 'django_enum_temp'
    transition_db_type = 'django_enum_transition'

    def __init__(self, db_type, add_values=None, remove_values=None, on_delete=models.PROTECT):
        self.db_type = db_type
        self.add_values = set(add_values)
        self.remove_values = set(remove_values)
        self.on_delete = on_delete

    def describe(self):
        return 'Alter enum type {db_type},{added}{removed}'.format(
            db_type=self.db_type,
            added=' added {} value(s)'.format(len(self.add_values)) if self.add_values else '',
            removed=' removed {} value(s)'.format(len(self.remove_values)) if self.remove_values else '')

    def state_forwards(self, app_label, state):
        from_enum = state.db_types[self.db_type]
        to_enum = enum_state((from_enum.values_set() | self.add_values) - self.remove_values, name=self.db_type, app_label=app_label)
        state.add_type(self.db_type, to_enum)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        # Compare from_state and to_state and generate the appropriate ALTER commands
        pre_actions = []
        post_actions = []

        # Make sure ORM is ready for use
        from_state.clear_delayed_apps_cache()
        db_alias = schema_editor.connection.alias

        # Get field/model list
        fields = [
            (from_model, to_model, from_field, self.on_delete or field.on_delete)
            for info in self.get_fields(from_state, self.db_type)
            for from_model in [from_state.apps.get_model(info.model_app_label, info.model_name)]
            for from_field in [from_model._meta.get_field(info.field_name)]
            for to_model in [to_state.apps.get_model(info.model_app_label, info.model_name)]
            ]

        if self.remove_values:
            # The first post delete actions are to finalise the field types
            if schema_editor.connection.features.has_enum:
                if schema_editor.connection.features.requires_enum_declaration:
                    sql_alter_column_type = getattr(schema_editor,
                        'sql_alter_column_type_using',
                        schema_editor.sql_alter_column_type)
                    for (from_model, to_model, field, on_delete) in fields:
                        db_table = schema_editor.quote_name(from_model._meta.db_table)
                        db_field = schema_editor.quote_name(field.column)
                        sql = schema_editor.sql_alter_column % {
                            'table': db_table,
                            'changes': sql_alter_column_type % {
                                'column': db_field,
                                'type': self.temp_db_type,
                                'old_type': self.db_type}}
                        post_actions.append((sql, []))
                else:
                    for (from_model, to_model, field, on_delete) in fields:
                        db_table = schema_editor.quote_name(from_model._meta.db_table)
                        db_field = schema_editor.quote_name(field.column)
                        new_field = to_model._meta.get_field(field.name)
                        db_type, params = new_field.db_type(schema_editor.connection).paramatized
                        sql = schema_editor.sql_alter_column % {
                            'table': db_table,
                            'changes': schema_editor.sql_alter_column_type % {
                                'column': db_field,
                                'type': db_type}}
                        post_actions.append((sql, params))

            if self.add_values:
                # If there's the possibility of inconsistent actions, use transition type
                # ie, ADD VALUE 'new_val' and REMOVE VALUE 'rem_val' ON DELETE SET('new_val')
                # On DB's without enum support this isn't necessary as they are always CHAR
                transition_fields = [(from_model, field)
                    for (from_model, to_model, field, on_delete) in fields
                    if hasattr(on_delete, 'deconstruct')
                        or (on_delete == models.SET_DEFAULT and field.get_default() in self.add_values)]

                if transition_fields and schema_editor.connection.features.has_enum:
                    transition_values = to_state.db_types[self.db_type].values_set() | self.remove_values
                    transition_enum = enum_state(transition_values, 'transitional_enum')
                    if schema_editor.connection.features.requires_enum_declaration:
                        # Create transition type
                        sql = schema_editor.sql_create_enum % {
                            'enum_type': self.transition_db_type,
                            'choices': ', '.join(['%s'] * len(transition_values))}
                        pre_actions.append((sql, list(transition_values)))
                        # Drop transition type after done
                        sql = schema_editor.sql_delete_enum % {
                            'enum_type': self.transition_db_type}
                        post_actions.append((sql, []))

                    # Set fields to transition type
                    for (model, field) in transition_fields:
                        db_table = schema_editor.quote_name(model._meta.db_table)
                        db_field = schema_editor.quote_name(field.column)
                        field.type_name = self.transition_db_type
                        field.type_def = transition_enum
                        db_type, params = field.db_type(schema_editor.connection).paramatized
                        sql = schema_editor.sql_alter_column % {
                            'table': db_table,
                            'changes': schema_editor.sql_alter_column_type % {
                                'column': db_field,
                                'type': db_type}}
                        pre_actions.append((sql, params))

            if schema_editor.connection.features.requires_enum_declaration:
                # Create new type with temporary name
                to_enum = to_state.db_types[self.db_type]
                sql = schema_editor.sql_create_enum % {
                    'enum_type': self.temp_db_type,
                    'values': ', '.join(['%s'] * len(to_enum))}
                pre_actions.append((sql, to_enum.values()))
                # Clean up original type and rename new one to replace it
                sql = schema_editor.sql_delete_enum % {
                    'enum_type': self.db_type}
                post_actions.append((sql, []))
                sql = schema_editor.sql_rename_enum % {
                    'old_type': self.temp_db_type,
                    'enum_type': self.db_type}
                post_actions.append((sql, []))

        elif self.add_values:
            # Just adding values? Directly modify types, no hassle!
            if schema_editor.connection.features.requires_enum_declaration:
                for value in self.add_values:
                    sql = schema_editor.sql_alter_enum % {
                        'enum_type': self.db_type,
                        'value': '%s'}
                    post_actions.append((sql, [value]))
            elif schema_editor.connection.features.has_enum:
                for (from_model, to_model, field, on_delete) in fields:
                    db_table = schema_editor.quote_name(from_model._meta.db_table)
                    db_field = schema_editor.quote_name(field.column)
                    new_field = to_model._meta.get_field(field.name)
                    db_type, params = new_field.db_type(schema_editor.connection).paramatized

                    schema_editor.sql_alter_column % {
                        'table': db_table,
                        'changes': schema_editor.sql_alter_column_type % {
                            'column': db_field,
                            'type' : db_type}}
                    post_actions.append((sql, params))

        # Prepare database for data to be migrated
        for sql, params in pre_actions:
            schema_editor.execute(sql, params)

        # Apply all on_delete actions making data consistent with to_state values
        if self.remove_values:
            # Cheap hack to allow on_delete to work
            for (from_model, to_model, field, on_delete) in fields:
                field.remote_field = self

            # Records affected by on_delete action
            on_delete_gen = ((
                    field,
                    from_model.objects.using(db_alias).filter(
                        models.Q(('{}__in'.format(field.name), self.remove_values))
                    ).only('pk'),
                    on_delete)
                for (from_model, to_model, field, on_delete) in fields)

            # Validate on_delete constraints
            collector = Collector(using=db_alias)
            for (field, qs, on_delete) in on_delete_gen:
                if qs:
                    # Trigger the on_delete collection directly
                    on_delete(collector, field, qs, db_alias)
            collector.delete()

        # Apply final changes
        for sql, params in post_actions:
            schema_editor.execute(sql, params)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass