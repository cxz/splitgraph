import json
import subprocess
from decimal import Decimal

import pytest

from splitgraph.config import PG_PWD, PG_USER
from splitgraph.core._common import parse_connection_string, serialize_connection_string
from splitgraph.core.engine import repository_exists
from splitgraph.core.repository import Repository
from splitgraph.engine import switch_engine

try:
    # python 3.4+ should use builtin unittest.mock not mock package
    from unittest.mock import patch
except ImportError:
    from mock import patch

from click.testing import CliRunner

from splitgraph._data.registry import get_published_info
from splitgraph.commandline import *
from splitgraph.commandline._common import image_spec_parser
from splitgraph.hooks.mount_handlers import get_mount_handlers
from test.splitgraph.conftest import PG_MNT, MG_MNT, OUTPUT, add_multitag_dataset_to_engine, SPLITFILE_ROOT, \
    REMOTE_ENGINE


def test_image_spec_parsing():
    assert image_spec_parser()('test/pg_mount') == (Repository('test', 'pg_mount'), 'latest')
    assert image_spec_parser(default='HEAD')('test/pg_mount') == (Repository('test', 'pg_mount'), 'HEAD')
    assert image_spec_parser()('test/pg_mount:some_tag') == (Repository('test', 'pg_mount'), 'some_tag')
    assert image_spec_parser()('pg_mount') == (Repository('', 'pg_mount'), 'latest')
    assert image_spec_parser()('pg_mount:some_tag') == (Repository('', 'pg_mount'), 'some_tag')
    assert image_spec_parser(default='HEAD')('pg_mount:some_tag') == (Repository('', 'pg_mount'), 'some_tag')


def test_conn_string_parsing():
    assert parse_connection_string("user:pwd@host.com:1234/db") == ("host.com", 1234, "user", "pwd", "db")
    with pytest.raises(ValueError):
        parse_connection_string("abcdef@blabla/blabla")


def test_conn_string_serialization():
    assert serialize_connection_string("host.com", 1234, "user", "pwd", "db") == "user:pwd@host.com:1234/db"


