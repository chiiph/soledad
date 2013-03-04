"""
Soledad - Synchronization Of Locally Encrypted Data Among Devices.

Soledad is the part of LEAP that manages storage and synchronization of
application data. It is built on top of U1DB reference Python API and
implements (1) a SQLCipher backend for local storage in the client, (2) a
SyncTarget that encrypts data to the user's private OpenPGP key before
syncing, and (3) a CouchDB backend for remote storage in the server side.
"""

import os
import string
import random
import hmac
import configparser
import re
from u1db.remote import http_client
from leap.soledad.backends import sqlcipher
from leap.soledad.util import GPGWrapper
from leap.soledad.backends.leap_backend import (
    LeapDocument,
    DocumentNotEncrypted,
)


class KeyDoesNotExist(Exception):
    """
    Soledad attempted to find a key that does not exist locally.
    """


class KeyAlreadyExists(Exception):
    """
    Soledad attempted to create a key that already exists locally.
    """


#-----------------------------------------------------------------------------
# Soledad: local encrypted storage and remote encrypted sync.
#-----------------------------------------------------------------------------

class Soledad(object):
    """
    Soledad provides encrypted data storage and sync.

    A Soledad instance is used to store and retrieve data in a local encrypted
    database and synchronize this database with Soledad server.

    This class is also responsible for bootstrapping users' account by
    creating OpenPGP keys and other cryptographic secrets and/or
    storing/fetching them on Soledad server.
    """

    # other configs
    SECRET_LENGTH = 50
    DEFAULT_CONF = {
        'gnupg_home': '%s/gnupg',
        'secret_path': '%s/secret.gpg',
        'local_db_path': '%s/soledad.u1db',
        'config_file': '%s/soledad.ini',
        'soledad_server_url': '',
    }

    def __init__(self, user_email, prefix=None, gnupg_home=None,
                 secret_path=None, local_db_path=None,
                 config_file=None, soledad_server_url=None, auth_token=None,
                 initialize=True):
        """
        Bootstrap Soledad, initialize cryptographic material and open
        underlying U1DB database.
        """
        self._user_email = user_email
        self._auth_token = auth_token
        self._init_config(
            {'prefix': prefix,
             'gnupg_home': gnupg_home,
             'secret_path': secret_path,
             'local_db_path': local_db_path,
             'config_file': config_file,
             'soledad_server_url': soledad_server_url,
             }
        )
        if self.soledad_server_url:
            self._client = SoledadClient(server_url, token=auth_token)
        if initialize:
            self.bootstrap()

    def _bootstrap(self):
        """
        Bootstrap local Soledad instance.

        There are 3 stages for Soledad Client bootstrap:

            1. No key material has been generated, so we need to generate and
               upload to the server.

            2. Key material has already been generated and uploaded to the
               server, but has not been downloaded to this device/installation
               yet.

            3. Key material has already been generated and uploaded, and is
               also stored locally, so we just need to load it from disk.

        This method decides which bootstrap stage has to be performed and
        performs it.
        """
        self._init_dirs()
        self._gpg = GPGWrapper(gnupghome=self.gnupg_home)
        if not self._has_keys():
            try:
                # stage 2 bootstrap
                self._retrieve_keys()
            except Exception:
            # stage 1 bootstrap
                self._init_keys()
                self._send_keys()
        # stage 3 bootstrap
        self._load_keys()
        self._init_db()

    def _init_config(self, param_conf):
        """
        Initialize configuration, with precedence order give by: instance
        parameters > config file > default values.
        """
        self.prefix = param_conf['prefix'] or \
            os.environ['HOME'] + '/.config/leap/soledad'
        m = re.compile('.*%s.*')
        for key, default_value in self.DEFAULT_CONF.iteritems():
            val = param_conf[key] or default_value
            if m.match(val):
                val = val % self.prefix
            setattr(self, key, val)
        # get config from file
        config = configparser.ConfigParser()
        config.read(self.config_file)
        if 'soledad-client' in config:
            for key in default_conf:
                if key in config['soledad-client'] and not param_conf[key]:
                    setattr(self, key, config['soledad-client'][key])

    def _init_dirs(self):
        """
        Create work directories.
        """
        if not os.path.isdir(self.prefix):
            os.makedirs(self.prefix)

    def _init_keys(self):
        """
        Generate (if needed) and load OpenPGP keypair and secret for symmetric
        encryption.
        """
        # load/generate OpenPGP keypair
        if not self._has_openpgp_keypair():
            self._gen_openpgp_keypair()
        self._load_openpgp_keypair()
        # load/generate secret
        if not self._has_secret():
            self._gen_secret()
        self._load_secret()

    def _init_db(self):
        """
        Initialize the database for local storage .
        """
        # instantiate u1db
        # TODO: verify if secret for sqlcipher should be the same as the
        # one for symmetric encryption.
        self._db = sqlcipher.open(
            self.local_db_path,
            self._secret,
            create=True,
            document_factory=LeapDocument,
            soledad=self)

    def close(self):
        """
        Close underlying U1DB database.
        """
        self._db.close()

    #-------------------------------------------------------------------------
    # Management of secret for symmetric encryption
    #-------------------------------------------------------------------------

    def _has_secret(self):
        """
        Verify if secret for symmetric encryption exists on local encrypted
        file.
        """
        # does the file exist in disk?
        if not os.path.isfile(self.secret_path):
            return False
        # is it asymmetrically encrypted?
        f = open(self.secret_path, 'r')
        content = f.read()
        if not self.is_encrypted_asym(content):
            raise DocumentNotEncrypted(
                "File %s is not encrypted!" % self.secret_path)
        # can we decrypt it?
        fp = self._gpg.encrypted_to(content)['fingerprint']
        if fp != self._fingerprint:
            raise KeyDoesNotExist("Secret for symmetric encryption is "
                                  "encrypted to key with fingerprint '%s' "
                                  "which we don't have." % fp)
        return True

    def _load_secret(self):
        """
        Load secret for symmetric encryption from local encrypted file.
        """
        if not self._has_secret():
            raise KeyDoesNotExist("Tried to load key for symmetric "
                                  "encryption but it does not exist on disk.")
        try:
            with open(self.secret_path) as f:
                self._secret = str(self._gpg.decrypt(f.read()))
        except IOError:
            raise IOError('Failed to open secret file %s.' % self.secret_path)

    def _gen_secret(self):
        """
        Generate a secret for symmetric encryption and store in a local
        encrypted file.
        """
        if self._has_secret():
            raise KeyAlreadyExists("Tried to generate secret for symmetric "
                                   "encryption but it already exists on "
                                   "disk.")
        self._secret = ''.join(
            random.choice(
                string.ascii_letters +
                string.digits) for x in range(self.SECRET_LENGTH))
        ciphertext = self._gpg.encrypt(self._secret, self._fingerprint,
                                       self._fingerprint)
        f = open(self.secret_path, 'w')
        f.write(str(ciphertext))
        f.close()

    #-------------------------------------------------------------------------
    # Management of OpenPGP keypair
    #-------------------------------------------------------------------------

    def _has_openpgp_keypair(self):
        """
        Verify if there exists an OpenPGP keypair for this user.
        """
        # TODO: verify if we have the corresponding private key.
        try:
            self._gpg.find_key_by_email(self._user_email, secret=True)
            return True
        except LookupError:
            return False

    def _gen_openpgp_keypair(self):
        """
        Generate an OpenPGP keypair for this user.
        """
        if self._has_openpgp_keypair():
            raise KeyAlreadyExists("Tried to generate OpenPGP keypair but it "
                                   "already exists on disk.")
        params = self._gpg.gen_key_input(
            key_type='RSA',
            key_length=4096,
            name_real=self._user_email,
            name_email=self._user_email,
            name_comment='Generated by LEAP Soledad.')
        self._gpg.gen_key(params)

    def _load_openpgp_keypair(self):
        """
        Find fingerprint for this user's OpenPGP keypair.
        """
        if not self._has_openpgp_keypair():
            raise KeyDoesNotExist("Tried to load OpenPGP keypair but it does "
                                  "not exist on disk.")
        self._fingerprint = self._gpg.find_key_by_email(
            self._user_email)['fingerprint']

    def publish_pubkey(self, keyserver):
        """
        Publish OpenPGP public key to a keyserver.
        """
        # TODO: this has to talk to LEAP's Nickserver.
        pass

    #-------------------------------------------------------------------------
    # General crypto utility methods.
    #-------------------------------------------------------------------------

    def _has_keys(self):
        return self._has_openpgp_keypair() and self._has_secret()

    def _load_keys(self):
        self._load_openpgp_keypair()
        self._load_secret()

    def _gen_keys(self):
        self._gen_openpgp_keypair()
        self._gen_secret()

    def _retrieve_keys(self):
        h = hmac.new(self._user_email, 'user-keys').hexdigest()
        self._client._request_json('GET', ['user-keys', h])
        # TODO: create corresponding error on server side

    #-------------------------------------------------------------------------
    # Data encryption and decryption
    #-------------------------------------------------------------------------

    def encrypt(self, data, sign=None, passphrase=None, symmetric=False):
        """
        Encrypt data.
        """
        return str(self._gpg.encrypt(data, self._fingerprint, sign=sign,
                                     passphrase=passphrase,
                                     symmetric=symmetric))

    def encrypt_symmetric(self, doc_id, data, sign=None):
        """
        Encrypt data using symmetric secret.
        """
        return self.encrypt(data, sign=sign,
                            passphrase=self._hmac_passphrase(doc_id),
                            symmetric=True)

    def decrypt(self, data, passphrase=None, symmetric=False):
        """
        Decrypt data.
        """
        return str(self._gpg.decrypt(data, passphrase=passphrase))

    def decrypt_symmetric(self, doc_id, data):
        """
        Decrypt data using symmetric secret.
        """
        return self.decrypt(data, passphrase=self._hmac_passphrase(doc_id))

    def _hmac_passphrase(self, doc_id):
        return hmac.new(self._secret, doc_id).hexdigest()

    def is_encrypted(self, data):
        return self._gpg.is_encrypted(data)

    def is_encrypted_sym(self, data):
        return self._gpg.is_encrypted_sym(data)

    def is_encrypted_asym(self, data):
        return self._gpg.is_encrypted_asym(data)

    #-------------------------------------------------------------------------
    # Document storage, retrieval and sync
    #-------------------------------------------------------------------------

    def put_doc(self, doc):
        """
        Update a document in the local encrypted database.
        """
        return self._db.put_doc(doc)

    def delete_doc(self, doc):
        """
        Delete a document from the local encrypted database.
        """
        return self._db.delete_doc(doc)

    def get_doc(self, doc_id, include_deleted=False):
        """
        Retrieve a document from the local encrypted database.
        """
        return self._db.get_doc(doc_id, include_deleted=include_deleted)

    def get_docs(self, doc_ids, check_for_conflicts=True,
                 include_deleted=False):
        """
        Get the content for many documents.
        """
        return self._db.get_docs(doc_ids,
                                 check_for_conflicts=check_for_conflicts,
                                 include_deleted=include_deleted)

    def create_doc(self, content, doc_id=None):
        """
        Create a new document in the local encrypted database.
        """
        return self._db.create_doc(content, doc_id=doc_id)

    def get_doc_conflicts(self, doc_id):
        """
        Get the list of conflicts for the given document.
        """
        return self._db.get_doc_conflicts(doc_id)

    def resolve_doc(self, doc, conflicted_doc_revs):
        """
        Mark a document as no longer conflicted.
        """
        return self._db.resolve_doc(doc, conflicted_doc_revs)

    def sync(self, url):
        """
        Synchronize the local encrypted database with LEAP server.
        """
        # TODO: create authentication scheme for sync with server.
        return self._db.sync(url, creds=None, autocreate=True)


#-----------------------------------------------------------------------------
# Soledad client
#-----------------------------------------------------------------------------

class NoTokenForAuth(Exception):
    """
    No token was found for token-based authentication.
    """


class SoledadClient(http_client.HTTPClientBase):

    @staticmethod
    def connect(url, token=None):
        return SoledadClient(url, token=token)

    def __init__(self, url, creds=None, token=None):
        super(SoledadClient, self).__init__(url, creds)
        self.token = token

    def _set_token(self, token):
        self._token = token

    def _get_token(self):
        return self._token

    token = property(_get_token, _set_token,
                     doc='Token for token-based authentication.')

    def _request_json(self, method, url_parts, params=None, body=None,
                      content_type=None, auth=False):
        if auth:
            if not token:
                raise NoTokenForAuth()
            params.update({'auth_token', self.token})
        super(SoledadClient, self)._request_json(method, url_parts, params,
                                                 body, content_type)


__all__ = ['util']
