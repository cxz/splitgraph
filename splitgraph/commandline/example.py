"""
Command line routines generating example data / Splitfiles
"""
from random import getrandbits

import click

from splitgraph.core import Repository, repository_exists, ResultShape

_DEMO_TABLE_SIZE = 10
_DEMO_CHANGE_SIZE = 2
_DEMO_TEMPLATE = """# Import the table from the latest image in the first repository
FROM {repo_1} IMPORT {table_1} AS table_1

# Import the table from a certain image (passed in as a parameter) in the second repository
FROM {repo_2}:${{IMAGE_2}} IMPORT {table_2} AS table_2

# Create a join table
SQL CREATE TABLE result AS SELECT table_1.key, table_1.value AS value_1,\\
                                  table_2.value AS value_2\\
                           FROM table_1 JOIN table_2\\
                           ON table_1.key = table_2.key 
"""


@click.group(name='example')
def example():
    """Generate demo Splitgraph data."""
    pass


@click.command(name='generate')
@click.argument('repository', type=Repository.from_schema)
def generate_c(repository):
    """
    Generate a repository with some example data.

    :param repository: Repository to generate. Must not already exist.
    """
    if repository_exists(repository):
        raise click.ClickException("Repository %s already exists, use sgr rm to delete it!" % repository.to_schema())

    repository.init()
    repository.engine.create_table(repository.to_schema(), 'demo', [(1, 'key', 'integer', True),
                                                                    (2, 'value', 'varchar', False)])

    # Insert some random data
    repository.engine.run_sql_batch("INSERT INTO demo VALUES (%s, %s)", [(i, "%0.2x" % getrandbits(256))
                                                                         for i in range(_DEMO_TABLE_SIZE)],
                                    schema=repository.to_schema())
    image = repository.commit()
    print("Generated %s:%s with %s rows, image hash %s." % (repository.to_schema(), 'demo', _DEMO_TABLE_SIZE,
                                                            image.image_hash[:12]))


@click.command(name='alter')
@click.argument('repository', type=Repository.from_schema)
def alter_c(repository):
    """
    Alter the table in an example repository.

    This deletes the first 2 rows, updates the next 2 rows and adds 2 rows at the end. Doesn't create a new image.

    :param repository: Generated demo repository.
    """

    keys = repository.run_sql("SELECT key FROM demo", return_shape=ResultShape.MANY_ONE)
    last = repository.run_sql("SELECT MAX(key) FROM demo", return_shape=ResultShape.ONE_ONE)

    # Delete first N rows
    print("Deleting %d rows..." % _DEMO_CHANGE_SIZE)
    repository.engine.run_sql_batch("DELETE FROM demo WHERE key = %s", [(k,) for k in keys[:_DEMO_CHANGE_SIZE]],
                                    schema=repository.to_schema())

    # Update next N rows
    print("Updating %d rows..." % _DEMO_CHANGE_SIZE)
    repository.engine.run_sql_batch("UPDATE demo SET value = %s WHERE key = %s",
                                    [("%0.2x" % getrandbits(256), k)
                                     for k in keys[_DEMO_CHANGE_SIZE:_DEMO_CHANGE_SIZE * 2]],
                                    schema=repository.to_schema())

    # Insert rows at the end
    print("Adding %d rows..." % _DEMO_CHANGE_SIZE)
    repository.engine.run_sql_batch("INSERT INTO demo VALUES (%s, %s)",
                                    [(k, "%0.2x" % getrandbits(256))
                                     for k in range(last + 1, last + _DEMO_CHANGE_SIZE + 1)],
                                    schema=repository.to_schema())


@click.command(name='splitfile')
@click.argument('repository_1', type=Repository.from_schema)
@click.argument('repository_2', type=Repository.from_schema)
def splitfile_c(repository_1, repository_2):
    """
    Generate a sample Splitfile.

    The Splitfile imports tables from two example repositories (generated by sgr generate/alter) and runs a
    JOIN query on them.

    :param repository_1: First repository
    :param repository_2: Second repository
    """
    print(_DEMO_TEMPLATE.format(repo_1=repository_1.to_schema(), repo_2=repository_2.to_schema(),
                                table_1='demo', table_2='demo'))


example.add_command(generate_c)
example.add_command(alter_c)
example.add_command(splitfile_c)