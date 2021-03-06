""" Implement Muffin Application. """
import logging.config
import os
import re
from asyncio import coroutine, iscoroutine, Future
from importlib import import_module
from inspect import isfunction, isclass, ismethod

from aiohttp import web
from aiohttp.hdrs import METH_ANY
from cached_property import cached_property

from muffin import CONFIGURATION_ENVIRON_VARIABLE
from muffin.handler import Handler
from muffin.manage import Manager
from muffin.urls import StaticRoute
from muffin.utils import LStruct, to_coroutine


RETYPE = type(re.compile('@'))


class MuffinException(Exception):

    """ Implement a Muffin's exception. """

    pass


class Application(web.Application):

    """ Improve aiohttp Application. """

    # Default application settings
    defaults = {

        # Path to configuration module
        'CONFIG': None,

        # Enable debug mode
        'DEBUG': False,

        # Default encoding
        'ENCODING': 'utf-8',

        # Logging options
        'LOG_LEVEL': 'WARNING',
        'LOG_FORMAT': '%(asctime)s [%(process)d] [%(levelname)s] %(message)s',
        'LOG_DATE_FORMAT': '[%Y-%m-%d %H:%M:%S %z]',

        # List of plugins
        'PLUGINS': [],

        # Setup static files in development
        'STATIC_PREFIX': '/static',
        'STATIC_FOLDERS': ['static'],
    }

    def __init__(self, name, *, loop=None, router=None, middlewares=(), logger=web.web_logger,
                 handler_factory=web.RequestHandlerFactory, **OPTIONS):
        """ Initialize the application. """
        super(Application, self).__init__(loop=loop, router=router, middlewares=middlewares,
                                          logger=logger, handler_factory=handler_factory)

        self.name = name

        self._error_handlers = {}
        self._start_callbacks = []

        # Overide options
        self.defaults['CONFIG'] = OPTIONS.pop('CONFIG', self.defaults['CONFIG'])
        self.cfg.update(OPTIONS)

        # Setup logging
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(self.cfg.LOG_FORMAT, self.cfg.LOG_DATE_FORMAT))
        self.logger.addHandler(ch)
        self.logger.setLevel(self.cfg.LOG_LEVEL)
        self.logger.name = 'muffin'

        self.manage = Manager(self)

        # Setup static files option
        if isinstance(self.cfg.STATIC_FOLDERS, str):
            self.cfg.STATIC_FOLDERS = [self.cfg.STATIC_FOLDERS]

        elif not isinstance(self.cfg.STATIC_FOLDERS, list):
            self.cfg.STATIC_FOLDERS = list(self.cfg.STATIC_FOLDERS)

        # Setup plugins
        self.plugins = self.ps = LStruct()
        for plugin in self.cfg.PLUGINS:
            try:
                self.install(plugin)
            except Exception as exc:
                self.logger.error('Plugin is invalid: %s', plugin)
                self.logger.exception(exc)

        # Setup Logging
        LOGGING_CFG = self.cfg.get('LOGGING')
        if LOGGING_CFG and isinstance(LOGGING_CFG, dict):
            logging.config.dictConfig(LOGGING_CFG)

    def __repr__(self):
        """ Human readable representation. """
        return "<Application: %s>" % self.name

    @cached_property
    def cfg(self):
        """ Load the application configuration. """
        config = LStruct(self.defaults)
        module = config['CONFIG'] = os.environ.get(
            CONFIGURATION_ENVIRON_VARIABLE, config['CONFIG'])

        if module:
            try:
                module = import_module(module)
                config.update({
                    name: getattr(module, name) for name in dir(module)
                    if name == name.upper() and not name.startswith('_')
                })

            except ImportError:
                config.CONFIG = None
                self.register_on_start(
                    lambda app: app.logger.warn("The configuration hasn't found: %s" % module))

        return config

    def install(self, plugin, name=None):
        """Install a plugin to the application."""
        if isinstance(plugin, str):
            module, _, attr = plugin.partition(':')
            module = import_module(module)
            plugin = getattr(module, attr or 'Plugin')

        name = name or plugin.name
        if name in self.ps:
            raise MuffinException('Plugin with name `%s` is already intalled.' % name)

        if isinstance(plugin, type):
            plugin = plugin()

        if hasattr(plugin, 'setup'):
            plugin.setup(self)

        if hasattr(plugin, 'middleware_factory') \
                and plugin.middleware_factory not in self.middlewares:
            self.middlewares.append(plugin.middleware_factory)

        if hasattr(plugin, 'start'):
            self.register_on_start(plugin.start)

        if hasattr(plugin, 'finish'):
            self.register_on_finish(plugin.finish)

        # Save plugin links
        self.ps[name] = plugin

    @coroutine
    def start(self):
        """Start the application.

        Support for start-callbacks and lock the application's configuration and plugins.

        """
        if self._error_handlers and exc_middleware_factory not in self._middlewares:
            self._middlewares.append(exc_middleware_factory)

        # Register static paths
        for path in self.cfg.STATIC_FOLDERS:
            if os.path.isdir(path):
                route = StaticRoute(None, self.cfg.STATIC_PREFIX.rstrip('/') + '/', path)
                self.router.register_route(route)

            else:
                self.logger.warn('Disable static folder (hasnt found): %s' % path)

        # Run start callbacks
        for (cb, args, kwargs) in self._start_callbacks:
            try:
                res = cb(self, *args, **kwargs)
                if (iscoroutine(res) or isinstance(res, Future)):
                    yield from res
            except Exception as exc:
                self.loop.call_exception_handler({
                    'message': "Error in start callback",
                    'exception': exc,
                    'application': self,
                })

        # Lock the application's settings and plugin's registry after start
        self.cfg.lock()
        self.ps.lock()

        # Lock plugin's configurations
        for plugin in self.ps.values():
            plugin.cfg.lock()

    def register_on_start(self, func, *args, **kwargs):
        """ Register a start callback. """
        self._start_callbacks.append((func, args, kwargs))

    def register(self, *paths, methods=None, name=None, handler=None):
        """ Register function/coroutine/muffin.Handler on current application. """

        if isinstance(methods, str):
            methods = [methods]

        def wrapper(view):
            if handler is None:

                handler_ = view
                methods_ = methods or [METH_ANY]

                if isfunction(handler_) or ismethod(handler_):
                    handler_ = Handler.from_view(view, *methods_, name=name)

                handler_.connect(self, *paths, methods=methods_, name=name)

            else:

                view_name = view.__name__
                if hasattr(handler, view_name):
                    raise RuntimeError('Handler already have method "%s"' % view_name)
                setattr(handler, view_name, to_coroutine(view))
                name_ = name or view_name
                handler.connect(self, *paths, methods=methods, name=name_, view=view_name)

            return view

        # Support for @app.register(func)
        if len(paths) == 1 and callable(paths[0]):
            view = paths[0]

            if isclass(view) and issubclass(view, web.HTTPError):
                return wrapper

            paths = []
            return wrapper(view)

        return wrapper


@coroutine
def exc_middleware_factory(app, handler):
    """ Handle exceptions. """
    @coroutine
    def middleware(request):
        try:
            return (yield from handler(request))
        except Exception as exc:
            if type(exc) in app._error_handlers:
                request.exception = exc
                return (yield from app._error_handlers[type(exc)](request))
            raise
    return middleware


def run():
    """ Run the Gunicorn Application. """
    from .worker import GunicornApp

    GunicornApp("%(prog)s [OPTIONS] [APP]").run()


if __name__ == '__main__':
    run()
