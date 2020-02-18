"""
Command line interface for Data Delivery Portal
"""

# IMPORTS ############################################################ IMPORTS #

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
import shutil
import zipfile
import zlib
import tarfile
import gzip
import json

from pathlib import Path
import tempfile

import click
import couchdb
import sys
import hashlib
import os
import filetype
import mimetypes
from typing import Union

import datetime
from itertools import chain
import logging
import logging.config

from ctypes import *

from crypt4gh import lib, header, keys
from functools import partial
from getpass import getpass

from code_api.dp_exceptions import AuthenticationError, CouchDBException, \
    CompressionError, DataException, DeliveryPortalException, DeliveryOptionException, \
    EncryptionError, HashException, SecurePasswordException, StreamingError
from botocore.exceptions import ClientError

import boto3
from boto3.s3.transfer import TransferConfig
import smart_open

import concurrent.futures

import time

# CONFIG ############################################################## CONFIG #

logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
})


# GLOBAL VARIABLES ########################################## GLOBAL VARIABLES #

COMPRESSED_FORMATS = dict()


# CLASSES ############################################################ CLASSES #


# FUNCTIONS ######################################################## FUNCTIONS #

# Cryptography # # # # # # # # # # # # # # # # # # # # # # # # # Cryptography #

def secure_password_hash(password_settings: str, password_entered: str) -> (str):
    """Generates secure password hash.

    Args: 
        password_settings: String containing the salt, length of hash, n-exponential, 
                            r and p variables. Taken from database. Separated by '$'. 
        password_entered: The user-specified password. 

    Returns: 
        str: The derived hash from the user-specified password. 

    """

    settings = password_settings.split("$")
    for i in [1, 2, 3, 4]:
        settings[i] = int(settings[i])

    kdf = Scrypt(salt=bytes.fromhex(settings[0]),
                 length=settings[1],
                 n=2**settings[2],
                 r=settings[3],
                 p=settings[4],
                 backend=default_backend())

    return (kdf.derive(password_entered.encode('utf-8'))).hex()


# Database-related # # # # # # # # # # # # # # # # # # # # # Database-related #

def couch_connect() -> (couchdb.client.Server):
    """Connects to a couchdb interface. Currently hard-coded. 

    Returns: 
        couchdb.client.Server: CouchDB server instance. 

    """

    try:
        couch = couchdb.Server('http://delport:delport@localhost:5984/')
    except CouchDBException as cdbe:
        sys.exit(f"Database login failed. {cdbe}")
    else:
        return couch


def get_current_time() -> (str):
    """Gets the current time. Formats timestamp.

    Returns: 
        str: Timestamp in format 'YY-MM-DD_HH-MM-SS'

    """

    now = datetime.datetime.now()
    timestamp = ""
    sep = ""

    for t in (now.year, "-", now.month, "-", now.day, " ",
              now.hour, ":", now.minute, ":", now.second):
        if len(str(t)) == 1 and isinstance(t, int):
            timestamp += f"0{t}"
        else:
            timestamp += f"{t}"

    return timestamp


# Formats # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # Formats #

def compression_dict() -> (dict):
    """Creates a dictionary of compressed types.

    Returns:
        dict: All mime types regarded as compressed formats

    """

    extdict = mimetypes.encodings_map   # Original dict with compressed formats

    # Add custom formats
    extdict['.z'] = 'compress'
    extdict['.tgz'] = 'tar+gzip'
    extdict['.tbz2'] = 'tar+bz2'

    # Add more formats with same name as extension
    formats = ['gzip', 'lzo', 'snappy', 'zip', 'mp3', 'jpg',
               'jpeg', 'mpg', 'mpeg', 'avi', 'gif', 'png']
    for f_ in formats:
        extdict[f'.{f_}'] = f_

    return extdict