def test_commandline_basics(local_engine_with_pg_and_mg):
    runner = CliRunner()

    # sgr status
    result = runner.invoke(status_c, [])
    assert PG_MNT.to_schema() in result.output
    assert MG_MNT.to_schema() in result.output
    old_head = PG_MNT.get_head()
    assert old_head in result.output

    # sgr sql
    runner.invoke(sql_c, ["INSERT INTO \"test/pg_mount\".fruits VALUES (3, 'mayonnaise')"])
    runner.invoke(sql_c, ["CREATE TABLE \"test/pg_mount\".mushrooms (mushroom_id integer, name varchar)"])
    runner.invoke(sql_c, ["DROP TABLE \"test/pg_mount\".vegetables"])
    runner.invoke(sql_c, ["DELETE FROM \"test/pg_mount\".fruits WHERE fruit_id = 1"])
    result = runner.invoke(sql_c, ["SELECT * FROM \"test/pg_mount\".fruits"])
    assert "(3, 'mayonnaise')" in result.output
    assert "(1, 'apple')" not in result.output
    # Test schema search_path
    result = runner.invoke(sql_c, ["--schema", "test/pg_mount", "SELECT * FROM fruits"])
    assert "(3, 'mayonnaise')" in result.output
    assert "(1, 'apple')" not in result.output

    def check_diff(args):
        result = runner.invoke(diff_c, [str(a) for a in args])
        assert "added 1 row" in result.output
        assert "removed 1 row" in result.output
        assert "vegetables: table removed"
        assert "mushrooms: table added"
        result = runner.invoke(diff_c, [str(a) for a in args] + ['-v'])
        assert "(3, 'mayonnaise'): +" in result.output
        assert "(1, 'apple'): -" in result.output

    # sgr diff, HEAD -> current staging (0-param)
    check_diff([PG_MNT])

    # sgr commit (with an extra snapshot
    result = runner.invoke(commit_c, [str(PG_MNT), '-m', 'Test commit', '-s'])
    assert result.exit_code == 0
    new_head = PG_MNT.get_head()
    assert new_head != old_head
    assert PG_MNT.get_image(new_head).parent_id == old_head
    assert new_head[:10] in result.output

    # sgr diff, old head -> new head (2-param), truncated hashes
    # technically these two hashes have a 2^(-20*4) = a 8e-25 chance of clashing but let's not dwell on that
    check_diff([PG_MNT, old_head[:20], new_head[:20]])

    # sgr diff, just the new head -- assumes the diff on top of the old head.
    check_diff([PG_MNT, new_head[:20]])

    # sgr diff, just the new head -- assumes the diff on top of the old head.
    check_diff([PG_MNT, new_head[:20]])

    # sgr diff, reverse order -- actually checks the two tables out and materializes them since there isn't a
    # path of DIFF objects between them.
    result = runner.invoke(diff_c, [str(PG_MNT), new_head[:20], old_head[:20]])
    assert "added 1 row" in result.output
    assert "removed 1 row" in result.output
    assert "vegetables: table removed"
    assert "mushrooms: table added"
    result = runner.invoke(diff_c, [str(PG_MNT), new_head[:20], old_head[:20], '-v'])
    # Since the images were flipped, here the result is that, since the row that was added
    # didn't exist in the first image, diff() thinks it was _removed_ and vice versa for the other row.
    assert "(3, 'mayonnaise'): -" in result.output
    assert "(1, 'apple'): +" in result.output

    # sgr status with the new commit
    result = runner.invoke(status_c, [str(PG_MNT)])
    assert 'test/pg_mount' in result.output
    assert 'Parent: ' + old_head in result.output
    assert new_head in result.output

    # sgr log
    result = runner.invoke(log_c, [str(PG_MNT)])
    assert old_head in result.output
    assert new_head in result.output
    assert "Test commit" in result.output

    # sgr log (tree)
    result = runner.invoke(log_c, [str(PG_MNT), '-t'])
    assert old_head[:5] in result.output
    assert new_head[:5] in result.output

    # sgr show the new commit
    result = runner.invoke(show_c, [str(PG_MNT) + ':' + new_head[:20], '-v'])
    assert "Test commit" in result.output
    assert "Parent: " + old_head in result.output
    fruit_objs = PG_MNT.get_image(new_head).get_table('fruits').objects
    mushroom_objs = PG_MNT.get_image(new_head).get_table('mushrooms').objects

    # Check verbose show also has the actual object IDs
    for o, of in fruit_objs + mushroom_objs:
        assert o in result.output


def test_upstream_management(local_engine_with_pg):
    runner = CliRunner()

    # sgr upstream test/pg_mount
    result = runner.invoke(upstream_c, ["test/pg_mount"])
    assert result.exit_code == 0
    assert "has no upstream" in result.output

    # Set to nonexistent engine
    result = runner.invoke(upstream_c, ["test/pg_mount", "--set", "dummy_engine", "test/pg_mount"])
    assert result.exit_code == 1
    assert "Remote engine 'dummy_engine' does not exist" in result.output

    # Set to existing engine (should we check the repo actually exists?)
    result = runner.invoke(upstream_c, ["test/pg_mount", "--set", "remote_engine", "test/pg_mount"])
    assert result.exit_code == 0
    assert "set to track remote_engine:test/pg_mount" in result.output

    # Get upstream again
    result = runner.invoke(upstream_c, ["test/pg_mount"])
    assert result.exit_code == 0
    assert "is tracking remote_engine:test/pg_mount" in result.output

    # Reset it
    result = runner.invoke(upstream_c, ["test/pg_mount", "--reset"])
    assert result.exit_code == 0
    assert "Deleted upstream for test/pg_mount" in result.output
    assert PG_MNT.get_upstream() is None

    # Reset it again
    result = runner.invoke(upstream_c, ["test/pg_mount", "--reset"])
    assert result.exit_code == 1
    assert "has no upstream" in result.output


