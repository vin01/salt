"""
Jinja loading utils to enable a more powerful backend for jinja templates
"""

import itertools
import logging
import os.path
import pprint
import re
import shlex
import time
import uuid
import warnings
from collections.abc import Hashable
from functools import wraps
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import jinja2
from jinja2 import BaseLoader, TemplateNotFound, nodes
from jinja2.environment import TemplateModule
from jinja2.exceptions import TemplateRuntimeError
from jinja2.ext import Extension

import salt.utils.data
import salt.utils.files
import salt.utils.json
import salt.utils.stringutils
import salt.utils.url
import salt.utils.yaml
from salt.exceptions import TemplateError
from salt.utils.decorators.jinja import jinja_filter, jinja_global, jinja_test
from salt.utils.odict import OrderedDict
from salt.utils.versions import Version

try:
    from markupsafe import Markup
except ImportError:
    # jinja < 3.1
    from jinja2 import Markup  # pylint: disable=no-name-in-module

log = logging.getLogger(__name__)

__all__ = ["SaltCacheLoader", "SerializerExtension"]

GLOBAL_UUID = uuid.UUID("91633EBF-1C86-5E33-935A-28061F4B480E")
JINJA_VERSION = Version(jinja2.__version__)


class SaltCacheLoader(BaseLoader):
    """
    A special jinja Template Loader for salt.
    Requested templates are always fetched from the server
    to guarantee that the file is up to date.
    Templates are cached like regular salt states
    and only loaded once per loader instance.
    """

    def __init__(
        self,
        opts,
        saltenv="base",
        encoding="utf-8",
        pillar_rend=False,
        _file_client=None,
    ):
        self.opts = opts
        self.saltenv = saltenv
        self.encoding = encoding
        self.pillar_rend = pillar_rend
        if self.pillar_rend:
            if saltenv not in self.opts["pillar_roots"]:
                self.searchpath = []
            else:
                self.searchpath = opts["pillar_roots"][saltenv]
        else:
            self.searchpath = [os.path.join(opts["cachedir"], "files", saltenv)]
        log.debug("Jinja search path: %s", self.searchpath)
        self.cached = []
        self._file_client = _file_client
        self._close_file_client = _file_client is None

    def file_client(self):
        """
        Return a file client. Instantiates on first call.
        """
        # If there was no file_client passed to the class, create a cache_client
        # and use that. This avoids opening a new file_client every time this
        # class is instantiated
        if (
            self._file_client is None
            or not hasattr(self._file_client, "opts")
            or self._file_client.opts["file_roots"] != self.opts["file_roots"]
        ):
            import salt.fileclient

            self._file_client = salt.fileclient.get_file_client(
                self.opts, self.pillar_rend
            )
            self._close_file_client = True
        return self._file_client

    def cache_file(self, template):
        """
        Cache a file from the salt master
        """
        saltpath = salt.utils.url.create(template)
        fcl = self.file_client()
        return fcl.get_file(saltpath, "", True, self.saltenv)

    def check_cache(self, template):
        """
        Cache a file only once
        """
        if template not in self.cached:
            ret = self.cache_file(template)
            if ret is not False:
                self.cached.append(template)

    def get_source(self, environment, template):
        """
        Salt-specific loader to find imported jinja files.

        Jinja imports will be interpreted as originating from the top
        of each of the directories in the searchpath when the template
        name does not begin with './' or '../'.  When a template name
        begins with './' or '../' then the import will be relative to
        the importing file.

        """
        # FIXME: somewhere do separator replacement: '\\' => '/'
        _template = template
        if template.split("/", 1)[0] in ("..", "."):
            is_relative = True
        else:
            is_relative = False
        # checks for relative '..' paths that step-out of file_roots
        if is_relative:
            # Starts with a relative path indicator
            if not environment or "tpldir" not in environment.globals:
                log.warning(
                    'Relative path "%s" cannot be resolved without an environment',
                    template,
                )
                raise TemplateNotFound(template)
            base_path = environment.globals["tpldir"]
            _template = os.path.normpath("/".join((base_path, _template)))
            if _template.split("/", 1)[0] == "..":
                log.warning(
                    'Discarded template path "%s": attempts to'
                    " ascend outside of salt://",
                    template,
                )
                raise TemplateNotFound(template)
            # local file clients should pass the dot-expanded relative path
            # when it's an absolute local filesystem location
            if environment.globals.get("opts", {}).get(
                "file_client"
            ) == "local" and os.path.isabs(base_path):
                _template = os.path.relpath(_template, base_path)

        self.check_cache(_template)

        if environment and template:
            tpldir = os.path.dirname(_template).replace("\\", "/")
            tplfile = _template
            if is_relative:
                tpldir = environment.globals.get("tpldir", tpldir)
                tplfile = template
            tpldata = {
                "tplfile": tplfile,
                "tpldir": "." if tpldir == "" else tpldir,
                "tpldot": tpldir.replace("/", "."),
            }
            environment.globals.update(tpldata)

        if _template in self.cached or os.path.exists(_template):
            # pylint: disable=cell-var-from-loop
            for spath in self.searchpath:
                filepath = os.path.join(spath, _template)
                try:
                    with salt.utils.files.fopen(filepath, "rb") as ifile:
                        contents = ifile.read().decode(self.encoding)
                        mtime = os.path.getmtime(filepath)

                        def uptodate():
                            try:
                                return os.path.getmtime(filepath) == mtime
                            except OSError:
                                return False

                        return contents, filepath, uptodate
                except OSError:
                    # there is no file under current path
                    continue
            # pylint: enable=cell-var-from-loop

        # there is no template file within searchpaths
        raise TemplateNotFound(template)

    def destroy(self):
        if self._close_file_client is False:
            return
        if self._file_client is None:
            return
        file_client = self._file_client
        self._file_client = None

        try:
            file_client.destroy()
        except AttributeError:
            # PillarClient and LocalClient objects do not have a destroy method
            pass

    def __enter__(self):
        self.file_client()
        return self

    def __exit__(self, *args):
        self.destroy()