def file_type(fpath: str) -> (str, str, bool, str):
    """Guesses file mime. 

    Args: 
        fpath: Path to file.

    """

    mime = None             # file mime
    extension = None
    is_compressed = False
    comp_alg = None   # compression algorithm

    if os.path.isdir(fpath):
        mime = "folder"
    else:
        mime, encoding = mimetypes.guess_type(fpath)    # Guess file type
        extension = os.path.splitext(fpath)[1]          # File extension

        # Set compressed files as compressed
        if extension in COMPRESSED_FORMATS:
            is_compressed = True
            comp_alg = COMPRESSED_FORMATS[extension]

        # If the file mime type couldn't be found, manually check for ngs files
        if mime is None:
            if extension in mimetypes.types_map:
                mime = mimetypes.types_map[extension]
            elif extension == "":
                mime = None
            elif extension in (".abi", ".ab1"):
                mime = "ngs-data/abi"
            elif extension in (".embl"):
                mime = "ngs-data/embl"
            elif extension in (".clust", ".cw", ".clustal"):
                mime = "ngs-data/clustal"
            elif extension in (".fa", ".fasta", ".fas", ".fna", ".faa", ".afasta"):
                mime = "ngs-data/fasta"
            elif extension in (".fastq", ".fq"):
                mime = "ngs-data/fastq"
            elif extension in (".gbk", ".genbank", ".gb"):
                mime = "ngs-data/genbank"
            elif extension in (".paup", ".nexus"):
                mime = "ngs-data/nexus"
            else:
                mime = None
                click.echo(
                    f"Warning! Could not detect file type for file {fpath}")

        return mime, extension, is_compressed, comp_alg


# Login # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # Login #

def verify_user_credentials(config: str, username: str, password: str, project: str) -> (str, str, str):
    """Checks that the correct options and credentials are entered.

    Args: 
        config:     File containing the users DP username and password, 
                    and the project relating to the upload/download.
                    Can be used instead of inputing the credentials separately.
        username:   Username for DP log in. 
        password:   Password connected to username.
        project:    Project ID. 

    Returns: 
        tuple: A tuple containing three strings

            Username (str)

            Password (str)

            Project ID (str)

    """

    credentials = dict()

    # If none of username, password and config options are set
    # raise exception and quit execution -- dp cannot be accessed
    if all(x is None for x in [username, password, config]):
        raise DeliveryPortalException("Delivery Portal login credentials "
                                      "not specified. Enter --username/-u "
                                      "AND --password/-pw, or --config/-c. "
                                      "For help: 'dp_api --help'.")
    else:
        if config is not None:              # If config file entered
            if os.path.exists(config):      # and exist
                try:
                    with open(config, 'r') as cf:
                        credentials = json.load(cf)
                except OSError as ose:
                    sys.exit(f"Could not open path-file {config}: {ose}")

                # Check that all credentials are entered and quit if not
                for c in ['username', 'password', 'project']:
                    if c not in credentials:
                        raise DeliveryPortalException("The config file does not "
                                                      f"contain: '{c}'.")
                return credentials
        else:   # If config file is not entered check other options
            if username is None or password is None:
                raise DeliveryPortalException("Delivery Portal login credentials "
                                              "not specified. Enter --username/-u "
                                              "AND --password/-pw, or --config/-c."
                                              "For help: 'dp_api --help'.")
            else:
                if project is None:
                    raise DeliveryPortalException("Project not specified. Enter "
                                                  "project ID using --project option "
                                                  "or add to config file using --config/-c"
                                                  "option.")
                return username, \
                    password, \
                    project


