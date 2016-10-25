'''Abstract base and mixin classes for database models.'''
import os
import logging
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy import func
from sqlalchemy.schema import DropTable, CreateTable
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.dialects import registry
from abc import ABCMeta
from abc import abstractmethod
from abc import abstractproperty

from tmlib import utils

logger = logging.getLogger(__name__)


class DeclarativeABCMeta(DeclarativeMeta, ABCMeta):

    '''Metaclass for declarative base classes.'''

    def __init__(self, name, bases, d):
        distribute_by = (
            d.pop('__distribute_by_hash__', None) or
            getattr(self, '__bind_key__', None)
        )
        DeclarativeMeta.__init__(self, name, bases, d)
        if hasattr(self, '__table__'):
            if distribute_by is not None:
                column_names = [c.name for c in self.__table__.columns]
                if distribute_by not in column_names:
                    raise ValueError(
                        'Hash for PostgresXL distribution "%s" '
                        'is not a column of table "%s"'
                        % (distribute_by, self.__table__.name)
                    )
                self.__table__.info['distribute_by_hash'] = distribute_by
            else:
                self.__table__.info['distribute_by_replication'] = True


_MainBase = declarative_base(
    name='MainBase', metaclass=DeclarativeABCMeta
)

_ExperimentBase = declarative_base(
    name='ExperimentBase', metaclass=DeclarativeABCMeta
)


class DateMixIn(object):

    '''Mixin class to automatically add columns with datetime stamps to a
    database table.

    Attributes
    ----------
    created_at: datetime.datetime
        date and time when the row was inserted into the column
    updated_at: datetime.datetime
        date and time when the row was last updated
    '''

    created_at = Column(
        DateTime, default=func.now()
    )
    updated_at = Column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class IdMixIn(object):

    '''Mixin class to automatically add an ID column to a database table
    with primary key constraint.

    Attributes
    ----------
    id: int
        unique identifier number
    '''

    id = Column(Integer, primary_key=True)

    @property
    def hash(self):
        '''str: encoded `id`'''
        return utils.encode_pk(self.id)


class MainModel(_MainBase, IdMixIn):

    '''Abstract base class for models of the main database.'''

    __abstract__ = True


class ExperimentModel(_ExperimentBase, IdMixIn):

    '''Abstract base class for models of an experiment-specific database.'''

    __abstract__ = True


class FileSystemModel(ExperimentModel):

    '''Abstract base class for model classes, which refer to data
    stored on disk outside of the database.

    Devired classes must implement :meth:`location` as :func:`sqlalchemy.ext.hybrid.hyprid_property`
    '''

    __abstract__ = True

    _location = Column('location', String(200))

    @abstractproperty
    def location(self):
        '''str: location of the file/directory/container'''
        pass

    @location.setter
    def location(self, value):
        self._location = value


class DirectoryModel(FileSystemModel):

    '''Abstract base class for model classes, which refer to data
    stored in directories on disk.
    '''

    __abstract__ = True


class FileModel(FileSystemModel):

    '''Abstract base class for model classes, which refer to data
    stored in files on disk.
    '''

    __abstract__ = True

    @property
    def format(self):
        '''str: file extension, e.g. ".tif" or ".jpg"'''
        return os.path.splitext(self.name)[1]

    @abstractmethod
    def get(self):
        pass

    @abstractmethod
    def put(self, data):
        pass


registry.register('postgresql.xl', 'tmlib.models.dialect', 'PGXLDialect_psycopg2')


@compiles(DropTable, 'postgresql')
def _compile_drop_table(element, compiler, **kwargs):
    table = element.element
    logger.debug('drop table "%s" with cascade', table.name)
    return compiler.visit_drop_table(element) + ' CASCADE'

