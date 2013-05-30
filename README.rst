Soledad -- Synchronization Of Locally Encrypted Data Among Devices
==================================================================

This software is under development.

Dependencies
------------

To install Soledad you'll need to run the following command in order to install its dependencies:

  apt-get install libsqlite3-dev

This is only needed for the client side Soledad, but the server installation involves the client too for the time being.

Tests
-----

To run CouchDB tests, be sure you have CouchDB installed on your system.
Tests can be run with:

  python setup.py test