def check_access(login_info: dict) -> (str):
    """Checks the users access to the delivery portal and the specified project,
    and the projects S3 access.

    Args: 
        login_info: Dictionary containing username, password and project ID. 

    Returns: 
        str: User ID connected to the specified user. 

    """

    username = login_info['username']
    password = login_info['password']
    project = login_info['project']

    try:
        user_db = couch_connect()['user_db']    # Connect to user database
    except CouchDBException as cdbe:
        sys.exit(f"Could not collect database 'user_db'. {cdbe}")
    else:
        for id_ in user_db:  # Search the database for the user
            if username == user_db[id_]['username']:  # If found check password
                if (user_db[id_]['password']['hash'] !=
                        secure_password_hash(password_settings=user_db[id_]['password']['settings'],
                                             password_entered=password)):
                    raise DeliveryPortalException("Wrong password. "
                                                  "Access to Delivery Portal denied.")
                else:
                    calling_command = sys._getframe().f_back.f_code.co_name
                    # If facility is uploading or researcher is downloading, access is granted
                    if (user_db[id_]['role'] == 'facility' and calling_command == "put") or \
                            (user_db[id_]['role'] == 'researcher' and calling_command == "get"):
                        # Check project access
                        project_access_granted = project_access(user=id_,
                                                                project=project)
                        if not project_access_granted:
                            raise DeliveryPortalException(
                                "Project access denied. Cancelling upload."
                            )
                        else:
                            return id_

                    else:
                        raise DeliveryOptionException("Chosen upload/download "
                                                      "option not granted. "
                                                      f"You chose: '{calling_command}'. "
                                                      "For help: 'dp_api --help'")
        # The user not found.
        raise CouchDBException("Username not found in database. "
                               "Access to Delivery Portal denied.")


def project_access(user: str, project: str) -> (bool):
    """Checks the users access to a specific project.

    Args: 
        user: User ID.
        project: ID of project that the user is requiring access to.

    Returns: 
        bool: True if project access granted

    """

    couch = couch_connect()    # Connect to database
    user_projects = couch['user_db'][user]['projects']

    if project not in couch['project_db']:
        raise CouchDBException(f"The project {project} does not exist.")
    else:
        if project not in user_projects:
            raise DeliveryOptionException("You do not have access to the specified project "
                                          f"{project}. Aborting upload.")
        else:
            project_db = couch['project_db'][project]
            # If the project exists but does not have any 'project_info'
            # raise exception and quit
            if 'project_info' not in project_db:
                raise CouchDBException("There is no 'project_info' recorded "
                                       "for the specified project.")
            else:
                # If the project delivery option is not S3, raise except and quit
                if 'delivery_option' not in project_db['project_info']:
                    raise CouchDBException("A delivery option has not been "
                                           "specified for this project. ")
                else:
                    if not project_db['project_info']['delivery_option'] == "S3":
                        raise DeliveryOptionException("The specified project does "
                                                      "not have access to S3 delivery.")
                    else:
                        return True  # No exceptions - access granted


# Path processing # # # # # # # # # # # # # # # # # # # # # # Path processing #

def create_directories(tdir: str) -> (bool, tuple):
    """Creates all temporary directories.

    Args: 
        tdir: Path to new temporary directory
        paths: Tuple containing all data-file paths

    Returns: 
        tuple: Tuple containing

            bool: True if directories created
            tuple: All created directories 
    """

    dirs = tuple(p for p in [tdir,
                             f"{tdir}/files",
                             f"{tdir}/keys",
                             f"{tdir}/meta",
                             f"{tdir}/logs"])

    for d_ in dirs:
        try:
            os.mkdir(d_)
        except OSError as ose:
            click.echo(f"The directory '{d_}' could not be created: {ose}"
                       "Cancelling delivery. Deleting temporary directory.")
            return False

    return True, dirs


