from datetime import datetime, timedelta
from os.path import normcase, normpath, join, getmtime, getsize, isdir, exists
from pkg_resources import resource_exists, resource_filename, resource_isdir
import mimetypes

from repoze.lru import lru_cache

from pyramid.asset import resolve_asset_spec
from pyramid.httpexceptions import HTTPNotFound
from pyramid.httpexceptions import HTTPMovedPermanently
from pyramid.path import caller_package
from pyramid.response import Response
from pyramid.traversal import traversal_path
from pyramid.traversal import quote_path_segment

DEFAULT_CHUNKSIZE = 1<<16 # 64 kilobytes

def init_mimetypes(mimetypes):
    # this is a function so it can be unittested
    if hasattr(mimetypes, 'init'):
        mimetypes.init()
        return True
    return False

# See http://bugs.python.org/issue5853 which is a recursion bug
# that seems to effect Python 2.6, Python 2.6.1, and 2.6.2 (a fix
# has been applied on the Python 2 trunk).
init_mimetypes(mimetypes)

class FileResponse(Response):
    """
    Serves a static filelike object.
    """
    def __init__(self, path, request, expires, chunksize=DEFAULT_CHUNKSIZE):
        super(FileResponse, self).__init__()
        self.request = request
        self.last_modified = datetime.utcfromtimestamp(getmtime(path))

        # Check 'If-Modified-Since' request header
        # Browser might already have in cache
        modified_since = request.if_modified_since
        if modified_since is not None:
            if self.last_modified <= modified_since:
                self.status = 304
                return

        # Provide partial response if requested
        content_length = getsize(path)
        request_range = self._get_range(content_length)
        if request_range is not None:
            start, end = request_range
            if start >= content_length:
                self.status_int = 416 # Request range not satisfiable
                return

            self.status_int = 206 # Partial Content
            self.headers['Content-Range'] = 'bytes %d-%d/%d' % (
                start, end-1, content_length)
            content_length = end - start

        self.date = datetime.utcnow()
        self.app_iter = _file_iter(path, chunksize, request_range)
        self.content_type = mimetypes.guess_type(path, strict=False)[0]

        self.content_length = content_length
        if expires is not None:
            self.expires = self.date + expires

    def _get_range(self, content_length):
        # WebOb earlier than 0.9.7 has broken range parser implementation.
        # The current released version at this time is 0.9.6, so we do this
        # ourselves.  (It is fixed on trunk, though.)
        request = self.request
        range_header = request.headers.get('Range', None)
        if range_header is None:
            return None

        # Refuse to parse multiple byte ranges.  They are just plain silly.
        if ',' in range_header:
            return None

        unit, range_s = range_header.split('=', 1)
        if unit != 'bytes':
            # Other units are not supported
            return None

        if range_s.startswith('-'):
            start = content_length - int(range_s[1:])
            return start, content_length

        start, end = map(int, range_s.split('-'))
        return start, end + 1

