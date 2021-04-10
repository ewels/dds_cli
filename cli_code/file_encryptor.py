"""File encryptor module"""

import pathlib
import traceback
import os
import logging
import sys
from cryptography.hazmat import backends
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.kdf import hkdf
from cryptography.hazmat.primitives import hashes
from nacl.bindings import crypto_aead_chacha20poly1305_ietf_encrypt
from nacl.bindings import crypto_aead_chacha20poly1305_ietf_decrypt

from cryptography.hazmat.primitives import serialization

from cli_code.cli_decorators import generate_checksum
from cli_code import FileSegment

###############################################################################
# START LOGGING CONFIG ################################# START LOGGING CONFIG #
###############################################################################

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

###############################################################################
# CLASSES ########################################################### CLASSES #
###############################################################################


class ECDHKeyHandler:
    """Generates the shared encryption/decryption key, and transforms the components."""

    @staticmethod
    def generate_shared_key(my_private, peer_public, salt: bytes = b""):
        """Derive the shared key for file encryption."""

        # Generate or from db
        if salt == b"":
            salt = os.urandom(16)

        # Project public key
        # peer_public_bytes = bytes.fromhex(peer_public)
        # loaded_peer_public = x25519.X25519PublicKey.from_public_bytes(peer_public_bytes)

        # Generate shared key and derive encryption key with salt
        shared_key = (my_private).exchange(peer_public_key=peer_public)
        derived_shared_key = hkdf.HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"",
            backend=backends.default_backend(),
        ).derive(shared_key)

        LOG.debug("Salt: %s", salt)
        LOG.debug("Derived shared key: %s", derived_shared_key)
        return derived_shared_key, salt.hex().upper()

    @staticmethod
    def public_to_hex(public_key):
        """Converts public key to hexstring."""

        # public = self.private.public_key()
        public_bytes = public_key.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )

        return public_bytes.hex().upper()

    @staticmethod
    def get_public_component_hex(private_key):
        """Gets the public key and converts to hex string."""

        public = private_key.public_key()
        public_bytes = public.public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )

        return public_bytes.hex().upper()


class Encryptor(ECDHKeyHandler):
    """Handles the encryption of the files."""

    def __init__(self, project_keys):
        self.max_nonce = 2 ** (12 * 8)  # Max mumber of nonces

        # Only peer public needed, private should be None
        self.peer_public = x25519.X25519PublicKey.from_public_bytes(
            bytes.fromhex(project_keys[1])
        )

        # This generates public too
        self.my_private = x25519.X25519PrivateKey.generate()

        self.key, self.salt = self.generate_shared_key(
            my_private=self.my_private, peer_public=self.peer_public
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
            return False  # uncomment to pass exception through

        return True

    def encrypt_filechunks(self, chunks, outfile: pathlib.Path, progress: tuple = None):
        """Encrypts the file in chunks.

        Encrypts the file in chunks using the IETF ratified ChaCha20-Poly1305
        construction described in RFC7539.
        """

        # Additional data
        aad = None

        # Save encryption output to file
        with outfile.open(mode="wb") as out:
            # Create and save first IV/nonce
            iv_bytes = os.urandom(12)
            out.write(iv_bytes)

            # Get first iv/nonce as integer
            iv_int = int.from_bytes(iv_bytes, "little")
            nonce = b""  # Catch last nonce
            for chunk in chunks:
                # Restart at 0 if nonce number at maximum number of chunks per key
                nonce = (
                    iv_int if iv_int < self.max_nonce else iv_int % self.max_nonce
                ).to_bytes(length=12, byteorder="little")

                # Encrypt chunk
                encrypted_chunk = crypto_aead_chacha20poly1305_ietf_encrypt(
                    message=chunk, aad=aad, nonce=nonce, key=self.key
                )
                out.write(encrypted_chunk)

                progress[0].advance(progress[1], FileSegment.SEGMENT_SIZE_RAW)
                iv_int += 1  # Increment nonce

            # Save last nonce
            out.write(nonce)


class Decryptor(ECDHKeyHandler):
    """Handles the decryption of the files."""

    def __init__(self, project_keys: tuple, peer_public: str, key_salt: str):

        self.max_nonce = 2 ** (12 * 8)

        # Only private needed, public generated from it.
        self.my_private = x25519.X25519PrivateKey.from_private_bytes(
            bytes.fromhex(project_keys[0])
        )

        # Only peer public used
        self.peer_public = x25519.X25519PublicKey.from_public_bytes(
            bytes.fromhex(peer_public)
        )

        self.key, _ = self.generate_shared_key(
            my_private=self.my_private,
            peer_public=self.peer_public,
            salt=bytes.fromhex(key_salt),
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
            return False  # uncomment to pass exception through

        return True

    def decrypt_file(self, infile: pathlib.Path):
        """Decrypts the file"""

        with infile.open(mode="rb+") as file:
            # Get last nonce
            file.seek(-12, os.SEEK_END)
            last_nonce = file.read(12)

            # Remove last nonce from file
            file.seek(-12, os.SEEK_END)
            file.truncate()

            # Jump back to beginning and get first nonce
            file.seek(0)
            first_nonce = file.read(12)

            # Decrypt file
            if file.tell() != 12:
                raise SystemExit

            iv_int = int.from_bytes(first_nonce, "little")
            aad = None
            nonce = b""

            for chunk in iter(lambda: file.read(FileSegment.SEGMENT_SIZE_CIPHER), b""):
                # Get nonce as bytes for decryption: if the nonce is larger than the
                # max number of chunks allowed - wrap to 0 again
                nonce = (
                    iv_int if iv_int < self.max_nonce else iv_int % self.max_nonce
                ).to_bytes(length=12, byteorder="little")

                iv_int += 1

                yield crypto_aead_chacha20poly1305_ietf_decrypt(
                    ciphertext=chunk, aad=aad, nonce=nonce, key=self.key
                )

            LOG.debug("Testing nonce...")
            if last_nonce != nonce:
                raise SystemExit("Nonces do not match!!")
            LOG.debug("Last nonce should be: %s, was: %s", last_nonce, nonce)