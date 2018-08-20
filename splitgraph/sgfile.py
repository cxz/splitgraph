import re
from hashlib import sha256

from parsimonious.grammar import Grammar

from splitgraph.commands import init, commit, checkout, import_tables, clone, unmount
from splitgraph.constants import SplitGraphException
from splitgraph.meta_handler import get_current_head, get_canonical_snap_id, get_tagged_id
from splitgraph.pg_replication import _replication_slot_exists

SGFILE_GRAMMAR = Grammar(r"""
    commands = space command space (newline space command space)*
    command = comment / output / import / sql
    comment = space "#" non_newline
    output = "OUTPUT" space mountpoint space image_hash?
    import = "FROM" space (conn_string space)? mountpoint (":" tag)? space "IMPORT" space tables
    sql = "SQL" space sql_statement
    
    table = (table_name space "AS" space table_alias) / table_name
    tables = table space ("," space table)*
    
    image_hash = ~"[0-9a-f]*"i
    mountpoint = identifier
    table_name = identifier
    table_alias = identifier
    tag = identifier
    sql_statement = non_newline
    
    newline = ~"\n*"
    non_newline = ~"[^\n]*"
    conn_string = ~"\S+:\S+@.+:\d+\/\S+"
    identifier = ~"[_a-zA-Z0-9\-]*"
    space = ~"\s*"
""")


def _canonicalize(sql):
    return ' '.join(sql.lower().split())


def _combine_hashes(hashes):
    return sha256(''.join(hashes).encode('ascii')).hexdigest()


def preprocess(commands, params={}):
    # Also replaces all $PARAM in the sgfile text with the params in the dictionary.
    commands = commands.replace("\\\n", "")
    for k, v in params.items():
        # Regex fun: if the replacement is '\1' + substitution (so that we put back the previously-consumed
        # possibly-escape character) and the substitution begins with a number (like an IP address),
        # then it gets treated as a match group (say, \11) which fails silently and adds weird gibberish
        # to the result.
        commands = re.sub(r'([^\\])\$' + re.escape(k), r'\g<1>' + v, commands, flags=re.MULTILINE)
    # Search for any unreplaced $-parameters
    unreplaced = set(re.findall(r'[^\\](\$\S+)', commands, flags=re.MULTILINE))
    if unreplaced:
        raise SplitGraphException("Unknown values for parameters " + ', '.join(unreplaced) + '!')
    # Finally, replace the escaped $
    return commands.replace('\\$', '$')


def parse_commands(commands, params={}):
    # Unpacks the parse tree into a list of command nodes.
    commands = preprocess(commands, params)
    parse_tree = SGFILE_GRAMMAR.parse(commands)
    return [n.children[0] for n in _extract_nodes(parse_tree, ['command']) if n.children[0].expr_name != 'comment']


def _extract_nodes(node, types):
    # Crawls the parse tree and only extracts nodes of given types.
    # Doesn't crawl further down if it reaches a required type.
    if node.expr_name in types:
        return [node]
    result = []
    for child in node.children:
        result.extend(_extract_nodes(child, types))
    return result


def _parse_table_alias(table_node):
    # Extracts the table name and its alias from the parse tree.
    table_name_alias = _extract_nodes(table_node, ['identifier'])

    table_name = table_name_alias[0].match.group(0)
    if len(table_name_alias) > 1:
        table_alias = table_name_alias[1].match.group(0)
        return table_name, table_alias
    return table_name, table_name


def _extract_all_table_aliases(node):
    table_names = []
    table_aliases = []

    for table in _extract_nodes(node, ['table']):
        tn, ta = _parse_table_alias(table)
        table_names.append(tn)
        table_aliases.append(ta)

    return table_names, table_aliases


def _checkout_or_calculate_layer(conn, output, image_hash, calc_func):
    # Have we already calculated this hash?
    try:
        checkout(conn, output, image_hash)
        print("Using the cache.")
    except SplitGraphException:
        calc_func()