class PrintableDict(OrderedDict):
    """
    Ensures that dict str() and repr() are YAML friendly.

    .. code-block:: python

        mapping = OrderedDict([('a', 'b'), ('c', None)])
        print mapping
        # OrderedDict([('a', 'b'), ('c', None)])

        decorated = PrintableDict(mapping)
        print decorated
        # {'a': 'b', 'c': None}
    """

    def __str__(self):
        output = []
        for key, value in self.items():
            if isinstance(value, str):
                # keeps quotes around strings
                output.append(f"{key!r}: {value!r}")
            else:
                # let default output
                output.append(f"{key!r}: {value!s}")
        return "{" + ", ".join(output) + "}"

    def __repr__(self):  # pylint: disable=W0221
        output = []
        for key, value in self.items():
            # Raw string formatter required here because this is a repr
            # function.
            output.append(f"{key!r}: {value!r}")
        return "{" + ", ".join(output) + "}"


# Additional globals
@jinja_global("raise")
def jinja_raise(msg):
    raise TemplateError(msg)


# Additional tests
@jinja_test("match")
def test_match(txt, rgx, ignorecase=False, multiline=False):
    """Returns true if a sequence of chars matches a pattern."""
    flag = 0
    if ignorecase:
        flag |= re.I
    if multiline:
        flag |= re.M
    compiled_rgx = re.compile(rgx, flag)
    return True if compiled_rgx.match(txt) else False


@jinja_test("equalto")
def test_equalto(value, other):
    """Returns true if two values are equal."""
    return value == other


