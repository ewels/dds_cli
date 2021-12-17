"""Data Delivery System Project Status manager."""
import logging

# Installed
import requests
import simplejson

# Own modules
from dds_cli import base
from dds_cli import exceptions
from dds_cli import DDSEndpoint

###############################################################################
# START LOGGING CONFIG ################################# START LOGGING CONFIG #
###############################################################################

LOG = logging.getLogger(__name__)


###############################################################################
# CLASSES ########################################################### CLASSES #
###############################################################################


class ProjectStatusManager(base.DDSBaseClass):
    """Project Status manager class."""

    def __init__(
        self,
        username: str,
        project: str,
        no_prompt: bool = False,
    ):
        """Handle actions regarding project status in the cli."""
        # Initiate DDSBaseClass to authenticate user
        super().__init__(
            username=username,
            no_prompt=no_prompt,
            method_check=False,
        )
        self.project = project

    # Public methods ###################### Public methods #
    def get_status(self, show_history):
        """Get current status and status history of the project"""

        try:
            response = requests.get(
                DDSEndpoint.UPDATE_PROJ_STATUS,
                headers=self.token,
                params={
                    "project": self.project,
                },
                json={"history": show_history},
            )
        except requests.exceptions.RequestException as err:
            raise exceptions.ApiRequestError(message=str(err))

        # Check response
        if not response.ok:
            raise exceptions.APIError(f"Failed to get any projects: {response.text}")

        # Get result from API
        try:
            resp_json = response.json()
        except simplejson.JSONDecodeError as err:
            raise exceptions.APIError(f"Could not decode JSON response: {err}")
        else:
            LOG.info(f"Current status of {self.project}: {resp_json.get('current_status')}")
            if show_history:
                history = "Status history \n"
                for row in resp_json.get("history"):
                    history += ", ".join([item for item in row]) + " \n"
                LOG.info(history)

    def update_status(self, new_status, deadline=None, is_aborted=False):
        """Update project status"""

        extra_params = {"new_status": new_status}
        if deadline:
            extra_params["deadline"] = deadline
        if is_aborted:
            extra_params["is_aborted"] = is_aborted
        try:
            response = requests.post(
                DDSEndpoint.UPDATE_PROJ_STATUS,
                headers=self.token,
                params={
                    "project": self.project,
                },
                json=extra_params,
            )
        except requests.exceptions.RequestException as err:
            raise exceptions.ApiRequestError(message=str(err))

        # Check response
        if not response.ok:
            raise exceptions.APIError(f"An Error occured: {response.json().get('message')}")
        # Get result from API
        try:
            resp_json = response.json()
        except simplejson.JSONDecodeError as err:
            raise exceptions.APIError(f"Could not decode JSON response: {err}")
        else:
            LOG.info(f"Project {resp_json.get('message')}")