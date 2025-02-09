"""\
.. currentmodule:: pgtoolkit.conf

This module implements ``postgresql.conf`` file format. This is the same format
for ``recovery.conf``. The main entry point of the API is :func:`parse`. The
module can be used as a CLI script.


API Reference
-------------

.. autofunction:: parse
.. autofunction:: parse_string
.. autoclass:: Configuration
.. autoclass:: ParseError


Using as a CLI Script
---------------------

You can use this module to dump a configuration file as JSON object

.. code:: console

    $ python -m pgtoolkit.conf postgresql.conf | jq .
    {
      "lc_monetary": "fr_FR.UTF8",
      "datestyle": "iso, dmy",
      "log_rotation_age": "1d",
      "log_min_duration_statement": "3s",
      "log_lock_waits": true,
      "log_min_messages": "notice",
      "log_directory": "log",
      "port": 5432,
      "log_truncate_on_rotation": true,
      "log_rotation_size": 0
    }
    $

"""

from __future__ import annotations

import contextlib
import copy
import enum
import json
import pathlib
import re
import sys
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import IO, Any, NoReturn, Union
from warnings import warn

from ._helpers import JSONDateEncoder, open_or_return


class ParseError(Exception):
    """Error while parsing configuration content."""


class IncludeType(enum.Enum):
    """Include directive types.

    https://www.postgresql.org/docs/13/config-setting.html#CONFIG-INCLUDES
    """

    include_dir = enum.auto()
    include_if_exists = enum.auto()
    include = enum.auto()


def parse(fo: str | pathlib.Path | IO[str]) -> Configuration:
    """Parse a configuration file.

    The parser tries to return Python object corresponding to value, based on
    some heuristics. booleans, octal number, decimal integers and floating
    point numbers are parsed. Multiplier units like kB or MB are applyied and
    you get an int. Interval value like ``3s`` are returned as
    :class:`datetime.timedelta`.

    In case of doubt, the value is kept as a string. It's up to you to enforce
    format.

    Include directives are processed recursively, when 'fo' is a file path (not
    a file object). If some included file is not found a FileNotFoundError
    exception is raised. If a loop is detected in include directives, a
    RuntimeError is raised.

    :param fo: A line iterator such as a file-like object or a path.
    :returns: A :class:`Configuration` containing parsed configuration.

    """
    with open_or_return(fo) as f:
        conf = Configuration(getattr(f, "name", None))
        list(_consume(conf, f))

    return conf


def _consume(conf: Configuration, content: Iterable[str]) -> Iterator[None]:
    for include_path, include_type in conf.parse(content):
        yield from parse_include(conf, include_path, include_type)


def parse_string(string: str, source: str | None = None) -> Configuration:
    """Parse configuration data from a string.

    Optional *source* argument can be used to set the context path of built
    Configuration.

    :raises ParseError: if the string contains include directives referencing a relative
        path and *source* is unspecified.
    """
    conf = Configuration(source)
    conf.parse_string(string)
    return conf