# Additional filters
@jinja_filter("skip")
def skip_filter(data):
    """
    Suppress data output

    .. code-block:: yaml

        {% my_string = "foo" %}

        {{ my_string|skip }}

    will be rendered as empty string,

    """
    return ""


@jinja_filter("sequence")
def ensure_sequence_filter(data):
    """
    Ensure sequenced data.

    **sequence**

        ensure that parsed data is a sequence

    .. code-block:: jinja

        {% set my_string = "foo" %}
        {% set my_list = ["bar", ] %}
        {% set my_dict = {"baz": "qux"} %}

        {{ my_string|sequence|first }}
        {{ my_list|sequence|first }}
        {{ my_dict|sequence|first }}


    will be rendered as:

    .. code-block:: yaml

        foo
        bar
        baz
    """
    if not isinstance(data, (list, tuple, set, dict)):
        return [data]
    return data


@jinja_filter("to_bool")
def to_bool(val):
    """
    Returns the logical value.

    .. code-block:: jinja

        {{ 'yes' | to_bool }}

    will be rendered as:

    .. code-block:: text

        True
    """
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (str, (str,))):
        return val.lower() in ("yes", "1", "true")
    if isinstance(val, int):
        return val > 0
    if not isinstance(val, Hashable):
        return len(val) > 0
    return False