def s3_upload(file: str, s3_resource, bucket):
    """Handles processing of files including compression and encryption. 

    Args: 
        file:   File to be uploaded 
        s3_resource: The S3 connection resource
        bucket: S3 bucket to upload to

    """

    filetoupload = os.path.abspath(file)
    filename = os.path.basename(filetoupload)

    # Upload file
    MB = 1024 ** 2
    GB = 1024 ** 3
    config = TransferConfig(multipart_threshold=5*GB, multipart_chunksize=5*MB)
    if bucket in s3_resource.buckets.all():
        if file_exists_in_bucket(s3_resource=s3_resource, bucketname=bucket.name, filename=filename):
            print(f"File exists: {filename}, not uploading file.")
        else:
            try:
                s3_resource.meta.client.upload_file(filetoupload, bucket.name,
                                                    filename, Config=config)
            except Exception as e:
                print("Something wrong: ", e)


# S3 checks # # # # # # # # # # # # # # # # # # # # # # # # # # # # S3 checks #

def file_exists_in_bucket(s3_resource, bucketname: str, filename: str) -> (bool):
    """Checks if the current file already exists in the specified bucket.
    If so, the file will not be uploaded.

    Args: 
        s3_resource: Boto3 S3 resource
        bucketname: Name of bucket to check for file
        filename: Name of file to look for 

    Returns: 
        bool: True if the file already exists, False if it doesnt

    """

    try:  # Check if file already exists in bucket
        s3_resource.Object(bucketname, filename).load()  # None if exists
    except ClientError as ce:
        return False
    else:  # File exists
        return True


# Testing # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # Testing #

def testfunction(file):
    return f"{time.time()} \t {file}"


# MAIN ################################################################## MAIN #

@click.group()
def cli():
    global COMPRESSED_FORMATS
    COMPRESSED_FORMATS = compression_dict()


@cli.command()
@click.option('--config', '-c',
              required=False,
              type=click.Path(exists=True),
              help="Path to config file containing e.g. username, password, project id, etc.")
@click.option('--username', '-u',
              required=False,
              type=str,
              help="Delivery Portal username.")
@click.option('--password', '-pw',
              required=False,
              type=str,
              help="Delivery Portal password.")
@click.option('--project', '-p',
              required=False,
              type=str,
              help="Project to upload files to.")
@click.option('--pathfile', '-f',
              required=False,
              type=click.Path(exists=True),
              multiple=False,
              help="Path to file containing all files and folders to be uploaded.")
@click.option('--data', '-d',
              required=False,
              type=click.Path(exists=True),
              multiple=True,
              help="Path to file or folder to upload.")
