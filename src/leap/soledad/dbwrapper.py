# -*- coding: utf-8 -*-
# dbwrapper.py
# Copyright (C) 2013 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
thread-safe wrapper for sqlite/pysqlcipher
"""
import logging
import threading
import Queue
import time

from functools import partial

from leap.soledad.backends import sqlcipher

logger = logging.getLogger(__name__)


class SQLCipherWrapper(threading.Thread):

    def __init__(self, *args, **kwargs):
        """
        Initializes a wrapper that proxies method and attribute
        access to an underlying SQLCipher instance. We instantiate sqlcipher
        in a thread, and all method accesses communicate with it using a
        Queue.

        :param *args: position arguments to pass to pysqlcipher initialization
        :type args: tuple

        :param **kwargs: keyword arguments to pass to pysqlcipher
                         initialization
        :type kwargs: dict
        """
        threading.Thread.__init__(self)
        self._db = None
        self._wrargs = args, kwargs

        self._queue = Queue.Queue()
        self._stopped = threading.Event()

        self.start()

    def _init_db(self):
        """
        Initializes sqlcipher database.

        This is called on a separate thread.
        """
        # instantiate u1db
        args, kwargs = self._wrargs
        self._db = sqlcipher.open(*args, **kwargs)

    def run(self):
        """
        Main loop for the sqlcipher thread.
        """
        logger.debug("SQLCipherWrapper thread started.")
        self._lock = threading.Lock()

        logger.debug("Initializing sqlcipher")
        self._init_db()

        while True:
            if not self._db:
                logger.debug('db not ready yet, waiting...')
                time.sleep(1)

            self._lock.acquire()
            mth, q, wrargs = self._queue.get()

            if mth == "__end_thread":
                break

            attr = getattr(self._db, mth, None)
            if not attr:
                logger.error('method %s does not exist' % (mth,))
                continue

            res = None

            if callable(attr):
                # invoke the method with the passed args
                args = wrargs.get('args', [])
                kwargs = wrargs.get('kwargs', {})
                try:
                    res = attr(*args, **kwargs)
                except Exception as e:
                    logger.error(
                        "Error on proxied method %s: '%s'." % (
                        attr, e.args[0]))
            else:
                # non-callable attribute
                res = attr
            logger.debug('returning proxied db call...')
            q.put(res)
            self._lock.release()

        self._stopped.set()
        logger.debug("SQLCipherWrapper thread terminated.")

    def close(self):
        """
        Closes the sqlcipher database and finished the thread
        """
        self._db.close()
        self.__end_thread()

    def __getattr__(self, attr):
        """
        Returns _db proxied attributes.
        """

        def __proxied_mth(method, *args, **kwargs):
            if not self._stopped.isSet():
                wrargs = {'args': args, 'kwargs': kwargs}
                q = Queue.Queue()
                self._queue.put((method, q, wrargs))
                res = q.get()

                return res

        rgetattr = object.__getattribute__

        if attr != "_db":
            proxied = partial(__proxied_mth, attr)
            return proxied

        # fallback to regular behavior
        return rgetattr(self, attr)

