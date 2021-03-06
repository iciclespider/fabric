"""
This module contains Fab's `main` method plus related subroutines.

`main` is executed as the command line ``fab`` program and takes care of
parsing options and commands, loading the user settings file, loading a
fabfile, and executing the commands given.

The other callables defined in this module are internal only. Anything useful
to individuals leveraging Fabric as a library, should be kept elsewhere.
"""

from operator import add
from optparse import OptionParser
import os
import sys
import textwrap

from fabric import api # For checking callables against the API 
from fabric.contrib import project, files # Ditto
from network import normalize
import state # For easily-mockable access to roles, env and etc
from state import commands, env_options, win32
from utils import abort, indent, warn


# One-time calculation of "all internal callables" to avoid doing this on every
# check of a given fabfile callable (in is_task()). Also generally useful for
# introspection (by e.g. our own fabfile which uses this info to help tweak
# some of the documentation)
_modules = [api, project, files]
internals = {} # Kept public for introspection
_internal_callables = [] # Convenience "cache" of just the callables.
for module in _modules:
    for name, item in vars(module).iteritems():
        if callable(item) and item not in _internal_callables:
            internals[name] = {
                'callable': item,
                # Need to use item.__module__ here to get REAL module;
                # also strip out first item which is 'fabric.'.
                'module_name': '.'.join(item.__module__.split('.')[1:])
            }
            _internal_callables.append(item)


def rc_path():
    """
    Return platform-specific file path for $HOME/<env.settings_file>.
    """
    if not win32:
        return os.path.expanduser("~/" + state.env.settings_file)
    else:
        from win32com.shell.shell import SHGetSpecialFolderPath
        from win32com.shell.shellcon import CSIDL_PROFILE
        return "%s/%s" % (
            SHGetSpecialFolderPath(0,CSIDL_PROFILE),
            state.env.settings_file
        )


def load_settings(path):
    """
    Take given file path and return dictionary of any key=value pairs found.
    """
    if os.path.exists(path):
        comments = lambda s: s and not s.startswith("#")
        settings = filter(comments, open(path, 'r'))
        return dict((k.strip(), v.strip()) for k, _, v in
            [s.partition('=') for s in settings])
    # Handle nonexistent or empty settings file
    return {}


def find_fabfile():
    """
    Attempt to locate a fabfile, either explicitly or by searching parent dirs.

    Uses the value of ``env.fabfile``, which defaults to ``fabfile.py``,
    as the target of the search. This may be overridden on the command line.

    If ``env.fabfile`` contains path elements other than a filename (e.g.
    ``../fabfile.py`` or ``dir1/dir2/other.py``) it will be treated as a file
    path and directly checked for existence without any sort of searching. When
    in this mode, tile-expansion will be applied, so one may refer to e.g.
    ``~/special_fabfile.py``.

    Either way, `find_fabfile` will return an absolute path if a file is found,
    or None otherwise.
    """
    if os.path.dirname(state.env.fabfile):
        expanded = os.path.expanduser(state.env.fabfile)
        if os.path.exists(expanded):
            return os.path.abspath(expanded)
        else:
            return None
    else:
        path = '.'
        # Stop before falling off root of filesystem (should be platform
        # agnostic)
        while os.path.split(os.path.abspath(path))[1]:
            joined = os.path.join(path, state.env.fabfile)
            if os.path.exists(joined):
                return os.path.abspath(joined)
            path = os.path.join('..', path)
        return None


def is_task(tup):
    """
    Takes (name, object) tuple, returns True if it's a non-Fab public callable.
    """
    name, func = tup
    return (
        callable(func)
        and (func not in _internal_callables)
        and not name.startswith('_')
    )


def load_fabfile(path):
    """
    Import given fabfile path and return dictionary of its public callables.
    """
    # Get directory and fabfile name
    directory, fabfile = os.path.split(path)
    # If the directory isn't in the PYTHONPATH, add it so our import will work
    added_to_path = False
    if directory not in sys.path:
        sys.path.insert(0, directory)
        added_to_path = True
    # Perform the import (trimming off the .py)
    imported = __import__(os.path.splitext(fabfile)[0])
    # Remove directory from path if we added it ourselves (just to be neat)
    if added_to_path:
        del sys.path[0]
    # Return dictionary of callables only (and don't include Fab operations or
    # underscored callables)
    return dict(filter(is_task, vars(imported).items()))