def parse_include(
    conf: Configuration,
    path: pathlib.Path,
    include_type: IncludeType,
    *,
    _processed: set[pathlib.Path] | None = None,
) -> Iterator[None]:
    """Parse on include directive with 'path' value of type 'include_type' into
    'conf' object.
    """
    if _processed is None:
        _processed = set()

    def notfound(
        path: pathlib.Path, include_type: str, reference_path: str | None
    ) -> FileNotFoundError:
        ref = (
            f"{reference_path!r}" if reference_path is not None else "<string literal>"
        )
        return FileNotFoundError(
            f"{include_type} '{path}', included from {ref}, not found"
        )

    if not path.is_absolute():
        if not conf.path:
            raise ParseError(
                "cannot process include directives referencing a relative path"
            )
        relative_to = pathlib.Path(conf.path).absolute()
        assert relative_to.is_absolute()
        if relative_to.is_file():
            relative_to = relative_to.parent
        path = relative_to / path

    if include_type == IncludeType.include_dir:
        if not path.exists() or not path.is_dir():
            raise notfound(path, "directory", conf.path)
        for confpath in sorted(path.glob("*.conf")):
            if not confpath.name.startswith("."):
                yield from parse_include(
                    conf,
                    confpath,
                    IncludeType.include,
                    _processed=_processed,
                )

    elif include_type == IncludeType.include_if_exists:
        if path.exists():
            yield from parse_include(
                conf, path, IncludeType.include, _processed=_processed
            )

    elif include_type == IncludeType.include:
        if not path.exists():
            raise notfound(path, "file", conf.path)

        if path in _processed:
            raise RuntimeError(f"loop detected in include directive about '{path}'")
        _processed.add(path)

        subconf = Configuration(path=str(path))
        with path.open() as f:
            for sub_include_path, sub_include_type in subconf.parse(f):
                yield from parse_include(
                    subconf,
                    sub_include_path,
                    sub_include_type,
                    _processed=_processed,
                )
        conf.entries.update(subconf.entries)

    else:
        assert False, include_type  # pragma: nocover


MEMORY_MULTIPLIERS = {
    "kB": 1024,
    "MB": 1024 * 1024,
    "GB": 1024 * 1024 * 1024,
    "TB": 1024 * 1024 * 1024 * 1024,
}
_memory_re = re.compile(r"^\s*(?P<number>\d+)\s*(?P<unit>[kMGT]B)\s*$")
TIMEDELTA_ARGNAME = {
    "ms": "milliseconds",
    "s": "seconds",
    "min": "minutes",
    "h": "hours",
    "d": "days",
}
_timedelta_re = re.compile(r"^\s*(?P<number>\d+)\s*(?P<unit>ms|s|min|h|d)\s*$")

_minute = 60
_hour = 60 * _minute
_day = 24 * _hour
_timedelta_unit_map = [
    ("d", _day),
    ("h", _hour),
    # The space before 'min' is intentionnal. I find '1 min' more readable
    # than '1min'.
    (" min", _minute),
    ("s", 1),
]


Value = Union[str, bool, float, int, timedelta]


def parse_value(raw: str) -> Value:
    # Ref.
    # https://www.postgresql.org/docs/current/static/config-setting.html#CONFIG-SETTING-NAMES-VALUES

    quoted = False
    if raw.startswith("'"):
        if not raw.endswith("'"):
            raise ValueError(raw)
        # unquote value and unescape quotes
        raw = raw[1:-1].replace("''", "'").replace(r"\'", "'")
        quoted = True

    if raw.startswith("0") and raw != "0":
        try:
            int(raw, base=8)
            return raw
        except ValueError:
            pass

    m = _memory_re.match(raw)
    if m:
        return raw.strip()

    m = _timedelta_re.match(raw)
    if m:
        unit = m.group("unit")
        arg = TIMEDELTA_ARGNAME[unit]
        kwargs = {arg: int(m.group("number"))}
        return timedelta(**kwargs)

    if raw in ("true", "yes", "on"):
        return True

    if raw in ("false", "no", "off"):
        return False

    if not quoted:
        try:
            return int(raw)
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return raw

    return raw


def serialize_value(value: Value) -> str:
    # This is the reverse of parse_value.
    if isinstance(value, bool):
        value = "on" if value else "off"
    elif isinstance(value, str):
        # Only quote if not already quoted.
        if not (value.startswith("'") and value.endswith("'")):
            # Only double quotes, if not already done; we assume this is
            # done everywhere in the string or nowhere.
            if "''" not in value and r"\'" not in value:
                value = value.replace("'", "''")
            value = "'%s'" % value
    elif isinstance(value, timedelta):
        seconds = value.days * _day + value.seconds
        if value.microseconds:
            unit = " ms"
            value = seconds * 1000 + value.microseconds // 1000
        else:
            for unit, mod in _timedelta_unit_map:
                if seconds % mod:
                    continue
                value = seconds // mod
                break
        value = f"'{value}{unit}'"
    else:
        value = str(value)
    return value


