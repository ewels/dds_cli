"""S3 Connector module"""

###############################################################################
# IMPORTS ########################################################### IMPORTS #
###############################################################################

# Standard library
import logging
import traceback
import requests
import sys

# Installed

# Own modules
from cli_code import DDSEndpoint

###############################################################################
# LOGGING ########################################################### LOGGING #
###############################################################################

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)

###############################################################################
# CLASSES ########################################################### CLASSES #
###############################################################################


class S3Connector:

    def __init__(self, project_id, token):
        self.safespring_project, self.keys = \
            self.get_s3info(project_id=project_id, token=token)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
            return False  # uncomment to pass exception through

        return True

    def get_s3info(self, project_id, token):
        """Gets the safespring project and keys."""

        args = {"project": project_id}

        response = requests.get(DDSEndpoint.S3KEYS, params=args, headers=token)

        if not response.ok:
            sys.exit("Failed retrieving Safespring project name. "
                     f"Error code: {response.status_code} "
                     f" -- {response.reason}"
                     f"{response.text}")

        s3info = response.json()
        return s3info["safespring_project"], s3info["keys"]

    def connect(self):
        """"""