class static_view(object):
    """ An instance of this class is a callable which can act as a
    :app:`Pyramid` :term:`view callable`; this view will serve
    static files from a directory on disk based on the ``root_dir``
    you provide to its constructor.

    The directory may contain subdirectories (recursively); the static
    view implementation will descend into these directories as
    necessary based on the components of the URL in order to resolve a
    path into a response.

    You may pass an absolute or relative filesystem path or a
    :term:`asset specification` representing the directory
    containing static files as the ``root_dir`` argument to this
    class' constructor.

    If the ``root_dir`` path is relative, and the ``package_name``
    argument is ``None``, ``root_dir`` will be considered relative to
    the directory in which the Python file which *calls* ``static``
    resides.  If the ``package_name`` name argument is provided, and a
    relative ``root_dir`` is provided, the ``root_dir`` will be
    considered relative to the Python :term:`package` specified by
    ``package_name`` (a dotted path to a Python package).

    ``cache_max_age`` influences the ``Expires`` and ``Max-Age``
    response headers returned by the view (default is 3600 seconds or
    five minutes).

    ``use_subpath`` influences whether ``request.subpath`` will be used as
    ``PATH_INFO`` when calling the underlying WSGI application which actually
    serves the static files.  If it is ``True``, the static application will
    consider ``request.subpath`` as ``PATH_INFO`` input.  If it is ``False``,
    the static application will consider request.path_info as ``PATH_INFO``
    input. By default, this is ``False``.

    .. note:: If the ``root_dir`` is relative to a :term:`package`, or
         is a :term:`asset specification` the :app:`Pyramid`
         :class:`pyramid.config.Configurator` method can be
         used to override assets within the named ``root_dir``
         package-relative directory.  However, if the ``root_dir`` is
         absolute, configuration will not be able to
         override the assets it contains.  """

    FileResponse = FileResponse # override point

    def __init__(self, root_dir, cache_max_age=3600, package_name=None,
                 use_subpath=False, index='index.html',
                 chunksize=DEFAULT_CHUNKSIZE):
        # package_name is for bw compat; it is preferred to pass in a
        # package-relative path as root_dir
        # (e.g. ``anotherpackage:foo/static``).
        if isinstance(cache_max_age, int):
            cache_max_age = timedelta(seconds=cache_max_age)
        self.expires = cache_max_age
        if package_name is None:
            package_name = caller_package().__name__
        package_name, docroot = resolve_asset_spec(root_dir, package_name)
        self.use_subpath = use_subpath
        self.package_name = package_name
        self.docroot = docroot
        self.norm_docroot = normcase(normpath(docroot))
        self.chunksize = chunksize
        self.index = index

    def __call__(self, context, request):
        if self.use_subpath:
            path_tuple = request.subpath
        else:
            path_tuple = traversal_path(request.path_info)

        path = secure_path(path_tuple)

        if path is None:
            # belt-and-suspenders security; this should never be true
            # unless someone screws up the traversal_path code
            # (request.subpath is computed via traversal_path too)
            return HTTPNotFound('Out of bounds: %s' % request.url)

        if self.package_name: # package resource

            resource_path ='%s/%s' % (self.docroot.rstrip('/'), path)
            if resource_isdir(self.package_name, resource_path):
                if not request.path_url.endswith('/'):
                    return self.add_slash_redirect(request)
                resource_path = '%s/%s' % (resource_path.rstrip('/'),self.index)
            if not resource_exists(self.package_name, resource_path):
                return HTTPNotFound(request.url)
            filepath = resource_filename(self.package_name, resource_path)

        else: # filesystem file

            # os.path.normpath converts / to \ on windows
            filepath = normcase(normpath(join(self.norm_docroot, path)))
            if isdir(filepath):
                if not request.path_url.endswith('/'):
                    return self.add_slash_redirect(request)
                filepath = join(filepath, self.index)
            if not exists(filepath):
                return HTTPNotFound(request.url)

        return self.FileResponse(filepath, request,self.expires,self.chunksize)

    def add_slash_redirect(self, request):
        url = request.path_url + '/'
        qs = request.query_string
        if qs:
            url = url + '?' + qs
        return HTTPMovedPermanently(url)

def _file_iter(path, chunksize, content_range=None):
    file = open(path, 'rb')
    
    if content_range is not None:

        class ByteReader(object):
            def __init__(self, n_bytes):
                self.bytes_left = n_bytes

            def __call__(self):
                b = file.read(min(self.bytes_left, chunksize))
                self.bytes_left -= len(b)
                return b

        start, end = content_range
        file.seek(start)
        get_bytes = ByteReader(end - start)

    else:
        def get_bytes():
            return file.read(chunksize)

    try:
        buf = get_bytes()
        while buf:
            yield buf
            buf = get_bytes()
    finally:
        if hasattr(file, 'close'):
            file.close()

@lru_cache(1000)
def secure_path(path_tuple):
    if '' in path_tuple:
        return None
    for item in path_tuple:
        for val in ['.', '/']:
            if item.startswith(val):
                return None
    return '/'.join([quote_path_segment(x) for x in path_tuple])
