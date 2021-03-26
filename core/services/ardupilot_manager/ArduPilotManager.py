import os
import stat
import subprocess
import time
from copy import deepcopy
from pprint import pprint
from typing import Any, List, Optional, Tuple
from warnings import warn

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
            print(f"Loaded settings from {self.settings.settings_file}:")
            pprint(self.settings.content)
        else:
            self.settings.create_settings()

        self.configuration = deepcopy(self.settings.content)
        self._load_endpoints()
        self.subprocess: Optional[Any] = None
        self.firmware_download = FirmwareDownload()

    def run(self) -> None:
        ArduPilotManager.check_running_as_root()

        while not self.start_board(BoardDetector.detect()):
            print("Flight controller board not detected, will try again.")
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
            # Make the binary executable
            os.chmod(temporary_file, stat.S_IXOTH)
            os.rename(temporary_file, firmware)
        try:
            subprocess.check_output([firmware, "--help"])
        except Exception as error:
            raise RuntimeError(f"Failed to start navigator: {error}") from error

        local_endpoint = "tcp:0.0.0.0:5766"
        self.subprocess = subprocess.Popen(
            [
                firmware,
                "-A",
                local_endpoint,
                "--log-directory",
                f"{self.settings.firmware_path}/logs/",
                "--storage-directory",
                f"{self.settings.firmware_path}/storage/",
            ],
            shell=False,
            encoding="utf-8",
            errors="ignore",
        )

        # TODO: Fix ArduPilot UDP communication to use mavlink_manager
        # ArduPilot master is not working with UDP endpoints and mavlink-router
        # does not accept TCP master endpoints
        # self.start_mavlink_manager(Endpoint(local_endpoint))

        # Check if subprocess is running and wait until it finishes
        # Necessary since we don't have mavlink_manager running for navigator yet
        while self.subprocess.poll() is None:
            time.sleep(1)

    def start_serial(self, device: str) -> None:
        self.start_mavlink_manager(Endpoint(f"serial:{device}:115200"))

    def start_mavlink_manager(self, device: Endpoint) -> None:
        self.add_endpoint(Endpoint("udpin:0.0.0.0:14550"))
        self.mavlink_manager.set_master_endpoint(device)
        self.mavlink_manager.start()
        while self.mavlink_manager.is_running():
            time.sleep(1)

    def start_board(self, boards: List[Tuple[FlightControllerType, str]]) -> bool:
        if not boards:
            return False

        if len(boards) > 1:
            print(f"More than a single board detected: {boards}")

        # Sort by priority
        boards.sort(key=lambda tup: tup[0].value)

        flight_controller_type, place = boards[0]

        if FlightControllerType.Navigator == flight_controller_type:
            self.start_navigator()
        elif FlightControllerType.Serial == flight_controller_type:
            self.start_serial(place)
        else:
            raise RuntimeError("Invalid board type: {boards}")

        return False

    def restart(self) -> bool:
        return self.mavlink_manager.restart()

    def _load_endpoints(self) -> None:
        if "endpoints" not in self.configuration:
            self.configuration["endpoints"] = []
        else:
            endpoints = self.configuration["endpoints"]
            self.mavlink_manager.add_endpoints([Endpoint(e) for e in endpoints])

    def get_endpoints(self) -> List[Endpoint]:
        return self.mavlink_manager.endpoints()

    def add_endpoint(self, endpoint: Endpoint) -> bool:
        if not self.mavlink_manager.add_endpoint(endpoint):
            return False
        self.configuration.get("endpoints", []).append(endpoint.__dict__)
        self.settings.save(self.configuration)
        return True

    def remove_endpoint(self, endpoint: Endpoint) -> bool:
        if not self.mavlink_manager.remove_endpoint(endpoint):
            return False
        try:
            self.configuration.get("endpoints", []).remove(endpoint.__dict__)
            self.settings.save(self.configuration)
            return True
        except Exception as error:
            warn(f"Error: {error}")
            return False