def put(config: str, username: str, password: str, project: str,
        pathfile: str, data: tuple) -> (str):
    """Uploads the files to S3 bucket. Only usable by facilities. """

    all_files = list()
    upload_path = dict()    # format: {original-file:file-to-be-uploaded}
    hash_dict = dict()      # format: {original-file:hmac}
    failed = dict()         # failed file/folder uploads

    # Check for all required login credentials and project and return in correct format
    user_info = verify_user_credentials(config=config,
                                        username=username,
                                        password=password,
                                        project=project)

    # Check user access to DP and project, and project to S3 delivery option
    user_id = check_access(login_info=user_info)

    if not isinstance(user_id, str):
        raise DeliveryPortalException("User ID not set, "
                                      "cannot proceed with data delivery.")

    # If no files are entered --> quit
    if not data and not pathfile:   
        raise DeliveryPortalException(
            "No data to be uploaded. Specify individual files/folders using "
            "the --data/-d option one or more times, or the --pathfile/-f. "
            "For help: 'dp_api --help'"
        )
    else:   
        # If --data option --> put all files in list
        if data is not None:    
            all_files = [os.path.abspath(d) if os.path.exists(d)
                         else [None, d] for d in data]

        # If --pathfile option --> put all files in list
        if pathfile is not None:
            pathfile_abs = os.path.abspath(pathfile)
            if os.path.exists(pathfile_abs):    # Precaution, already checked in click.option
                with open(pathfile_abs, 'r') as file: # Read lines, strip \n and put in list
                    all_files += [os.path.abspath(line.strip()) if os.path.exists(line.strip())
                                  else [None, line.strip()] for line in file]
            else: 
                raise IOError(f"--pathfile option {pathfile} does not exist. Cancelling delivery.")
            
            # Check for file duplicates
            for element in all_files:
                if all_files.count(element) != 1:
                    raise DeliveryOptionException(f"The path to file {element} is listed multiple times, "
                                                  "please remove path dublicates.")
        
        # This should never be able to be true - just precaution
        if not all_files:    
            raise DeliveryPortalException("Data tuple empty. Nothing to upload."
                                          "Cancelling delivery.")

    # Create temporary folder with timestamp and all subfolders
    timestamp = get_current_time().replace(" ", "_").replace(":", "-")
    temp_dir = f"{os.getcwd()}/DataDelivery_{timestamp}"
    dirs_created, dirs = create_directories(tdir=temp_dir)
    if not dirs_created:  # If error when creating one of the folders
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)  # Remove all prev created folders
                sys.exit(f"Temporary directory deleted. \n\n"
                         "----DELIVERY CANCELLED---\n")  # and quit
            except OSError as ose:
                sys.exit(f"Could not delete directory {temp_dir}: {ose}\n\n "
                         "----DELIVERY CANCELLED---\n")
    else:
        logging.basicConfig(filename=f"{temp_dir}/logs/data-delivery.log",
                            level=logging.DEBUG)

    # Get S3 credentials
    s3path = str(Path(os.getcwd())) + "/sensitive/s3_config.json"
    with open(s3path) as f:
        s3creds = json.load(f)

    # Keys and endpoint from file
    access_key = s3creds['access_key']
    secret_key = s3creds['secret_key']
    endpoint_url = s3creds['endpoint_url']

    # Start s3 connection resource
    s3_resource = boto3.resource(
        service_name='s3',
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )

    # Bucket to upload to specified by user
    bucketname = f"project_{user_info['project']}" 
    bucket = s3_resource.Bucket(bucketname)

    # Create multithreading pool
    with concurrent.futures.ThreadPoolExecutor() as executor:
        upload_threads = []
        for path in all_files:
            if type(path) == str: 
                # check if folder and then get all subfolders
                if os.path.isdir(path):
                    all_dirs = [x[0] for x in os.walk(path)]  # all (sub)dirs
                    for dir_ in all_dirs:
                        # check which files are in the directory
                        all_files = [f for f in os.listdir(dir_)
                                    if os.path.isfile(os.path.join(dir_, f))]
                        # Upload all files
                        for file in all_files:  
                            future = executor.submit(s3_upload, file,
                                                    s3_resource, bucket)
                            upload_threads.append(future)
                elif os.path.isfile(path):
                    # Upload file
                    future = executor.submit(s3_upload, path,
                                            s3_resource, bucket)
                    upload_threads.append(future)
                else:
                    sys.exit(f"Path type {path} not identified."
                            "Have you entered the correct path?")

        for f in concurrent.futures.as_completed(upload_threads):
            print(f.result())


@cli.command()
@click.option('--config', '-c',
              required=False,
              type=click.Path(exists=True),
              help="Path to config file containing e.g. username, password, project id, etc.")
@click.option('--username', '-u',
              required=False,
              type=str,
              help="Delivery Portal username.")
@click.option('--password', '-pw',
              required=False,
              type=str,
              help="Delivery Portal password.")
@click.option('--project', '-p',
              required=False,
              type=str,
              help="Project to upload files to.")
@click.option('--pathfile', '-f',
              required=False,
              multiple=False,
              type=click.Path(exists=True),
              help="Path to file containing all files and folders to be uploaded.")
@click.option('--data', '-d',
              required=False,
              multiple=True,
              type=click.Path(exists=True),
              help="Path to file or folder to upload.")
def get(config: str, username: str, password: str, project: str,
        pathfile: str, data: tuple):
    """Downloads the files from S3 bucket. Not usable by facilities. """

    click.echo("download function")