def test_commandline_tag_checkout(local_engine_with_pg_and_mg):
    runner = CliRunner()
    # Do the quick setting up with the same commit structure
    old_head = PG_MNT.get_head()
    runner.invoke(sql_c, ["INSERT INTO \"test/pg_mount\".fruits VALUES (3, 'mayonnaise')"])
    runner.invoke(sql_c, ["CREATE TABLE \"test/pg_mount\".mushrooms (mushroom_id integer, name varchar)"])
    runner.invoke(sql_c, ["DROP TABLE \"test/pg_mount\".vegetables"])
    runner.invoke(sql_c, ["DELETE FROM \"test/pg_mount\".fruits WHERE fruit_id = 1"])
    runner.invoke(sql_c, ["SELECT * FROM \"test/pg_mount\".fruits"])
    result = runner.invoke(commit_c, [str(PG_MNT), '-m', 'Test commit'])
    assert result.exit_code == 0

    new_head = PG_MNT.get_head()

    # sgr tag <repo> <tag>: tags the current HEAD
    runner.invoke(tag_c, [str(PG_MNT), 'v2'])
    assert PG_MNT.resolve_image('v2') == new_head

    # sgr tag <repo>:imagehash <tag>:
    runner.invoke(tag_c, [str(PG_MNT) + ':' + old_head[:10], 'v1'])
    assert PG_MNT.resolve_image('v1') == old_head

    # sgr tag <mountpoint> with the same tag -- expect an error
    result = runner.invoke(tag_c, [str(PG_MNT), 'v1'])
    assert result.exit_code != 0
    assert 'Tag v1 already exists' in str(result.exc_info)

    # list tags
    result = runner.invoke(tag_c, [str(PG_MNT)])
    assert old_head[:12] + ': v1' in result.output
    assert new_head[:12] + ': HEAD, v2' in result.output

    # List tags on a single image
    result = runner.invoke(tag_c, [str(PG_MNT) + ':' + old_head[:20]])
    assert 'v1' in result.output
    assert 'HEAD, v2' not in result.output

    # Checkout by tag
    runner.invoke(checkout_c, [str(PG_MNT) + ':v1'])
    assert PG_MNT.get_head() == old_head

    # Checkout by hash
    runner.invoke(checkout_c, [str(PG_MNT) + ':' + new_head[:20]])
    assert PG_MNT.get_head() == new_head

    # Checkout with uncommitted changes
    runner.invoke(sql_c, ["INSERT INTO \"test/pg_mount\".fruits VALUES (3, 'mayonnaise')"])
    result = runner.invoke(checkout_c, [str(PG_MNT) + ':v1'])
    assert result.exit_code != 0
    assert "test/pg_mount has pending changes!" in str(result.exc_info)

    result = runner.invoke(checkout_c, [str(PG_MNT) + ':v1', '-f'])
    assert result.exit_code == 0
    assert not PG_MNT.has_pending_changes()

    # uncheckout
    result = runner.invoke(checkout_c, [str(PG_MNT), '-u', '-f'])
    assert result.exit_code == 0
    assert PG_MNT.get_head(raise_on_none=False) is None
    assert not get_engine().schema_exists(str(PG_MNT))

    # Delete the tag -- check the help entry correcting the command
    result = runner.invoke(tag_c, ['--remove', str(PG_MNT), 'v1'])
    assert result.exit_code != 0
    assert '--remove test/pg_mount:TAG_TO_DELETE' in result.output

    result = runner.invoke(tag_c, ['--remove', str(PG_MNT) + ':' + 'v1'])
    assert result.exit_code == 0
    assert PG_MNT.resolve_image('v1', raise_on_none=False) is None