def execute_commands(conn, commands, params={}):
    output = None

    node_list = parse_commands(commands, params=params)
    for i, node in enumerate(node_list):
        print("\n-> %d/%d %s" % (i + 1, len(node_list), node.text))
        if node.expr_name == 'output':
            interesting_nodes = _extract_nodes(node, ['identifier', 'image_hash'])

            output = interesting_nodes[0].match.group(0)
            print("Committing results to mountpoint %s" % output)
            try:
                get_current_head(conn, output)
            except SplitGraphException:
                # Output doesn't exist, create it.
                init(conn, output)

            # By default, use the current output HEAD. Check if it's overridden.
            if len(interesting_nodes) > 1:
                output_head = get_canonical_snap_id(conn, output, interesting_nodes[1].match.group(0))
                checkout(conn, output, output_head)
        elif node.expr_name == 'import':
            interesting_nodes = _extract_nodes(node, ['conn_string', 'identifier', 'tables'])
            if interesting_nodes[0].expr_name == 'conn_string':
                is_remote = True
                conn_string = interesting_nodes[0].match.group(0)
                mountpoint = interesting_nodes[1].match.group(0)
            else:
                is_remote = False
                mountpoint = interesting_nodes[0].match.group(0)

            if len(interesting_nodes) == 4:
                tag = interesting_nodes[-2].match.group(0)
            else:
                tag = 'latest'

            table_names, table_aliases = _extract_all_table_aliases(interesting_nodes[-1])
            # Don't use the actual routine here as we want more control: clone the remote repo in order to turn
            # the tag into an actual hash
            tmp_mountpoint = mountpoint + '_clone_tmp'
            try:
                if is_remote:
                    clone(conn, conn_string, mountpoint, tmp_mountpoint, download_all=False)
                # Calculate the hash of the new layer by combining the hash of the previous layer,
                # the hash of the source and all the table names/aliases getting imported.
                # This can be made more granular later by using, say, the object IDs of the tables
                # that are getting imported (so that if there's a new commit with some of the same objects,
                # we don't invalidate the downstream).
                if is_remote:
                    source_hash = get_tagged_id(conn, tmp_mountpoint, tag)
                else:
                    source_hash = get_tagged_id(conn, mountpoint, tag)
                output_head = get_current_head(conn, output)
                target_hash = _combine_hashes(
                    [output_head, source_hash] + [sha256(n.encode('utf-8')).hexdigest() for n in
                                                  table_names + table_aliases])

                print('%s:%s -> %s' % (output, output_head[:12], target_hash[:12]))

                def _calc():
                    print("Importing tables %r:%s from %s into %s" % (
                        table_names, source_hash[:12], mountpoint, output))
                    import_tables(conn, tmp_mountpoint if is_remote else mountpoint, table_names, output, table_aliases,
                                  image_hash=source_hash, target_hash=target_hash)

                _checkout_or_calculate_layer(conn, output, target_hash, _calc)
            finally:
                unmount(conn, tmp_mountpoint)
        elif node.expr_name == 'sql':
            if output is None:
                raise SplitGraphException(
                    "Error: no OUTPUT specified. OUTPUT mountpoint is snapshot during file execution.")
            # Calculate the hash of the layer we are trying to create.
            # Since we handle the "input" hashing in the import step, we don't need to care about the sources here.
            # Later on, we could enhance the caching and base the hash of the command on the hashes of objects that
            # definitely go there as sources.
            sql_command = _canonicalize(_extract_nodes(node, ['non_newline'])[0].text)
            output_head = get_current_head(conn, output)
            target_hash = _combine_hashes([output_head, sha256(sql_command.encode('utf-8')).hexdigest()])

            print('%s:%s -> %s' % (output, output_head[:12], target_hash[:12]))

            def _calc():
                print("Executing SQL...")
                with conn.cursor() as cur:
                    # Make sure we'll record the actual change.
                    assert _replication_slot_exists(conn)
                    # Execute all queries against the output by default.
                    cur.execute("SET search_path TO %s", (output,))
                    cur.execute(sql_command)
                    cur.execute("SET search_path TO public")
                commit(conn, output, target_hash, comment=sql_command)

            _checkout_or_calculate_layer(conn, output, target_hash, _calc)
