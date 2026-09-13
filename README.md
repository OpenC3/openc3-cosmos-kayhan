# OpenC3 COSMOS Kayhan Space Plugin

Sends spacecraft GPS telemetry from OpenC3 COSMOS to the
[Kayhan Space](https://kayhan.space) SatCat API for orbit determination (OD).

Every period (1 hour by default) the `KAYHAN_GPS` microservice:

1. Queries the COSMOS telemetry history for the configured GPS position and
   velocity items over the period which just elapsed
2. Downsamples the samples (one every 10s by default) and uploads them to the
   Kayhan SatCat OD API as a GNSS file for the configured NORAD catalog ID
3. Injects a `KAYHAN STATUS` packet describing the run so operators can monitor
   the integration from Telemetry Viewer, Packet Viewer, or their own scripts

Kayhan runs OD against the uploaded GNSS data and produces predictive and
post-fit OEMs which are used in downstream conjunction assessment. See the
[Kayhan OD documentation](https://docs.satcat.com/operations/orbit-determination/)
for details.

## Requirements

- OpenC3 COSMOS 7.1.0 or later. GPS history is read over a `start_time` /
  `end_time` window from the COSMOS time series database, through
  `CvtModel.tsdb_lookup` rather than the `get_tlm_values` API - see the note
  in `query_gps_history` for why.
- A Kayhan SatCat Operations Enterprise subscription with ODaaS enabled, and
  SatCat API client credentials.
- The GPS telemetry must already be flowing into COSMOS from another plugin.
  This plugin only reads it.

## Installation

1. Create two secrets in the COSMOS Admin tool, Secrets tab:
   - `KAYHAN_CLIENT_ID` - your SatCat API client ID
   - `KAYHAN_CLIENT_SECRET` - your SatCat API client secret
1. Go to the Admin tool, Plugins tab, click install, and choose the plugin
   `.gem` file
1. Fill out the plugin parameters (see below) and click Install
1. Open the `KAYHAN STATUS` screen in Telemetry Viewer to confirm the first run

Install the plugin once per spacecraft, giving each installation its own
`kayhan_target_name` and `gps_norad_id`.

## Plugin Parameters

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `kayhan_target_name` | `KAYHAN` | Name of the target holding the `STATUS` packet |
| `gps_norad_id` | `99999` | NORAD catalog ID (5 or 9 digits) of your spacecraft |
| `gps_target_name` | `INST` | Target holding the GPS telemetry |
| `gps_packet_name` | `ADCS` | Packet holding the GPS telemetry |
| `gps_x_item_name` | `POSX` | Position X item |
| `gps_y_item_name` | `POSY` | Position Y item |
| `gps_z_item_name` | `POSZ` | Position Z item |
| `gps_vx_item_name` | `VELX` | Velocity X item |
| `gps_vy_item_name` | `VELY` | Velocity Y item |
| `gps_vz_item_name` | `VELZ` | Velocity Z item |
| `gps_send_velocity` | `true` | Set false to upload position only |
| `kayhan_operator` | `Example Operator` | Your organization name, written to the GNSS file. Required. |
| `gps_ecef_frame` | `ITRF2020` | ECEF realization the GPS telemetry is given in |
| `gps_position_scale` | `1.0` | Multiplier converting the position items to meters |
| `gps_velocity_scale` | `1.0` | Multiplier converting the velocity items to meters per second |
| `kayhan_update_period_s` | `3600` | Seconds between runs, and the length of each history query |
| `kayhan_sample_interval_s` | `10` | Minimum seconds between uploaded samples |
| `satcat_base_url` | `https://satcat.com/` | SatCat base URL, change only if Kayhan directs you to |
| `satcat_timeout_s` | `120` | SatCat REST API timeout |

The defaults point at the `INST` target from the COSMOS demo so the plugin can
be exercised without a real spacecraft.

## GNSS File Format

Each run uploads a JSON GNSS file named
`gnss_<NORAD>_YYYY_MM_DD_HH_MM_SSZ.json` as required by Kayhan:

```json
{
  "meta": {
    "operator": "Example Operator",
    "objectId": 99999,
    "timeSystem": "UTC",
    "ecefFrame": "ITRF2020",
    "unitSystem": "kg-m-s-rad"
  },
  "data": [
    {
      "time": "2025-03-25T17:41:24.123456+00:00",
      "ecefPosX": -1426305.02,
      "ecefPosY": -5629278.0,
      "ecefPosZ": -3722229.32,
      "ecefVelX": -2539.366545,
      "ecefVelY": -3535.773395,
      "ecefVelZ": 6322.35352,
      "dataValid": 1
    }
  ]
}
```

The `kg-m-s-rad` unit system means positions must be in meters and velocities in
meters per second. Use `gps_position_scale` and `gps_velocity_scale` if your
telemetry is in other units, for example `1000` for kilometers. Times come from
the packet time of each GPS sample, which COSMOS keeps in UTC.

Samples missing any configured item are dropped rather than sent with
`dataValid` cleared, because a sample with no position has nothing to report.

## Status Telemetry

`KAYHAN STATUS` is injected after every run with the result (`OK`, `NO_DATA`, or
`ERROR`) and a message, the queried history window, the number of GPS samples
found and uploaded, the uploaded filename and size, query and upload durations,
and running totals including `CONSECUTIVE_ERRORS`, which has limits so a
failing integration turns yellow then red.

A run that fails does not advance the history window, so the same GPS data is
retried on the next run. The retry reaches back at most four periods, which
keeps a long outage from building an upload large enough for Kayhan to reject.

## Testing

```
pip install -r requirements.txt -r tests/requirements.txt
python -m pytest tests/
```

The tests import the microservice and the satcat SDK for real, stubbing only the
parts which need a running COSMOS: the Microservice base class constructor and
the telemetry APIs. No Redis, time series database, or Kayhan account is needed.

GitHub Actions runs these on every push and pull request.
`.github/workflows/plugin-unit-tests.yml` here is just a caller; the workflow itself
lives in `OpenC3/.github` so every plugin shares one copy. It detects Python
tests in `tests/` or `test/` and Ruby specs in `spec/` or `specs/`, so there is
nothing to configure per plugin.

`tests/gnss_example.json` is the GNSS file format example from Kayhan. The
uploaded payload is asserted against it, including a round trip which feeds the
example's own measurements back through the microservice and expects the file to
come out identical, so a change at either end fails the tests rather than the
upload. `test_plugin_config.py` covers the seams a plugin only fails at on
install: OPTION names in `plugin.txt` the microservice does not handle, and
screen widgets naming items the telemetry definition does not define.

## Building

1. `<Path to COSMOS installation>/openc3.sh cli rake build VERSION=X.Y.Z`
   (or `openc3.bat` for Windows). `VERSION` is required and the gem file is
   built locally.

## Releasing

Run the **Release COSMOS Plugin** workflow from the Actions tab and give it a
version. It builds the gem, tags the commit, and creates a GitHub release.

Publishing is opt-in per release:

| Option | Default | Needs |
| ------ | ------- | ----- |
| Deploy to OpenC3 App Store | on | `OPENC3_API_TOKEN` repo secret |
| Publish the gem to RubyGems | off | `RUBYGEMS_API_KEY` repo secret |

RubyGems is off by default because a published version can only be yanked,
never replaced. The workflow checks for the API key before it tags or uploads
anything, so a missing secret fails the run rather than leaving a half
finished release.

## Contributing

We encourage you to contribute to OpenC3!

Contributing is easy.

1. Fork the project
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This OpenC3 plugin is released under the MIT License. See [LICENSE.md](LICENSE.md)