@jinja_filter("indent")
def indent(s, width=4, first=False, blank=False, indentfirst=None):
    """
    A ported version of the "indent" filter containing a fix for indenting Markup
    objects. If the minion has Jinja version 2.11 or newer, the "indent" filter
    from upstream will be used, and this one will be ignored.
    """
    if indentfirst is not None:
        warnings.warn(
            "The 'indentfirst' argument is renamed to 'first' and will"
            " be removed in Jinja 3.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        first = indentfirst

    indention = " " * width
    newline = "\n"

    if isinstance(s, Markup):
        indention = Markup(indention)
        newline = Markup(newline)

    s += newline  # this quirk is necessary for splitlines method

    if blank:
        rv = (newline + indention).join(s.splitlines())
    else:
        lines = s.splitlines()
        rv = lines.pop(0)

        if lines:
            rv += newline + newline.join(
                indention + line if line else line for line in lines
            )

    if first:
        rv = indention + rv

    return rv


@jinja_filter("tojson")
def tojson(val, indent=None, **options):
    """
    Implementation of tojson filter (only present in Jinja 2.9 and later).
    Unlike the Jinja built-in filter, this allows arbitrary options to be
    passed in to the underlying JSON library.
    """
    options.setdefault("ensure_ascii", True)
    if indent is not None:
        options["indent"] = indent
    return (
        salt.utils.json.dumps(val, **options)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )


@jinja_filter("quote")
def quote(txt):
    """
    Wraps a text around quotes.

    .. code-block:: jinja

        {% set my_text = 'my_text' %}
        {{ my_text | quote }}

    will be rendered as:

    .. code-block:: text

        'my_text'
    """
    return shlex.quote(txt)


@jinja_filter()
def regex_escape(value):
    return re.escape(value)


@jinja_filter("regex_search")
def regex_search(txt, rgx, ignorecase=False, multiline=False):
    """
    Searches for a pattern in the text.

    .. code-block:: jinja

        {% set my_text = 'abcd' %}
        {{ my_text | regex_search('^(.*)BC(.*)$', ignorecase=True) }}

    will be rendered as:

    .. code-block:: text

        ('a', 'd')
    """
    flag = 0
    if ignorecase:
        flag |= re.I
    if multiline:
        flag |= re.M
    obj = re.search(rgx, txt, flag)
    if not obj:
        return
    # Handle regular expressions which do not not use grouping
    if obj and not obj.groups():
        return (obj.group(),)
    return obj.groups()


@jinja_filter("regex_match")
def regex_match(txt, rgx, ignorecase=False, multiline=False):
    """
    Searches for a pattern in the text.

    .. code-block:: jinja

        {% set my_text = 'abcd' %}
        {{ my_text | regex_match('^(.*)BC(.*)$', ignorecase=True) }}

    will be rendered as:

    .. code-block:: text

        ('a', 'd')
    """
    flag = 0
    if ignorecase:
        flag |= re.I
    if multiline:
        flag |= re.M
    obj = re.match(rgx, txt, flag)
    if not obj:
        return
    # Handle regular expressions which do not use grouping
    if obj and not obj.groups():
        return (obj.group(),)
    return obj.groups()


@jinja_filter("regex_replace")
def regex_replace(txt, rgx, val, ignorecase=False, multiline=False):
    r"""
    Searches for a pattern and replaces with a sequence of characters.

    .. code-block:: jinja

        {% set my_text = 'lets replace spaces' %}
        {{ my_text | regex_replace('\s+', '__') }}

    will be rendered as:

    .. code-block:: text

        lets__replace__spaces
    """
    flag = 0
    if ignorecase:
        flag |= re.I
    if multiline:
        flag |= re.M
    compiled_rgx = re.compile(rgx, flag)
    return compiled_rgx.sub(val, txt)


@jinja_filter("uuid")
def uuid_(val):
    """
    Returns a UUID corresponding to the value passed as argument.

    .. code-block:: jinja

        {{ 'example' | uuid }}

    will be rendered as:

    .. code-block:: text

        f4efeff8-c219-578a-bad7-3dc280612ec8
    """
    return str(uuid.uuid5(GLOBAL_UUID, salt.utils.stringutils.to_str(val)))


### List-related filters


@jinja_filter()
def unique(values):
    """
    Removes duplicates from a list.

    .. code-block:: jinja

        {% set my_list = ['a', 'b', 'c', 'a', 'b'] -%}
        {{ my_list | unique }}

    will be rendered as:

    .. code-block:: text

        ['a', 'b', 'c']
    """
    ret = None
    if isinstance(values, Hashable):
        ret = set(values)
    else:
        ret = []
        for value in values:
            if value not in ret:
                ret.append(value)
    return ret


@jinja_filter("min")
def lst_min(obj):
    """
    Returns the min value.

    .. code-block:: jinja

        {% set my_list = [1,2,3,4] -%}
        {{ my_list | min }}

    will be rendered as:

    .. code-block:: text

        1
    """
    return min(obj)


@jinja_filter("max")
def lst_max(obj):
    """
    Returns the max value.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | max }}

    will be rendered as:

    .. code-block:: text

        4
    """
    return max(obj)


@jinja_filter("avg")
def lst_avg(lst):
    """
    Returns the average value of a list.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | avg }}

    will be rendered as:

    .. code-block:: yaml

        2.5
    """
    if not isinstance(lst, Hashable):
        return float(sum(lst) / len(lst))
    return float(lst)


@jinja_filter("union")
def union(lst1, lst2):
    """
    Returns the union of two lists.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | union([2, 4, 6]) }}

    will be rendered as:

    .. code-block:: text

        [1, 2, 3, 4, 6]
    """
    if isinstance(lst1, Hashable) and isinstance(lst2, Hashable):
        return set(lst1) | set(lst2)
    return unique(lst1 + lst2)


@jinja_filter("intersect")
def intersect(lst1, lst2):
    """
    Returns the intersection of two lists.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | intersect([2, 4, 6]) }}

    will be rendered as:

    .. code-block:: text

        [2, 4]
    """
    if isinstance(lst1, Hashable) and isinstance(lst2, Hashable):
        return set(lst1) & set(lst2)
    return unique([ele for ele in lst1 if ele in lst2])


@jinja_filter("difference")
def difference(lst1, lst2):
    """
    Returns the difference of two lists.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | difference([2, 4, 6]) }}

    will be rendered as:

    .. code-block:: text

        [1, 3, 6]
    """
    if isinstance(lst1, Hashable) and isinstance(lst2, Hashable):
        return set(lst1) - set(lst2)
    return unique([ele for ele in lst1 if ele not in lst2])


@jinja_filter("symmetric_difference")
def symmetric_difference(lst1, lst2):
    """
    Returns the symmetric difference of two lists.

    .. code-block:: jinja

        {% my_list = [1,2,3,4] -%}
        {{ set my_list | symmetric_difference([2, 4, 6]) }}

    will be rendered as:

    .. code-block:: text

        [1, 3]
    """
    if isinstance(lst1, Hashable) and isinstance(lst2, Hashable):
        return set(lst1) ^ set(lst2)
    return unique(
        [ele for ele in union(lst1, lst2) if ele not in intersect(lst1, lst2)]
    )


@jinja_filter("method_call")
def method_call(obj, f_name, *f_args, **f_kwargs):
    return getattr(obj, f_name, lambda *args, **kwargs: None)(*f_args, **f_kwargs)


try:
    pass_context = jinja2.pass_context
except AttributeError:
    # Old and deprecated method
    pass_context = jinja2.contextfunction


@pass_context
def show_full_context(ctx):
    return salt.utils.data.simple_types_filter(
        {key: value for key, value in ctx.items()}
    )


class SerializerExtension(Extension):
    '''
    Yaml and Json manipulation.

    **Format filters**

    Allows jsonifying or yamlifying any data structure. For example, this dataset:

    .. code-block:: python

        data = {
            'foo': True,
            'bar': 42,
            'baz': [1, 2, 3],
            'qux': 2.0
        }

    .. code-block:: jinja

        yaml = {{ data|yaml }}
        json = {{ data|json }}
        python = {{ data|python }}
        xml  = {{ {'root_node': data}|xml }}

    will be rendered as::

        yaml = {bar: 42, baz: [1, 2, 3], foo: true, qux: 2.0}
        json = {"baz": [1, 2, 3], "foo": true, "bar": 42, "qux": 2.0}
        python = {'bar': 42, 'baz': [1, 2, 3], 'foo': True, 'qux': 2.0}
        xml = """<<?xml version="1.0" ?>
                 <root_node bar="42" foo="True" qux="2.0">
                  <baz>1</baz>
                  <baz>2</baz>
                  <baz>3</baz>
                 </root_node>"""

    The yaml filter takes an optional flow_style parameter to control the
    default-flow-style parameter of the YAML dumper.

    .. code-block:: jinja

        {{ data|yaml(False) }}

    will be rendered as:

    .. code-block:: yaml

        bar: 42
        baz:
          - 1
          - 2
          - 3
        foo: true
        qux: 2.0

    **Load filters**

    Strings and variables can be deserialized with **load_yaml** and
    **load_json** tags and filters. It allows one to manipulate data directly
    in templates, easily:

    .. code-block:: jinja

        {%- set yaml_src = "{foo: it works}"|load_yaml %}
        {%- set json_src = '{"bar": "for real"}'|load_json %}
        Dude, {{ yaml_src.foo }} {{ json_src.bar }}!

    will be rendered as::

        Dude, it works for real!

    **Load tags**

    Salt implements ``load_yaml`` and ``load_json`` tags. They work like
    the `import tag`_, except that the document is also deserialized.

    Syntaxes are ``{% load_yaml as [VARIABLE] %}[YOUR DATA]{% endload %}``
    and ``{% load_json as [VARIABLE] %}[YOUR DATA]{% endload %}``

    For example:

    .. code-block:: jinja

        {% load_yaml as yaml_src %}
            foo: it works
        {% endload %}
        {% load_json as json_src %}
            {
                "bar": "for real"
            }
        {% endload %}
        Dude, {{ yaml_src.foo }} {{ json_src.bar }}!

    will be rendered as::

        Dude, it works for real!

    **Import tags**

    External files can be imported and made available as a Jinja variable.

    .. code-block:: jinja

        {% import_yaml "myfile.yml" as myfile %}
        {% import_json "defaults.json" as defaults %}
        {% import_text "completeworksofshakespeare.txt" as poems %}

    **Catalog**

    ``import_*`` and ``load_*`` tags will automatically expose their
    target variable to import. This feature makes catalog of data to
    handle.

    for example:

    .. code-block:: jinja

        # doc1.sls
        {% load_yaml as var1 %}
            foo: it works
        {% endload %}
        {% load_yaml as var2 %}
            bar: for real
        {% endload %}

    .. code-block:: jinja

        # doc2.sls
        {% from "doc1.sls" import var1, var2 as local2 %}
        {{ var1.foo }} {{ local2.bar }}

    ** Escape Filters **

    .. versionadded:: 2017.7.0

    Allows escaping of strings so they can be interpreted literally by another
    function.

    For example:

    .. code-block:: jinja

        regex_escape = {{ 'https://example.com?foo=bar%20baz' | regex_escape }}

    will be rendered as::

        regex_escape = https\\:\\/\\/example\\.com\\?foo\\=bar\\%20baz

    ** Set Theory Filters **

    .. versionadded:: 2017.7.0

    Performs set math using Jinja filters.

    For example:

    .. code-block:: jinja

        unique = {{ ['foo', 'foo', 'bar'] | unique }}

    will be rendered as::

        unique = ['foo', 'bar']

    ** Salt State Parameter Format Filters **

    .. versionadded:: 3005

    Renders a formatted multi-line YAML string from a Python dictionary. Each
    key/value pair in the dictionary will be added as a single-key dictionary
    to a list that will then be sent to the YAML formatter.

    For example:

    .. code-block:: jinja

        {% set thing_params = {
            "name": "thing",
            "changes": True,
            "warnings": "OMG! Stuff is happening!"
           }
        %}

        thing:
          test.configurable_test_state:
            {{ thing_params | dict_to_sls_yaml_params | indent }}

    will be rendered as::

    .. code-block:: yaml

        thing:
          test.configurable_test_state:
            - name: thing
            - changes: true
            - warnings: OMG! Stuff is happening!

    .. _`import tag`: https://jinja.palletsprojects.com/en/2.11.x/templates/#import
    '''

    tags = {
        "load_yaml",
        "load_json",
        "import_yaml",
        "import_json",
        "load_text",
        "import_text",
        "profile",
    }

    def __init__(self, environment):
        super().__init__(environment)
        self.environment.filters.update(
            {
                "yaml": self.format_yaml,
                "json": self.format_json,
                "xml": self.format_xml,
                "python": self.format_python,
                "load_yaml": self.load_yaml,
                "load_json": self.load_json,
                "load_text": self.load_text,
                "dict_to_sls_yaml_params": self.dict_to_sls_yaml_params,
                "combinations": itertools.combinations,
                "combinations_with_replacement": itertools.combinations_with_replacement,
                "compress": itertools.compress,
                "permutations": itertools.permutations,
                "product": itertools.product,
                "zip": zip,
                "zip_longest": itertools.zip_longest,
            }
        )

        if self.environment.finalize is None:
            self.environment.finalize = self.finalizer
        else:
            finalizer = self.environment.finalize

            @wraps(finalizer)
            def wrapper(self, data):
                return finalizer(self.finalizer(data))

            self.environment.finalize = wrapper

    def finalizer(self, data):
        """
        Ensure that printed mappings are YAML friendly.
        """

        def explore(data):
            if isinstance(data, (dict, OrderedDict)):
                return PrintableDict(
                    [(key, explore(value)) for key, value in data.items()]
                )
            elif isinstance(data, (list, tuple, set)):
                return data.__class__([explore(value) for value in data])
            return data

        return explore(data)

    def format_json(self, value, sort_keys=True, indent=None):
        json_txt = salt.utils.json.dumps(
            value, sort_keys=sort_keys, indent=indent
        ).strip()
        try:
            return Markup(json_txt)
        except UnicodeDecodeError:
            return Markup(salt.utils.stringutils.to_unicode(json_txt))

    def format_yaml(self, value, flow_style=True):
        yaml_txt = salt.utils.yaml.safe_dump(
            value, default_flow_style=flow_style
        ).strip()
        if yaml_txt.endswith("\n..."):
            yaml_txt = yaml_txt[: len(yaml_txt) - 4]
        try:
            return Markup(yaml_txt)
        except UnicodeDecodeError:
            return Markup(salt.utils.stringutils.to_unicode(yaml_txt))

    def format_xml(self, value):
        """Render a formatted multi-line XML string from a complex Python
        data structure. Supports tag attributes and nested dicts/lists.

        :param value: Complex data structure representing XML contents
        :returns: Formatted XML string rendered with newlines and indentation
        :rtype: str
        """

        def normalize_iter(value):
            if isinstance(value, (list, tuple)):
                if isinstance(value[0], str):
                    xmlval = value
                else:
                    xmlval = []
            elif isinstance(value, dict):
                xmlval = list(value.items())
            else:
                raise TemplateRuntimeError(
                    "Value is not a dict or list. Cannot render as XML"
                )
            return xmlval

        def recurse_tree(xmliter, element=None):
            sub = None
            for tag, attrs in xmliter:
                if isinstance(attrs, list):
                    for attr in attrs:
                        recurse_tree(((tag, attr),), element)
                elif element is not None:
                    sub = SubElement(element, tag)
                else:
                    sub = Element(tag)
                if isinstance(attrs, (str, int, bool, float)):
                    sub.text = str(attrs)
                    continue
                if isinstance(attrs, dict):
                    sub.attrib = {
                        attr: str(val)
                        for attr, val in attrs.items()
                        if not isinstance(val, (dict, list))
                    }
                for tag, val in [
                    item
                    for item in normalize_iter(attrs)
                    if isinstance(item[1], (dict, list))
                ]:
                    recurse_tree(((tag, val),), sub)
            return sub

        return Markup(
            minidom.parseString(
                tostring(recurse_tree(normalize_iter(value)))
            ).toprettyxml(indent=" ")
        )

    def format_python(self, value):
        return Markup(pprint.pformat(value).strip())

    def load_yaml(self, value):
        if isinstance(value, TemplateModule):
            value = str(value)
        try:
            return salt.utils.data.decode(salt.utils.yaml.safe_load(value))
        except salt.utils.yaml.YAMLError as exc:
            msg = "Encountered error loading yaml: "
            try:
                # Reported line is off by one, add 1 to correct it
                line = exc.problem_mark.line + 1
                buf = exc.problem_mark.buffer
                problem = exc.problem
            except AttributeError:
                # No context information available in the exception, fall back
                # to the stringified version of the exception.
                msg += str(exc)
            else:
                msg += f"{problem}\n"
                msg += salt.utils.stringutils.get_context(
                    buf, line, marker="    <======================"
                )
            raise TemplateRuntimeError(msg)
        except AttributeError:
            raise TemplateRuntimeError(f"Unable to load yaml from {value}")

    def load_json(self, value):
        if isinstance(value, TemplateModule):
            value = str(value)
        try:
            return salt.utils.json.loads(value)
        except (ValueError, TypeError, AttributeError):
            raise TemplateRuntimeError(f"Unable to load json from {value}")

    def load_text(self, value):
        if isinstance(value, TemplateModule):
            value = str(value)

        return value

    _load_parsers = {"load_yaml", "load_json", "load_text"}
    _import_parsers = {"import_yaml", "import_json", "import_text"}

    def parse(self, parser):
        if parser.stream.current.value in self._load_parsers:
            return self.parse_load(parser)
        elif parser.stream.current.value in self._import_parsers:
            return self.parse_import(
                parser, parser.stream.current.value.split("_", 1)[1]
            )
        elif parser.stream.current.value == "profile":
            return self.parse_profile(parser)

        parser.fail(
            "Unknown format " + parser.stream.current.value,
            parser.stream.current.lineno,
        )

    # pylint: disable=E1120,E1121
    def parse_profile(self, parser):
        lineno = next(parser.stream).lineno
        parser.stream.expect("name:as")
        label = parser.parse_expression()
        body = parser.parse_statements(["name:endprofile"], drop_needle=True)
        return self._parse_profile_block(parser, label, "profile block", body, lineno)

    def _create_profile_id(self, parser):
        return f"_salt_profile_{parser.free_identifier().name}"

    def _profile_start(self, label, source):
        return (label, source, time.time())

    def _profile_end(self, label, source, previous_time):
        log.profile(
            "Time (in seconds) to render %s '%s': %s",
            source,
            label,
            time.time() - previous_time,
        )

    def _parse_profile_block(self, parser, label, source, body, lineno):
        profile_id = self._create_profile_id(parser)
        ret = (
            [
                nodes.Assign(
                    nodes.Name(profile_id, "store").set_lineno(lineno),
                    self.call_method(
                        "_profile_start",
                        dyn_args=nodes.List([label, nodes.Const(source)]).set_lineno(
                            lineno
                        ),
                    ).set_lineno(lineno),
                ).set_lineno(lineno),
            ]
            + body
            + [
                nodes.ExprStmt(
                    self.call_method(
                        "_profile_end", dyn_args=nodes.Name(profile_id, "load")
                    ),
                ).set_lineno(lineno),
            ]
        )
        return ret

    def parse_load(self, parser):
        filter_name = parser.stream.current.value
        lineno = next(parser.stream).lineno
        if filter_name not in self.environment.filters:
            parser.fail(f"Unable to parse {filter_name}", lineno)

        parser.stream.expect("name:as")
        target = parser.parse_assign_target()
        macro_name = "_" + parser.free_identifier().name
        macro_body = parser.parse_statements(("name:endload",), drop_needle=True)

        return [
            nodes.Macro(macro_name, [], [], macro_body).set_lineno(lineno),
            nodes.Assign(
                target,
                nodes.Filter(
                    nodes.Call(
                        nodes.Name(macro_name, "load").set_lineno(lineno),
                        [],
                        [],
                        None,
                        None,
                    ).set_lineno(lineno),
                    filter_name,
                    [],
                    [],
                    None,
                    None,
                ).set_lineno(lineno),
            ).set_lineno(lineno),
        ]

    def parse_import(self, parser, converter):
        import_node = parser.parse_import()
        target = import_node.target
        lineno = import_node.lineno

        body = [
            import_node,
            nodes.Assign(
                nodes.Name(target, "store").set_lineno(lineno),
                nodes.Filter(
                    nodes.Name(target, "load").set_lineno(lineno),
                    f"load_{converter}",
                    [],
                    [],
                    None,
                    None,
                ).set_lineno(lineno),
            ).set_lineno(lineno),
        ]
        return self._parse_profile_block(
            parser, import_node.template, f"import_{converter}", body, lineno
        )

    def dict_to_sls_yaml_params(self, value, flow_style=False):
        """
        .. versionadded:: 3005

        Render a formatted multi-line YAML string from a Python dictionary. Each
        key/value pair in the dictionary will be added as a single-key dictionary
        to a list that will then be sent to the YAML formatter.

        :param value: Python dictionary representing Salt state parameters

        :param flow_style: Setting flow_style to False will enforce indentation
                           mode

        :returns: Formatted SLS YAML string rendered with newlines and
                  indentation
        """
        return self.format_yaml(
            [{key: val} for key, val in value.items()], flow_style=flow_style
        )
