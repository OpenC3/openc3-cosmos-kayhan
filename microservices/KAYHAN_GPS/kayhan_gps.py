"""Uploads spacecraft GPS history to the Kayhan Space SatCat API.

Every PERIOD seconds this microservice:

  1. Queries the COSMOS telemetry history for the configured GPS position and
     velocity items over the period which just elapsed
  2. Downsamples those samples and uploads them to the Kayhan SatCat orbit
     determination (OD) API as a GNSS file for the configured NORAD catalog ID
  3. Injects a KAYHAN STATUS packet so operators can see what happened
"""

import json
import os
import time
from datetime import datetime, timezone
from io import BytesIO

# Add any plugin lib directories to the Python search path so this microservice
# can import helpers from `/lib` folders inside installed plugins (Ruby gets this
# for free via gem `$LOAD_PATH`; Python does not).
import glob
from openc3.top_level import add_to_search_path

for path in glob.glob("/gems/gems/**/lib"):
    add_to_search_path(path, True)

from openc3.microservices.microservice import Microservice
from openc3.utilities.sleeper import Sleeper
from openc3.api import *

from satcat.sdk import Client
from satcat.sdk.settings import settings as satcat_settings

# Give the target decom and time series microservices a chance to start before
# the first query rather than delaying a full period
STARTUP_DELAY_S = 10
# When an upload fails the window is retried on the next run so no GPS data is
# lost. Cap how far back that retry can reach so a long outage can't build a
# query (and upload) large enough to be rejected.
MAX_BACKFILL_PERIODS = 4
DEFAULT_PERIOD_S = 3600
DEFAULT_SAMPLE_INTERVAL_S = 10
DEFAULT_SATCAT_TIMEOUT_S = 120
# Name of the status packet in the KAYHAN target
STATUS_PACKET_NAME = "STATUS"
LIMITS_SUFFIX = "__LIMITS"
# Characters the STATUS MESSAGE and GPS_SOURCE items hold. Writing a longer
# string raises, which would lose the whole status packet, so they're truncated.
MAX_MESSAGE_CHARS = 512
MAX_SOURCE_CHARS = 128
# The Kayhan GNSS format fixes the time and unit systems. Positions are given in
# meters and velocities in meters per second, which is what the GPS_POSITION_SCALE
# and GPS_VELOCITY_SCALE options convert the telemetry to.
GNSS_TIME_SYSTEM = "UTC"
GNSS_UNIT_SYSTEM = "kg-m-s-rad"


def iso_utc(timestamp):
    """Format a POSIX timestamp as an ISO 8601 UTC string, e.g. 2026-09-07T20:00:00.123456Z"""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def gnss_time(timestamp):
    """Format a POSIX timestamp the way Kayhan writes GNSS measurement times,
    e.g. 2026-09-07T20:00:00.123456+00:00"""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="microseconds")


def truncate(string, length):
    """Shorten a string to fit a fixed size telemetry item"""
    return string if len(string) <= length else string[: length - 3] + "..."