def test_misc_mountpoint_management(local_engine_with_pg_and_mg):
    runner = CliRunner()

    result = runner.invoke(status_c)
    assert str(PG_MNT) in result.output
    assert str(MG_MNT) in result.output

    # sgr rm -y test/pg_mount (no prompting)
    result = runner.invoke(rm_c, [str(MG_MNT), '-y'])
    assert result.exit_code == 0
    assert not repository_exists(MG_MNT)

    # sgr cleanup
    result = runner.invoke(cleanup_c)
    assert "Deleted 1 physical object(s)" in result.output

    # sgr init
    result = runner.invoke(init_c, ['output'])
    assert "Initialized empty repository output" in result.output
    assert repository_exists(OUTPUT)

    # sgr mount
    result = runner.invoke(mount_c, ['mongo_fdw', str(MG_MNT), '-c', 'originro:originpass@mongoorigin:27017',
                                     '-o', json.dumps({"stuff": {
            "db": "origindb",
            "coll": "stuff",
            "schema": {
                "name": "text",
                "duration": "numeric",
                "happy": "boolean"
            }}})])
    assert result.exit_code == 0
    assert get_engine().run_sql("SELECT duration from test_mg_mount.stuff WHERE name = 'James'") == [(Decimal(2),)]


def test_import(local_engine_with_pg_and_mg):
    runner = CliRunner()
    head = PG_MNT.get_head()

    # sgr import mountpoint, table, target_mountpoint (3-arg)
    result = runner.invoke(import_c, [str(MG_MNT), 'stuff', str(PG_MNT)])
    assert result.exit_code == 0
    new_head = PG_MNT.get_head()
    assert PG_MNT.get_image(new_head).get_table('stuff')
    assert not PG_MNT.get_image(head).get_table('stuff')

    # sgr import with alias
    result = runner.invoke(import_c, [str(MG_MNT), 'stuff', str(PG_MNT), 'stuff_copy'])
    assert result.exit_code == 0
    new_new_head = PG_MNT.get_head()
    assert PG_MNT.get_image(new_new_head).get_table('stuff_copy')
    assert not PG_MNT.get_image(new_head).get_table('stuff_copy')

    # sgr import with alias and custom image hash
    get_engine().run_sql("DELETE FROM test_mg_mount.stuff")
    new_mg_head = MG_MNT.commit()

    result = runner.invoke(import_c, [str(MG_MNT) + ':' + new_mg_head, 'stuff', str(PG_MNT), 'stuff_empty'])
    assert result.exit_code == 0
    new_new_new_head = PG_MNT.get_head()
    assert PG_MNT.get_image(new_new_new_head).get_table('stuff_empty')
    assert not PG_MNT.get_image(new_new_head).get_table('stuff_empty')
    assert PG_MNT.run_sql("SELECT * FROM stuff_empty") == []

    # sgr import with query, no alias
    result = runner.invoke(import_c, [str(MG_MNT) + ':' + new_mg_head, 'SELECT * FROM stuff', str(PG_MNT)])
    assert result.exit_code != 0
    assert 'TARGET_TABLE is required' in str(result.stdout)


def test_pull_push(local_engine_empty, remote_engine):
    runner = CliRunner()

    result = runner.invoke(clone_c, [str(PG_MNT)])
    assert result.exit_code == 0
    assert repository_exists(PG_MNT)

    remote_engine.run_sql("INSERT INTO \"test/pg_mount\".fruits VALUES (3, 'mayonnaise')")
    with switch_engine(REMOTE_ENGINE):
        remote_engine_head = PG_MNT.commit()

    result = runner.invoke(pull_c, [str(PG_MNT)])
    assert result.exit_code == 0
    PG_MNT.checkout(remote_engine_head)

    PG_MNT.run_sql("INSERT INTO fruits VALUES (4, 'mustard')")
    local_head = PG_MNT.commit()

    with switch_engine(REMOTE_ENGINE):
        assert not PG_MNT.get_image(local_head)
    result = runner.invoke(push_c, [str(PG_MNT), '-h', 'DB'])
    assert result.exit_code == 0
    assert PG_MNT.get_image(local_head).get_table('fruits')

    PG_MNT.get_image(local_head).tag('v1')
    PG_MNT.engine.commit()
    result = runner.invoke(publish_c, [str(PG_MNT), 'v1', '-r', SPLITFILE_ROOT + 'README.md'])
    assert result.exit_code == 0
    with switch_engine(REMOTE_ENGINE):
        image_hash, published_dt, deps, readme, schemata, previews = get_published_info(PG_MNT, 'v1')
    assert image_hash == local_head
    assert deps == []
    assert readme == "Test readme for a test dataset."
    assert schemata == {'fruits': [['fruit_id', 'integer', False],
                                   ['name', 'character varying', False]],
                        'vegetables': [['vegetable_id', 'integer', False],
                                       ['name', 'character varying', False]]}
    assert previews == {'fruits': [[1, 'apple'], [2, 'orange'], [3, 'mayonnaise'], [4, 'mustard']],
                        'vegetables': [[1, 'potato'], [2, 'carrot']]}