_unspecified: Any = object()


@dataclass
class Entry:
    """Configuration entry, parsed from a line in the configuration file."""

    name: str
    _value: Value
    # _: KW_ONLY from Python 3.10
    commented: bool = False
    comment: str | None = None
    raw_line: str = field(default=_unspecified, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.raw_line is _unspecified:
            # We parse value only if not already parsed from a file
            if isinstance(self._value, str):
                self._value = parse_value(self._value)
            # Store the raw_line to track the position in the list of lines.
            self.raw_line = str(self) + "\n"

    @property
    def value(self) -> Value:
        return self._value

    @value.setter
    def value(self, value: str | Value) -> None:
        if isinstance(value, str):
            value = parse_value(value)
        self._value = value

    def serialize(self) -> str:
        return serialize_value(self.value)

    def __str__(self) -> str:
        line = "%(name)s = %(value)s" % dict(name=self.name, value=self.serialize())
        if self.comment:
            line += "  # " + self.comment
        if self.commented:
            line = "#" + line
        return line


class EntriesProxy(dict[str, Entry]):
    """Proxy object used during Configuration edition.

    >>> p = EntriesProxy(port=Entry('port', '5432'),
    ...                  shared_buffers=Entry('shared_buffers', '1GB'))

    Existing entries can be edited:

    >>> p['port'].value = '5433'

    New entries can be added as:

    >>> p.add('listen_addresses', '*', commented=True, comment='IP address')
    >>> p  # doctest: +NORMALIZE_WHITESPACE
    {'port': Entry(name='port', _value=5433, commented=False, comment=None),
     'shared_buffers': Entry(name='shared_buffers', _value='1GB', commented=False, comment=None),
     'listen_addresses': Entry(name='listen_addresses', _value='*', commented=True, comment='IP address')}
    >>> del p['shared_buffers']
    >>> p  # doctest: +NORMALIZE_WHITESPACE
    {'port': Entry(name='port', _value=5433, commented=False, comment=None),
     'listen_addresses': Entry(name='listen_addresses', _value='*', commented=True, comment='IP address')}

    Adding an existing entry fails:
    >>> p.add('port', 5433)
    Traceback (most recent call last):
        ...
    ValueError: 'port' key already present

    So does adding a value to the underlying dict:
    >>> p['bonjour_name'] = 'pgserver'
    Traceback (most recent call last):
        ...
    TypeError: cannot set a key
    """

    def __setitem__(self, key: str, value: Any) -> NoReturn:
        raise TypeError("cannot set a key")

    def add(
        self,
        name: str,
        value: Value,
        *,
        commented: bool = False,
        comment: str | None = None,
    ) -> None:
        """Add a new entry."""
        if name in self:
            raise ValueError(f"'{name}' key already present")
        entry = Entry(name, value, commented=commented, comment=comment)
        super().__setitem__(name, entry)


class Configuration:
    r"""Holds a parsed configuration.

    You can access parameter using attribute or dictionnary syntax.

    >>> conf = parse(['port=5432\n', 'pg_stat_statement.min_duration = 3s\n'])
    >>> conf.port
    5432
    >>> conf.port = 5433
    >>> conf.port
    5433
    >>> conf['port'] = 5434
    >>> conf.port
    5434
    >>> conf['pg_stat_statement.min_duration'].total_seconds()
    3.0
    >>> conf.get("ssl")
    >>> conf.get("ssl", False)
    False

    Configuration instances can be merged:

    >>> otherconf = parse(["listen_addresses='*'\n", "port = 5454\n"])
    >>> sumconf = conf + otherconf
    >>> print(json.dumps(sumconf.as_dict(), cls=JSONDateEncoder, indent=2))
    {
      "port": 5454,
      "pg_stat_statement.min_duration": "3s",
      "listen_addresses": "*"
    }

    though, lines are discarded in the operation:

    >>> sumconf.lines
    []

    >>> conf += otherconf
    >>> print(json.dumps(conf.as_dict(), cls=JSONDateEncoder, indent=2))
    {
      "port": 5454,
      "pg_stat_statement.min_duration": "3s",
      "listen_addresses": "*"
    }
    >>> conf.lines
    []

    .. attribute:: path

        Path to a file. Automatically set when calling :func:`parse` with a path
        to a file. This is default target for :meth:`save`.

    .. automethod:: edit
    .. automethod:: save

    """  # noqa

    lines: list[str]
    entries: dict[str, Entry]
    path: str | None

    _parameter_re = re.compile(
        r"^(?P<name>[a-z_.]+)(?: +(?!=)| *= *)(?P<value>.*?)"
        "[\\s\t]*"
        r"(?P<comment>#.*)?$"
    )

    # Internally, lines property contains an updated list of all comments and
    # entries serialized. When adding a setting or updating an existing one,
    # the serialized line is updated accordingly. This allows to keep comments
    # and serialize only what's needed. Other lines are just written as-is.

    def __init__(self, path: str | None = None) -> None:
        self.__dict__.update(
            dict(
                lines=[],
                entries=OrderedDict(),
                path=path,
            )
        )

    def parse(self, fo: Iterable[str]) -> Iterator[tuple[pathlib.Path, IncludeType]]:
        for raw_line in fo:
            self.lines.append(raw_line)
            line = raw_line.strip()
            if not line:
                continue
            commented = False
            if line.startswith("#"):
                # Try to parse the commented line as a commented parameter,
                # but only if in the form of 'name = value' since we cannot
                # discriminate a commented sentence (with whitespaces) from a
                # commented parameter in the form of 'name value'.
                if "=" not in line:
                    continue
                line = line.lstrip("#").lstrip()
                m = self._parameter_re.match(line)
                if not m:
                    # This is a real comment
                    continue
                commented = True
            else:
                m = self._parameter_re.match(line)
                if not m:
                    raise ValueError("Bad line: %r." % raw_line)
            kwargs = m.groupdict()
            name = kwargs.pop("name")
            value = parse_value(kwargs.pop("value"))
            if name in IncludeType.__members__:
                if not commented:
                    include_type = IncludeType[name]
                    assert isinstance(value, str), type(value)
                    yield (pathlib.Path(value), include_type)
            else:
                comment = kwargs["comment"]
                if comment is not None:
                    kwargs["comment"] = comment.lstrip("#").lstrip()
                if commented:
                    # Only overwrite a previous entry if it is commented.
                    try:
                        existing_entry = self.entries[name]
                    except KeyError:
                        pass
                    else:
                        if not existing_entry.commented:
                            continue
                self.entries[name] = Entry(
                    name, value, commented=commented, raw_line=raw_line, **kwargs
                )

    def parse_string(self, string: str) -> None:
        list(_consume(self, string.splitlines(keepends=True)))

    def __add__(self, other: Any) -> Configuration:
        cls = self.__class__
        if not isinstance(other, cls):
            return NotImplemented
        s = cls()
        s.entries.update(self.entries)
        s.entries.update(other.entries)
        return s

    def __iadd__(self, other: Any) -> Configuration:
        cls = self.__class__
        if not isinstance(other, cls):
            return NotImplemented
        self.lines[:] = []
        self.entries.update(other.entries)
        return self

    def __getattr__(self, name: str) -> Value:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Value) -> None:
        if name in self.__dict__:
            self.__dict__[name] = value
        else:
            self[name] = value

    def __contains__(self, key: str) -> bool:
        return key in self.entries

    def __getitem__(self, key: str) -> Value:
        return self.entries[key].value

    def __setitem__(self, key: str, value: Value) -> None:
        if key in IncludeType.__members__:
            raise ValueError("cannot add an include directive")
        if key in self.entries:
            e = self.entries[key]
            e.value = value
            self._update_entry(e)
        else:
            self._add_entry(Entry(key, value))

    def get(self, key: str, default: Value | None = None) -> Value | None:
        try:
            return self[key]
        except KeyError:
            return default

    def _add_entry(self, entry: Entry) -> None:
        assert entry.name not in self.entries
        self.entries[entry.name] = entry
        # Append serialized line.
        entry.raw_line = str(entry) + "\n"
        self.lines.append(entry.raw_line)

    def _update_entry(self, entry: Entry) -> None:
        key = entry.name
        old_entry, self.entries[key] = self.entries[key], entry
        if old_entry.commented:
            # If the entry was previously commented, we uncomment it (assuming
            # that setting a value to a commented entry does not make much
            # sense.)
            entry.commented = False
        # Update serialized entry.
        old_line = old_entry.raw_line
        entry.raw_line = str(entry) + "\n"
        try:
            lineno = self.lines.index(old_line)
        except ValueError:
            if not entry.commented:
                msg = (
                    f"entry {key!r} not directly found in {self.path or 'parsed content'}"
                    " (it might be defined in an included file),"
                    " appending a new line to set requested value"
                )
                warn(msg, UserWarning)
                self.lines.append(entry.raw_line)
        else:
            self.lines[lineno : lineno + 1] = [entry.raw_line]

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries.values())

    def as_dict(self) -> dict[str, Value]:
        return {k: v.value for k, v in self.entries.items() if not v.commented}

    @contextlib.contextmanager
    def edit(self) -> Iterator[EntriesProxy]:
        r"""Context manager allowing edition of the Configuration instance.

        >>> import sys

        >>> cfg = Configuration()
        >>> includes = cfg.parse([
        ...     "#listen_addresses = 'localhost'  # what IP address(es) to listen on;\n",
        ...     "                                 # comma-separated list of addresses;\n",
        ...     "port = 5432                      # (change requires restart)\n",
        ...     "max_connections = 100            # (change requires restart)\n",
        ... ])
        >>> list(includes)
        []
        >>> cfg.save(sys.stdout)
        #listen_addresses = 'localhost'  # what IP address(es) to listen on;
                                         # comma-separated list of addresses;
        port = 5432                      # (change requires restart)
        max_connections = 100            # (change requires restart)

        >>> with cfg.edit() as entries:
        ...     entries["port"].value = 2345
        ...     entries["port"].comment = None
        ...     entries["listen_addresses"].value = '*'
        ...     del entries["max_connections"]
        ...     entries.add(
        ...         "unix_socket_directories",
        ...         "'/var/run/postgresql'",
        ...         comment="comma-separated list of directories",
        ...     )
        >>> cfg.save(sys.stdout)
        listen_addresses = '*'  # what IP address(es) to listen on;
                                         # comma-separated list of addresses;
        port = 2345
        unix_socket_directories = '/var/run/postgresql'  # comma-separated list of directories
        """  # noqa: E501
        entries = EntriesProxy({k: copy.copy(v) for k, v in self.entries.items()})
        try:
            yield entries
        except Exception:
            raise
        else:
            # Add or update entries.
            for k, entry in entries.items():
                assert isinstance(entry, Entry), "expecting Entry values"
                if k not in self:
                    self._add_entry(entry)
                elif self.entries[k] != entry:
                    self._update_entry(entry)
            # Discard removed entries.
            for k, entry in list(self.entries.items()):
                if k not in entries:
                    del self.entries[k]
                    if entry.raw_line is not None:
                        self.lines.remove(entry.raw_line)

    def save(self, fo: str | pathlib.Path | IO[str] | None = None) -> None:
        """Write configuration to a file.

        Configuration entries order and comments are preserved.

        :param fo: A path or file-like object. Required if :attr:`path` is
            None.

        """
        with open_or_return(fo or self.path, mode="w") as fo:
            for line in self.lines:
                fo.write(line)


def _main(argv: list[str]) -> int:  # pragma: nocover
    try:
        conf = parse(argv[0] if argv else sys.stdin)
        print(json.dumps(conf.as_dict(), cls=JSONDateEncoder, indent=2))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: nocover
    exit(_main(sys.argv[1:]))