class KayhanGps(Microservice):
    def __init__(self, name):
        super().__init__(name)
        self.kayhan_target_name = "KAYHAN"
        self.norad_id = None
        self.gps_target_name = None
        self.gps_packet_name = None
        self.position_item_names = [None, None, None]
        self.velocity_item_names = [None, None, None]
        self.send_velocity = True
        self.operator = ""
        self.ecef_frame = "ITRF2020"
        self.position_scale = 1.0
        self.velocity_scale = 1.0
        self.period = DEFAULT_PERIOD_S
        self.sample_interval = DEFAULT_SAMPLE_INTERVAL_S
        self.satcat_base_url = None
        self.satcat_timeout = DEFAULT_SATCAT_TIMEOUT_S

        for option in self.config.get("options") or []:
            match option[0].upper():
                case "KAYHAN_TARGET_NAME":
                    self.kayhan_target_name = option[1].upper()
                case "GPS_NORAD_ID":
                    self.norad_id = int(option[1])
                case "GPS_TARGET_NAME":
                    self.gps_target_name = option[1].upper()
                case "GPS_PACKET_NAME":
                    self.gps_packet_name = option[1].upper()
                case "GPS_X_ITEM_NAME":
                    self.position_item_names[0] = option[1].upper()
                case "GPS_Y_ITEM_NAME":
                    self.position_item_names[1] = option[1].upper()
                case "GPS_Z_ITEM_NAME":
                    self.position_item_names[2] = option[1].upper()
                case "GPS_VX_ITEM_NAME":
                    self.velocity_item_names[0] = option[1].upper()
                case "GPS_VY_ITEM_NAME":
                    self.velocity_item_names[1] = option[1].upper()
                case "GPS_VZ_ITEM_NAME":
                    self.velocity_item_names[2] = option[1].upper()
                case "GPS_SEND_VELOCITY":
                    self.send_velocity = option[1].upper() in ["TRUE", "YES", "1"]
                case "GPS_POSITION_SCALE":
                    self.position_scale = float(option[1])
                case "GPS_VELOCITY_SCALE":
                    self.velocity_scale = float(option[1])
                case "GPS_ECEF_FRAME":
                    self.ecef_frame = option[1]
                case "KAYHAN_OPERATOR":
                    # Operator names contain spaces so take the whole rest of the line
                    self.operator = " ".join(option[1:])
                case "PERIOD":
                    self.period = int(option[1])
                case "SAMPLE_INTERVAL":
                    self.sample_interval = float(option[1])
                case "SATCAT_BASE_URL":
                    self.satcat_base_url = option[1]
                case "SATCAT_TIMEOUT":
                    self.satcat_timeout = int(option[1])
                case _:
                    self.logger.error(f"Unknown option passed to microservice {name}: {option}")

        # The GPS items to query, position first, in the order they appear in a sample
        self.gps_item_names = list(self.position_item_names)
        if self.send_velocity:
            self.gps_item_names += self.velocity_item_names
        # Human readable description of where the GPS data comes from. Built from
        # whatever was configured so it's still useful when an option is missing.
        self.gps_source = truncate(
            " ".join(str(part) for part in [self.gps_target_name, self.gps_packet_name] + self.gps_item_names if part),
            MAX_SOURCE_CHARS,
        )

        # Misconfiguration is reported through the STATUS packet every period rather
        # than crashing so operators can see why nothing is being uploaded
        self.config_error = self.validate_config()
        if self.config_error:
            self.logger.error(self.config_error)
        else:
            self.setup_satcat()

        # Start of the next GPS history query, set on the first run
        self.window_start = None
        self.run_count = 0
        self.success_count = 0
        self.error_count = 0
        self.consecutive_errors = 0
        self.last_success = ""
        self.sleeper = Sleeper()

    def validate_config(self):
        """Return a message describing the first missing required option, or None"""
        if self.norad_id is None:
            return "GPS_NORAD_ID option is required"
        if not self.gps_target_name or not self.gps_packet_name:
            return "GPS_TARGET_NAME and GPS_PACKET_NAME options are required"
        if not all(self.gps_item_names):
            return "GPS X / Y / Z (and velocity if enabled) item name options are required"
        if not self.operator:
            return "KAYHAN_OPERATOR option is required, it names your organization to Kayhan"
        if not os.environ.get("SATCAT_AUTH_CLIENT_ID") or not os.environ.get("SATCAT_AUTH_CLIENT_SECRET"):
            return "KAYHAN_CLIENT_ID and KAYHAN_CLIENT_SECRET secrets must be created in Admin / Secrets"
        return None

    def setup_satcat(self):
        """Configure the satcat SDK from our options and injected secrets"""
        satcat_settings.auth_method = "client_credentials"
        satcat_settings.auth_client_id = os.environ["SATCAT_AUTH_CLIENT_ID"]
        satcat_settings.auth_client_secret = os.environ["SATCAT_AUTH_CLIENT_SECRET"]
        satcat_settings.default_timeout = self.satcat_timeout
        if self.satcat_base_url:
            # The SDK builds the API URL by inserting a subdomain, which requires
            # the trailing slash the Settings validator normally adds
            if not self.satcat_base_url.endswith("/"):
                self.satcat_base_url += "/"
            satcat_settings.satcat_base_url = self.satcat_base_url

    def run(self):
        # Allow the other target processes to start before running the microservice
        if self.sleeper.sleep(STARTUP_DELAY_S):
            return

        while True:
            start_time = time.time()
            if self.cancel_thread:
                break

            self.run_once()
            self.count += 1

            run_time = time.time() - start_time
            delta = self.period - run_time
            if delta > 0:
                # Delay till the next period
                if self.sleeper.sleep(delta):  # returns true and breaks loop on shutdown
                    break

    def run_once(self):
        """Query one period of GPS history, upload it to Kayhan and report the result"""
        now = time.time()
        if self.window_start is None:
            self.window_start = now - self.period
        # A failed run leaves window_start alone so the same data is retried, but
        # never reach back further than MAX_BACKFILL_PERIODS
        self.window_start = max(self.window_start, now - self.period * MAX_BACKFILL_PERIODS)

        self.run_count += 1
        status = {
            "NORAD_ID": self.norad_id or 0,
            "GPS_SOURCE": self.gps_source,
            "PERIOD": self.period,
            "RUN_COUNT": self.run_count,
            "QUERY_START": iso_utc(self.window_start),
            "QUERY_END": iso_utc(now),
            "WINDOW_DURATION": now - self.window_start,
            "SAMPLES_FOUND": 0,
            "SAMPLES_SENT": 0,
            "FILENAME": "",
            "UPLOAD_SIZE": 0,
            "QUERY_DURATION": 0.0,
            "UPLOAD_DURATION": 0.0,
        }

        try:
            if self.config_error:
                raise RuntimeError(self.config_error)

            self.state = "QUERYING GPS HISTORY"
            query_start = time.time()
            found, samples = self.query_gps_history(self.window_start, now)
            status["QUERY_DURATION"] = time.time() - query_start
            status["SAMPLES_FOUND"] = found
            status["SAMPLES_SENT"] = len(samples)

            if samples:
                self.state = "UPLOADING TO KAYHAN"
                upload_start = time.time()
                filename, size = self.upload_gnss(samples, now)
                status["UPLOAD_DURATION"] = time.time() - upload_start
                status["FILENAME"] = filename
                status["UPLOAD_SIZE"] = size
                status["STATE"] = "OK"
                status["MESSAGE"] = f"Uploaded {len(samples)} GPS samples to Kayhan as {filename}"
                self.success_count += 1
                self.last_success = iso_utc(time.time())
                self.logger.info(status["MESSAGE"])
            else:
                status["STATE"] = "NO_DATA"
                status["MESSAGE"] = truncate(
                    f"No {self.gps_target_name} {self.gps_packet_name} GPS samples found between "
                    f"{status['QUERY_START']} and {status['QUERY_END']}",
                    MAX_MESSAGE_CHARS,
                )
                self.logger.warn(status["MESSAGE"])

            # Only advance the window once the data has been successfully handled
            self.window_start = now
            self.consecutive_errors = 0
            self.error = None
        except Exception as error:
            self.error_count += 1
            self.consecutive_errors += 1
            self.error = error
            status["STATE"] = "ERROR"
            status["MESSAGE"] = truncate(repr(error), MAX_MESSAGE_CHARS)
            self.logger.error(f"Kayhan GPS upload failed, retrying next period: {repr(error)}")

        status["SUCCESS_COUNT"] = self.success_count
        status["ERROR_COUNT"] = self.error_count
        status["CONSECUTIVE_ERRORS"] = self.consecutive_errors
        status["LAST_SUCCESS"] = self.last_success
        status["NEXT_RUN"] = iso_utc(now + self.period)
        self.inject_status(status)
        self.state = "RUNNING"

    def query_gps_history(self, start_time, end_time):
        """Query the GPS history logged between start_time and end_time, returning the
        number of samples found and those samples downsampled to no more than one
        per SAMPLE_INTERVAL seconds"""
        prefix = f"{self.gps_target_name}__{self.gps_packet_name}__"
        # PACKET_TIMESECONDS timestamps each sample and must be first because the
        # rest of the row is unpacked relative to it
        items = [f"{prefix}PACKET_TIMESECONDS__RAW"]
        items += [f"{prefix}{item_name}__CONVERTED" for item_name in self.gps_item_names]

        # get_tlm_available maps each item to the value type actually available (RAW
        # for items without a conversion) which is required before requesting
        # historical values. Items which don't exist come back as None.
        available = get_tlm_available(items)
        missing = [items[index] for index, actual in enumerate(available) if actual is None]
        if missing:
            raise RuntimeError(f"Telemetry item(s) do not exist: {', '.join(missing)}")
        # It also appends __LIMITS to items which have limits, which get_tlm_values
        # does not accept, so strip it back off
        available = [item[: -len(LIMITS_SUFFIX)] if item.endswith(LIMITS_SUFFIX) else item for item in available]

        rows = get_tlm_values(available, start_time=iso_utc(start_time), end_time=iso_utc(end_time))
        return self.build_samples(rows)

    def build_samples(self, rows):
        """Convert historical get_tlm_values rows into Kayhan GNSS measurements, returning
        the number of rows found and the downsampled measurements"""
        if not rows:
            return 0, []
        # Each row is a list of [value, limits_state] pairs, but a single row response
        # is flattened to just the pairs, so wrap it back up
        if not isinstance(rows[0][0], list):
            rows = [rows]

        measurements = []
        for row in rows:
            values = [pair[0] for pair in row]
            if any(value is None for value in values):
                continue  # Partial row, e.g. the packet was received without a GPS fix
            measurements.append([float(value) for value in values])
        # Kayhan expects measurements in time order, which is also what the
        # downsampling below assumes
        measurements.sort(key=lambda measurement: measurement[0])

        samples = []
        last_epoch = None
        for measurement in measurements:
            epoch = measurement[0]
            if last_epoch is not None and (epoch - last_epoch) < self.sample_interval:
                continue  # Downsample to stay under the Kayhan OD upload size limit
            last_epoch = epoch
            sample = {
                "time": gnss_time(epoch),
                "ecefPosX": measurement[1] * self.position_scale,
                "ecefPosY": measurement[2] * self.position_scale,
                "ecefPosZ": measurement[3] * self.position_scale,
            }
            if self.send_velocity:
                sample["ecefVelX"] = measurement[4] * self.velocity_scale
                sample["ecefVelY"] = measurement[5] * self.velocity_scale
                sample["ecefVelZ"] = measurement[6] * self.velocity_scale
            # Rows missing any value were dropped above, so everything sent is valid
            sample["dataValid"] = 1
            samples.append(sample)
        return len(rows), samples

    def upload_gnss(self, samples, epoch):
        """Upload the samples to the Kayhan SatCat OD API, returning the filename and size"""
        gnss = {
            "meta": {
                "operator": self.operator,
                "objectId": self.norad_id,
                "timeSystem": GNSS_TIME_SYSTEM,
                "ecefFrame": self.ecef_frame,
                "unitSystem": GNSS_UNIT_SYSTEM,
            },
            "data": samples,
        }
        content = json.dumps(gnss, indent=2).encode()
        # Kayhan rejects files which don't follow this naming convention, see
        # https://docs.satcat.com/operations/orbit-determination/
        timestamp = time.strftime("%Y_%m_%d_%H_%M_%S", time.gmtime(epoch))
        filename = f"gnss_{self.norad_id}_{timestamp}Z.json"
        with Client() as client:
            client.od.upload_file(self.norad_id, BytesIO(content), filename=filename)
        return filename, len(content)

    def inject_status(self, status):
        """Publish the KAYHAN STATUS packet so operators can see the integration state"""
        try:
            inject_tlm(self.kayhan_target_name, STATUS_PACKET_NAME, status)
        except Exception as error:
            self.logger.error(f"Unable to inject {self.kayhan_target_name} {STATUS_PACKET_NAME}: {repr(error)}")

    def shutdown(self):
        self.sleeper.cancel()  # Breaks out of run()
        super().shutdown()


if __name__ == "__main__":
    KayhanGps.class_run()