def parse_options():
    """
    Handle command-line options with optparse.OptionParser.

    Return list of arguments, largely for use in `parse_arguments`.
    """
    #
    # Initialize
    #

    parser = OptionParser(usage="fab [options] <command>[:arg1,arg2=val2,host=foo,hosts='h1;h2',...] ...")

    #
    # Define options that don't become `env` vars (typically ones which cause
    # Fabric to do something other than its normal execution, such as --version)
    #

    # Version number (optparse gives you --version but we have to do it
    # ourselves to get -V too. sigh)
    parser.add_option('-V', '--version',
        action='store_true',
        dest='show_version',
        default=False,
        help="show program's version number and exit"
    )

    # List Fab commands found in loaded fabfiles/source files
    parser.add_option('-l', '--list',
        action='store_true',
        dest='list_commands',
        default=False,
        help="print list of possible commands and exit"
    )

    # Display info about a specific command
    parser.add_option('-d', '--display',
        metavar='COMMAND',
        help="print detailed info about a given command and exit"
    )

    # TODO: specify nonstandard fabricrc file (and call load_settings() on it)
    # -c ? --config ? --fabricrc ? --rcfile ?
    # what are some commonly used flags for conf file specification?

    # TODO: explicitly specify "fabfile(s)" to load.
    # - Can specify multiple times
    # - Disables implicit "local" fabfile discovery/loading
    #   - or should the default be to "append" to the implicitly loaded fabfile?
    #   - either way, also add a flag to toggle that append/disable behavior
    # Flags: -f ? --fabfile ? --source ? what are some common flags?

    # TODO: old 'let' functionality, i.e. global env additions/overrides
    # maybe "set" as the keyword? i.e. -s / --set x=y
    # allow multiple times (like with tar --exclude)

    #
    # Add in options which are also destined to show up as `env` vars.
    #

    for option in env_options:
        parser.add_option(option)

    #
    # Finalize
    #

    # Return three-tuple of parser + the output from parse_args (opt obj, args)
    opts, args = parser.parse_args()
    return parser, opts, args


def list_commands():
    print("Available commands:\n")
    # Want separator between name, description to be straight col
    max_len = reduce(lambda a, b: max(a, len(b)), commands.keys(), 0)
    sep = '  '
    names = sorted(commands.keys())
    for name in names:
        output = None
        # Print first line of docstring
        func = commands[name]
        if func.__doc__:
            lines = filter(None, func.__doc__.splitlines())
            first_line = lines[0].strip()
            # Wrap it if it's longer than N chars
            wrapped = textwrap.wrap(first_line, 75 - (max_len + len(sep)))
            output = name.ljust(max_len) + sep + wrapped[0]
            for line in wrapped[1:]:
                output += '\n' + (' ' * max_len) + sep + line
        # Or nothing (so just the name)
        else:
            output = name
        print(indent(output))
    sys.exit(0)


def display_command(command):
    # Sanity check
    if command not in commands:
        abort("Command '%s' not found, exiting." % command)
    # Print out nicely presented docstring
    print("Displaying detailed information for command '%s':" % command)
    print('')
    print(indent(commands[command].__doc__, strip=True))
    print('')
    sys.exit(0)


def parse_arguments(arguments):
    """
    Parse string list into list of 4-tuples: command name, args, kwargs, hosts.

    Parses the given list of arguments into command names and, optionally,
    per-command args/kwargs. Per-command args are attached to the command name
    with a colon (``:``), are comma-separated, and may use a=b syntax for
    kwargs.  These args/kwargs are passed into the resulting command as normal
    Python args/kwargs.

    For example::

        $ fab do_stuff:a,b,c=d

    will result in the function call ``do_stuff(a, b, c=d)``.

    If ``host`` or ``hosts`` kwargs are given, they will be used to fill
    Fabric's host list (see `get_hosts`). ``hosts`` will override
    ``host`` if both are given.
    
    When using ``hosts`` in this way, one must use semicolons (``;``), and must
    thus quote the host list string to prevent shell interpretation.

    For example::

        $ fab ping_servers:hosts="a;b;c",foo=bar

    will result in Fabric's host list for the ``ping_servers`` command being set
    to ``['a', 'b', 'c']``.
    
    ``host`` and ``hosts`` are removed from the kwargs mapping at this point, so
    commands are not required to expect them. Thus, the resulting call of the
    above example would be ``ping_servers(foo=bar)``.
    """
    cmds = []
    for cmd in arguments:
        args = []
        kwargs = {}
        hosts = []
        if ':' in cmd:
            cmd, argstr = cmd.split(':', 1)
            for pair in argstr.split(','):
                k, _, v = pair.partition('=')
                if v:
                    # Catch, interpret host/hosts kwargs
                    if k in ['host', 'hosts']:
                        if k == 'host':
                            hosts = [v.strip()]
                        elif k == 'hosts':
                            hosts = [x.strip() for x in v.split(';')]
                    # Otherwise, record as usual
                    else:
                        kwargs[k] = v
                else:
                    args.append(k)
        cmds.append((cmd, args, kwargs, hosts))
    return cmds


