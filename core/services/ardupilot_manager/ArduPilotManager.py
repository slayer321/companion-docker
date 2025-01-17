import os
import shutil
import stat
import subprocess
import time
from copy import deepcopy
from typing import Any, List, Optional, Set, Tuple

from loguru import logger

from firmware_download.FirmwareDownload import FirmwareDownload, Vehicle
from flight_controller_detector.Detector import Detector as BoardDetector
from flight_controller_detector.Detector import FlightControllerType
from mavlink_proxy.Endpoint import Endpoint
from mavlink_proxy.Manager import Manager as MavlinkManager
from settings import Settings
from Singleton import Singleton


class ArduPilotManager(metaclass=Singleton):
    def __init__(self) -> None:
        self.settings = Settings()
        self.mavlink_manager = MavlinkManager()

        # Load settings and do the initial configuration
        if self.settings.load():
            logger.info(f"Loaded settings from {self.settings.settings_file}.")
            logger.debug(self.settings.content)
        else:
            self.settings.create_settings()

        self.configuration = deepcopy(self.settings.content)
        self._load_endpoints()
        self.subprocess: Optional[Any] = None
        self.firmware_download = FirmwareDownload()

    def run(self) -> None:
        ArduPilotManager.check_running_as_root()

        while not self.start_board(BoardDetector.detect()):
            logger.warning("Flight controller board not detected, will try again.")
            time.sleep(2)

    @staticmethod
    def check_running_as_root() -> None:
        if os.geteuid() != 0:
            raise RuntimeError("ArduPilot manager needs to run with root privilege.")

    def start_navigator(self) -> None:
        firmware = os.path.join(self.settings.firmware_path, "ardusub")
        if not os.path.isfile(firmware):
            temporary_file = self.firmware_download.download(Vehicle.Sub, "Navigator")
            assert temporary_file, "Failed to download navigator binary."
            shutil.move(str(temporary_file), firmware)
            # Make the binary executable
            os.chmod(firmware, stat.S_IXOTH)
        try:
            subprocess.check_output([firmware, "--help"])
        except Exception as error:
            raise RuntimeError(f"Failed to start navigator: {error}") from error

        # ArduPilot process will connect as a client on the UDP server created by the mavlink router
        master_endpoint = Endpoint("Master", self.settings.app_name, "udpin", "127.0.0.1", 8852, protected=True)

        # Run ardupilot inside while loop to avoid exiting after reboot command
        ## Can be changed back to a simple command after https://github.com/ArduPilot/ardupilot/issues/17572
        ## gets fixed.
        # pylint: disable=consider-using-with
        self.subprocess = subprocess.Popen(
            "while true; do "
            f"{firmware} -A udp:{master_endpoint.place}:{master_endpoint.argument}"
            f"--log-directory {self.settings.firmware_path}/logs/"
            f"--storage-directory {self.settings.firmware_path}/storage/"
            "; sleep 1; done",
            shell=True,
            encoding="utf-8",
            errors="ignore",
        )

        self.start_mavlink_manager(master_endpoint)

    def start_serial(self, device: str) -> None:
        self.start_mavlink_manager(Endpoint("Master", self.settings.app_name, "serial", device, 115200, protected=True))

    def start_mavlink_manager(self, device: Endpoint) -> None:
        try:
            self.add_new_endpoints(
                {Endpoint("GCS Link", self.settings.app_name, "udpout", "192.168.2.1", 14550, protected=True)}
            )
            self.add_new_endpoints(
                {Endpoint("MAVLink2Rest", self.settings.app_name, "udpout", "127.0.0.1", 14000, protected=True)}
            )
        except Exception as error:
            logger.error(f"Could not create default GCS endpoint: {error}")
        self.mavlink_manager.set_master_endpoint(device)
        self.mavlink_manager.start()

    def start_board(self, boards: List[Tuple[FlightControllerType, str]]) -> bool:
        if not boards:
            return False

        if len(boards) > 1:
            logger.warning(f"More than a single board detected: {boards}")

        # Sort by priority
        boards.sort(key=lambda tup: tup[0].value)

        flight_controller_type, place = boards[0]
        logger.info(f"Board in use: {flight_controller_type.name}.")

        if FlightControllerType.Navigator == flight_controller_type:
            self.start_navigator()
            return True
        if FlightControllerType.Serial == flight_controller_type:
            self.start_serial(place)
            return True
        raise RuntimeError("Invalid board type: {boards}")

    def restart(self) -> None:
        self.mavlink_manager.restart()

    def _get_configuration_endpoints(self) -> Set[Endpoint]:
        return {Endpoint(**endpoint) for endpoint in self.configuration.get("endpoints") or []}

    def _save_endpoints_to_configuration(self, endpoints: Set[Endpoint]) -> None:
        self.configuration["endpoints"] = [endpoint.asdict() for endpoint in endpoints]

    def _load_endpoints(self) -> None:
        """Load endpoints from the configuration file to the mavlink manager."""
        for endpoint in self._get_configuration_endpoints():
            try:
                self.mavlink_manager.add_endpoint(endpoint)
            except Exception as error:
                logger.error(f"Could not load endpoint {endpoint}: {error}")

    def _reset_endpoints(self, endpoints: Set[Endpoint]) -> None:
        try:
            self.mavlink_manager.clear_endpoints()
            self.mavlink_manager.add_endpoints(endpoints)
            logger.info("Resetting endpoints to previous state.")
        except Exception as error:
            logger.error(f"Error resetting endpoints: {error}")

    def _update_endpoints(self) -> None:
        try:
            persistent_endpoints = set(filter(lambda endpoint: endpoint.persistent, self.get_endpoints()))
            self._save_endpoints_to_configuration(persistent_endpoints)
            self.settings.save(self.configuration)
            self.restart()
        except Exception as error:
            logger.error(f"Error updating endpoints: {error}")

    def get_endpoints(self) -> Set[Endpoint]:
        """Get all endpoints from the mavlink manager."""
        return self.mavlink_manager.endpoints()

    def add_new_endpoints(self, new_endpoints: Set[Endpoint]) -> None:
        """Add multiple endpoints to the mavlink manager and save them on the configuration file."""
        loaded_endpoints = self.get_endpoints()

        for endpoint in new_endpoints:
            try:
                self.mavlink_manager.add_endpoint(endpoint)
                logger.info(f"Adding endpoint '{endpoint.name}' and saving it to the settings file.")
            except Exception as error:
                logger.error(f"Failed to add endpoint '{endpoint.name}': {error}")
                self._reset_endpoints(loaded_endpoints)
                raise

        self._update_endpoints()

    def remove_endpoints(self, endpoints_to_remove: Set[Endpoint]) -> None:
        """Remove multiple endpoints from the mavlink manager and save them on the configuration file."""
        loaded_endpoints = self.get_endpoints()

        protected_endpoints = set(filter(lambda endpoint: endpoint.protected, endpoints_to_remove))
        if protected_endpoints:
            raise ValueError(f"Endpoints {protected_endpoints} are protected. Aborting operation.")

        for endpoint in endpoints_to_remove:
            try:
                self.mavlink_manager.remove_endpoint(endpoint)
                logger.info(f"Deleting endpoint '{endpoint.name}' and removing it from the settings file.")
            except Exception as error:
                logger.error(f"Failed to remove endpoint '{endpoint.name}': {error}")
                self._reset_endpoints(loaded_endpoints)
                raise

        self._update_endpoints()
