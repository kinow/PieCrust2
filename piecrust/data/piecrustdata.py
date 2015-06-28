import logging
from piecrust import APP_VERSION
from piecrust.data.debug import build_debug_info


logger = logging.getLogger(__name__)


class PieCrustData(object):
    debug_render = ['version', 'url', 'branding', 'debug_info']
    debug_render_invoke = ['version', 'url', 'branding', 'debug_info']
    debug_render_redirect = {'debug_info': '_debugRenderDebugInfo'}

    def __init__(self):
        self.version = APP_VERSION
        self.url = 'http://bolt80.com/piecrust/'
        self.branding = 'Baked with <em><a href="%s">PieCrust</a> %s</em>.' % (
                'http://bolt80.com/piecrust/', APP_VERSION)
        self._page = None
        self._data = None

    @property
    def debug_info(self):
        if self._page is not None and self._data is not None:
            try:
                return build_debug_info(self._page, self._data)
            except Exception as ex:
                logger.exception(ex)
                return ('An error occured while generating debug info. '
                        'Please check the logs.')
        return ''

    def _enableDebugInfo(self, page, data):
        self._page = page
        self._data = data

    def _debugRenderDebugInfo(self):
        return "The very thing you're looking at!"