def get_hosts(cli_hosts, command):
    """
    Return the host list the given command should be using.

    See :ref:`execution-model` for detailed documentation on how host lists are
    set.
    """
    # Command line takes precedence over anythin else.
    if cli_hosts:
        return cli_hosts
    # Decorator-specific hosts/roles go next and are unioned.
    func_hosts = getattr(command, 'hosts', [])
    func_roles = getattr(command, 'roles', [])
    if func_hosts or func_roles:
        role_hosts = (func_roles and reduce(add, [state.roles[y] for y in
            func_roles]) or [])
        return list(set(func_hosts + role_hosts))
    # Finally, the env is checked (which might contain values from the CLI or
    # from module-level code).
    if state.env.get('hosts'):
        return state.env.hosts
    # Empty list is the default if nothing is found.
    return []


def main():
    """
    Main command-line execution loop.
    """
    try:
        try:
            # Parse command line options
            parser, options, arguments = parse_options()

            # Update env with any overridden option values
            # NOTE: This needs to remain the first thing that occurs
            # post-parsing, since so many things hinge on the values in env.
            for option in env_options:
                state.env[option.dest] = getattr(options, option.dest)

            # Handle version number option
            if options.show_version:
                print "Fabric " + state.env.version
                sys.exit(0)

            # Load settings from user settings file, into shared env dict.
            state.env.update(load_settings(rc_path()))

            # Find local fabfile path or abort
            fabfile = find_fabfile()
            if not fabfile:
                abort("Couldn't find any fabfiles!")

            # Store absolute path to fabfile in case anyone needs it
            state.env.real_fabfile = fabfile

            # Load fabfile (which calls its module-level code, including
            # tweaks to env values) and put its commands in the shared commands
            # dict
            commands.update(load_fabfile(fabfile))

            # Abort if no commands found
            # TODO: continue searching for fabfiles if one we selected doesn't
            # contain any callables? Bit of an edge case, but still...
            if not commands:
                abort("Fabfile didn't contain any commands!")

            # Handle list-commands option (now that commands are loaded)
            if options.list_commands:
                list_commands()

            # Handle show (command-specific help) option
            if options.display:
                show_command(options.display)

            # If user didn't specify any commands to run, show help
            if not arguments:
                parser.print_help()
                sys.exit(0) # Or should it exit with error (1)?

            # Parse arguments into commands to run (plus args/kwargs/hosts)
            commands_to_run = parse_arguments(arguments)
            
            # Figure out if any specified names are invalid
            unknown_commands = []
            for tup in commands_to_run:
                if tup[0] not in commands:
                    unknown_commands.append(tup[0])

            # Abort if any unknown commands were specified
            if unknown_commands:
                abort("Command(s) not found:\n%s" \
                    % indent(unknown_commands))

            # At this point all commands must exist, so execute them in order.
            for name, args, kwargs, cli_hosts in commands_to_run:
                # Get callable by itself
                command = commands[name]
                # Set current command name (used for some error messages)
                state.env.command = name
                # Set host list
                hosts = get_hosts(cli_hosts, command)
                # If hosts found, execute the function on each host in turn
                for host in hosts:
                    username, hostname, port = normalize(host)
                    state.env.host_string = host
                    state.env.host = hostname
                    # Preserve user
                    prev_user = state.env.user
                    state.env.user = username
                    state.env.port = port
                    # Actually run command
                    commands[name](*args, **kwargs)
                    # Put old user back
                    state.env.user = prev_user
                # If no hosts found, assume local-only and run once
                if not hosts:
                    commands[name](*args, **kwargs)
            # If we got here, no errors occurred, so print a final note.
            print("\nDone.")
        finally:
            # TODO: explicit disconnect?
            pass
    except SystemExit:
        # a number of internal functions might raise this one.
        raise
    except KeyboardInterrupt:
        print >>sys.stderr, "\nStopped."
        sys.exit(1)
    except:
        sys.excepthook(*sys.exc_info())
        # we might leave stale threads if we don't explicitly exit()
        sys.exit(1)
    sys.exit(0)