def test_splitfile(local_engine_empty, remote_engine):
    runner = CliRunner()

    result = runner.invoke(build_c, [SPLITFILE_ROOT + 'import_remote_multiple.splitfile',
                                     '-a', 'TAG', 'latest', '-o', 'output'])
    assert result.exit_code == 0
    assert local_engine_empty.run_sql("SELECT id, fruit, vegetable FROM output.join_table") \
           == [(1, 'apple', 'potato'), (2, 'orange', 'carrot')]

    # Test the sgr provenance command. First, just list the dependencies of the new image.
    result = runner.invoke(provenance_c, ['output:latest'])
    with switch_engine(REMOTE_ENGINE):
        assert 'test/pg_mount:%s' % PG_MNT.resolve_image('latest') in result.output

    # Second, output the full splitfile (-f)
    result = runner.invoke(provenance_c, ['output:latest', '-f'], catch_exceptions=False)
    with switch_engine(REMOTE_ENGINE):
        assert 'FROM test/pg_mount:%s IMPORT' % PG_MNT.resolve_image('latest') in result.output
    assert 'SQL CREATE TABLE join_table AS SELECT' in result.output


def test_splitfile_rebuild_update(local_engine_empty, remote_engine):
    add_multitag_dataset_to_engine(remote_engine)
    runner = CliRunner()

    result = runner.invoke(build_c, [SPLITFILE_ROOT + 'import_remote_multiple.splitfile',
                                     '-a', 'TAG', 'v1', '-o', 'output'])
    assert result.exit_code == 0

    # Rerun the output:latest against v2 of the test/pg_mount
    result = runner.invoke(rebuild_c, ['output:latest', '--against', 'test/pg_mount:v2'])
    output_v2 = OUTPUT.get_head()
    assert result.exit_code == 0
    with switch_engine(REMOTE_ENGINE):
        v2 = PG_MNT.resolve_image('v2')
    assert OUTPUT.get_image(output_v2).provenance() == [(PG_MNT, v2)]

    # Now rerun the output:latest against the latest version of everything.
    # In this case, this should all resolve to the same version of test/pg_mount (v2) and not produce
    # any extra commits.
    curr_commits = OUTPUT.get_images()
    result = runner.invoke(rebuild_c, ['output:latest', '-u'])
    assert result.exit_code == 0
    assert output_v2 == OUTPUT.get_head()
    assert OUTPUT.get_images() == curr_commits


def test_mount_and_import(local_engine_empty):
    runner = CliRunner()
    try:
        # sgr mount
        result = runner.invoke(mount_c, ['mongo_fdw', 'tmp', '-c', 'originro:originpass@mongoorigin:27017',
                                         '-o', json.dumps({"stuff": {
                "db": "origindb",
                "coll": "stuff",
                "schema": {
                    "name": "text",
                    "duration": "numeric",
                    "happy": "boolean"
                }}})])
        assert result.exit_code == 0

        result = runner.invoke(import_c, ['tmp', 'stuff', str(MG_MNT)])
        assert result.exit_code == 0
        assert MG_MNT.get_image(MG_MNT.get_head()).get_table('stuff')

        result = runner.invoke(import_c, ['tmp', 'SELECT * FROM stuff WHERE duration > 10', str(MG_MNT),
                                          'stuff_query'])
        assert result.exit_code == 0
        assert MG_MNT.get_image(MG_MNT.get_head()).get_table('stuff_query')
    finally:
        Repository('', 'tmp').rm()


