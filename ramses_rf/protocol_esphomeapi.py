import logging
from serial import SerialBase

_LOGGER=logging.getLogger(__name__)
class Serial(SerialBase):
    def __init__(self, junk, *args, **kwargs):
        print("arg: {} args: {} kwargs: {}".format(junk,args,kwargs))
        super().__init__(junk, *args, **kwargs)
#    def __init__(self, junk, *args, **kwargs):
#        _LOGGER.debug("klass __init__ called: %s",self)
#        return
    def open(self):
        _LOGGER.debug("klass open called: %s",self)
        return




def serial_class_for_url(url):

    _LOGGER.debug("serial_class_for_url called: %s %s",url,Serial)

    return url,Serial
    

