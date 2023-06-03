import logging

_LOGGER=logging.getLogger(__name__)
class klass():
    def __init__(self, junk, *args, **kwargs):
        _LOGGER.debug("klass __init__ called: %s",self)
        return
    def open(self):
        _LOGGER.debug("klass open called: %s",self)
        return



def serial_class_for_url(url):

    _LOGGER.debug("serial_class_for_url called: %s %s",url,klass)

    return url,klass