def test_rm_repositories(local_engine_with_pg, remote_engine):
    runner = CliRunner()

    # sgr rm test/pg_mount, say "no"
    result = runner.invoke(rm_c, [str(PG_MNT)], input='n\n')
    assert result.exit_code == 1
    assert "Repository test/pg_mount will be deleted" in result.output
    assert repository_exists(PG_MNT)

    # sgr rm test/pg_mount, say "yes"
    result = runner.invoke(rm_c, [str(PG_MNT)], input='y\n')
    assert result.exit_code == 0
    assert not repository_exists(PG_MNT)

    # sgr rm test/pg_mount -r remote_engine
    result = runner.invoke(rm_c, [str(PG_MNT), '-r', 'remote_engine'], input='y\n')
    assert result.exit_code == 0
    with switch_engine(REMOTE_ENGINE):
        assert not repository_exists(PG_MNT)


def test_rm_images(local_engine_with_pg, remote_engine):
    runner = CliRunner()

    # Play around with both engines for simplicity -- both have 2 images with 2 tags
    add_multitag_dataset_to_engine(remote_engine)
    add_multitag_dataset_to_engine(local_engine_with_pg)

    local_v1 = PG_MNT.resolve_image('v1')
    local_v2 = PG_MNT.resolve_image('v2')

    # Test deleting checked out image causes an error
    result = runner.invoke(rm_c, [str(PG_MNT) + ':v2'])
    assert result.exit_code != 0
    assert "do sgr checkout -u test/pg_mount" in str(result.exc_info)

    PG_MNT.uncheckout()

    # sgr rm test/pg_mount:v2, say "no"
    result = runner.invoke(rm_c, [str(PG_MNT) + ':v2'], input='n\n')
    assert result.exit_code == 1
    # Specify most of the output verbatim here to make sure it's not proposing
    # to delete more than needed (just the single image and the single v2 tag)
    assert "Images to be deleted:\n" + local_v2 + '\nTotal: 1\n\nTags to be deleted:\nv2\nTotal: 1' \
           in result.output
    # Since we cancelled the operation, 'v2' still remains.
    assert PG_MNT.resolve_image('v2') == local_v2
    assert PG_MNT.get_image(local_v2) is not None

    # Uncheckout the remote too (it's supposed to be bare anyway)
    with switch_engine(REMOTE_ENGINE):
        remote_v2 = PG_MNT.resolve_image('v2')
        PG_MNT.uncheckout()

    # sgr rm test/pg_mount:v2 -r remote_engine, say "yes"
    result = runner.invoke(rm_c, [str(PG_MNT) + ':v2', '-r', 'remote_engine'], input='y\n')
    assert result.exit_code == 0
    with switch_engine(REMOTE_ENGINE):
        assert PG_MNT.resolve_image('v2', raise_on_none=False) is None
        assert PG_MNT.get_image(remote_v2) is None

    # sgr rm test/pg_mount:v1 -y
    # Should delete both images since v2 depends on v1
    result = runner.invoke(rm_c, [str(PG_MNT) + ':v1', '-y'])
    assert result.exit_code == 0
    assert local_v2 in result.output
    assert local_v1 in result.output
    assert 'v1' in result.output
    assert 'v2' in result.output
    # One image remaining (the 00000.. base image)
    assert len(PG_MNT.get_images()) == 1


def test_mount_docstring_generation():
    runner = CliRunner()

    # General mount help: should have all the handlers autoregistered and listed
    result = runner.invoke(mount_c, ['--help'])
    assert result.exit_code == 0
    for handler_name in get_mount_handlers():
        assert handler_name in result.output

    # Test the reserved params (that we parse separately) don't make it into the help text
    # and that other function args from the docstring do.
    result = runner.invoke(mount_c, ['postgres_fdw', '--help'])
    assert result.exit_code == 0
    assert "mountpoint" not in result.output
    assert "remote_schema" in result.output


def test_prune(local_engine_with_pg, remote_engine):
    runner = CliRunner()
    # Two engines, two repos, two images in each (tagged v1 and v2, v1 is the parent of v2).
    add_multitag_dataset_to_engine(remote_engine)
    with switch_engine(REMOTE_ENGINE):
        PG_MNT.uncheckout()

    add_multitag_dataset_to_engine(local_engine_with_pg)

    # sgr prune test/pg_mount -- all images are tagged, nothing to do.
    result = runner.invoke(prune_c, [str(PG_MNT)])
    assert result.exit_code == 0
    assert "Nothing to do" in result.output

    # Delete tag v2 and run sgr prune -r remote_engine test/pg_mount, say "no": the image
    # that used to be 'v2' now isn't tagged so it will be a candidate for removal (but not the v1 image).
    with switch_engine(REMOTE_ENGINE):
        remote_v2 = PG_MNT.resolve_image('v2')
        PG_MNT.get_image(remote_v2).delete_tag('v2')
        remote_engine.commit()

    result = runner.invoke(prune_c, [str(PG_MNT), '-r', 'remote_engine'], input='n\n')
    assert result.exit_code == 1  # Because "n" aborted the command
    assert remote_v2 in result.output
    assert 'Total: 1' in result.output
    # Make sure the image still exists
    with switch_engine(REMOTE_ENGINE):
        assert PG_MNT.get_image(remote_v2)

    # Delete tag v1 and run sgr prune -r remote_engine -y test_pg_mount:
    # now both images aren't tagged so will get removed.
    with switch_engine(REMOTE_ENGINE):
        remote_v1 = PG_MNT.resolve_image('v1')
        PG_MNT.get_image(remote_v1).delete_tag('v1')
        remote_engine.commit()
    result = runner.invoke(prune_c, [str(PG_MNT), '-r', 'remote_engine', '-y'])
    assert result.exit_code == 0
    assert remote_v2 in result.output
    assert remote_v1 in result.output
    # 2 images + the 000... image
    assert 'Total: 3' in result.output
    with switch_engine(REMOTE_ENGINE):
        assert not PG_MNT.get_images()

    # Finally, delete both tags from the local engine and prune. Since there's still
    # a HEAD tag pointing to the ex-v2, nothing will actually happen.
    result = runner.invoke(prune_c, [str(PG_MNT), '-y'])
    assert "Nothing to do." in result.output
    # 2 images + the 000.. image
    assert len(PG_MNT.get_images()) == 3
    assert len(PG_MNT.get_all_hashes_tags()) == 3


def test_config_dumping():
    runner = CliRunner()

    # sgr config (normal, with passwords shielded)
    result = runner.invoke(config_c)
    assert result.exit_code == 0
    assert PG_PWD not in result.output
    assert "remote_engine:" in result.output
    assert ("SG_ENGINE_USER=%s" % PG_USER) in result.output
    assert "DUMMY=test.splitgraph.splitfile" in result.output
    assert "S3=splitgraph.hooks.s3" in result.output

    # sgr config -s (no password shielding)
    result = runner.invoke(config_c, ['-s'])
    assert result.exit_code == 0
    assert ("SG_ENGINE_USER=%s" % PG_USER) in result.output
    assert ("SG_ENGINE_PWD=%s" % PG_PWD) in result.output
    assert "remote_engine:" in result.output

    # sgr config -sc (no password shielding, output in config format)
    result = runner.invoke(config_c, ['-sc'])
    assert result.exit_code == 0
    assert ("SG_ENGINE_USER=%s" % PG_USER) in result.output
    assert ("SG_ENGINE_PWD=%s" % PG_PWD) in result.output
    assert "[remote: remote_engine]" in result.output
    assert "[defaults]" in result.output
    assert "[commands]" in result.output
    assert "[external_handlers]" in result.output
    assert "[mount_handlers]" in result.output
    assert "S3=splitgraph.hooks.s3" in result.output


def test_init_new_db():
    try:
        get_engine().delete_database('testdb')

        # CliRunner doesn't run in a brand new process and by that point PG_DB has propagated
        # through a few modules that are difficult to patch out, so let's just shell out.
        output = subprocess.check_output("SG_ENGINE_DB_NAME=testdb sgr init", shell=True, stderr=subprocess.STDOUT)
        output = output.decode('utf-8')
        assert "Creating database testdb" in output
        assert "Installing the audit trigger" in output
    finally:
        get_engine().delete_database('testdb')
