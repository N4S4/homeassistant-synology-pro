# Home Assistant Synology Pro

A comprehensive Home Assistant custom integration for Synology NAS, exposing
**all** DSM subsystems as sensors, binary sensors, switches, and services —
powered by [`synology-api`](https://github.com/N4S4/synology-api).

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/N4S4/homeassistant-synology-pro?style=for-the-badge)](https://github.com/N4S4/homeassistant-synology-pro/releases)
[![License: GPL-3.0](https://img.shields.io/badge/license-GPL--3.0-blue.svg?style=for-the-badge)](LICENSE)
[![GitHub Activity](https://img.shields.io/github/commit-activity/m/N4S4/homeassistant-synology-pro?style=for-the-badge)](https://github.com/N4S4/homeassistant-synology-pro/commits)

<p align="center">
  <a href="https://my.home-assistant.io/redirect/hacs_repository/?owner=N4S4&repository=homeassistant-synology-pro&category=integration">
    <img src="https://my.home-assistant.io/badges/hacs_repository.svg" alt="Open your Home Assistant instance and open a repository inside the Home Assistant Community Store." />
  </a>
</p>

---

## Features

All sensors, binary sensors, and switches are **dynamically discovered**
from your NAS via the [`synology-api`](https://github.com/N4S4/synology-api)
library — no hardcoded entities.

### Sensors (~20 enabled by default, 2,000+ available)
- **System:** model, DSM version, serial, RAM, CPU temperature, uptime
- **Performance:** CPU utilization, memory utilization
- **Health:** system health status, overall status, DSM update available
- **Security:** security advisor rating and general info
- **Docker:** container count (total)
- **Downloads:** active download count
- **File Station:** service status

### Additional Modules (disabled by default — enable as needed)
- **Storage:** volume info, per-disk temperature, SMART status
- **Network:** interface details, firewall, DDNS, QuickConnect
- **Docker:** per-container stats, images, resources
- **Security Advisor:** scan results, login activity, checklist
- **Audio Station, Photos, VPN, Virtualization, Cloud Sync, DHCP, LDAP**
- **Log Center, Snapshots, Backup, Certificates, Users, Shares, Groups**
- **USB Copy, Note Station, Drive Admin Console, and more**

### Services (callable from automations)
- `synology_pro.create_snapshot` — Create a share snapshot
- `synology_pro.delete_snapshot` — Delete a share snapshot
- `synology_pro.restart_container` — Restart a Docker container
- `synology_pro.update_container` — Pull latest image & recreate
- `synology_pro.run_security_scan` — Trigger Security Advisor scan

## Installation

### HACS (Recommended)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=N4S4&repository=homeassistant-synology-pro&category=integration)

1. Click the badge above (or go to HACS → Integrations → ⋮ → Custom repositories)
2. Add `https://github.com/N4S4/homeassistant-synology-pro` as an **Integration** type
3. Search for "Synology Pro" and install
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** → search "Synology Pro"

### Manual

```bash
cd /path/to/homeassistant/config/custom_components
git clone https://github.com/N4S4/homeassistant-synology-pro.git synology_pro
```

Then restart HA and add the integration via the UI.

## Configuration

The integration uses the Home Assistant config flow (UI):

| Field | Description | Default |
|-------|-------------|---------|
| **Host** | NAS IP address or hostname | — |
| **Port** | DSM port | `5001` |
| **Username** | DSM account with admin rights | — |
| **Password** | DSM password | — |
| **Use SSL** | Enable HTTPS | ✅ On |
| **Verify SSL** | Validate certificate | ❌ Off |
| **DSM Version** | 6 or 7 | `7` |
| **Poll Interval** | Sensor refresh (seconds) | `60` |

## Automation Examples

```yaml
# Alert when NAS CPU temperature gets too high
- alias: "NAS Temperature Alert"
  trigger:
    - platform: numeric_state
      entity_id: sensor.synology_nas_192_168_1_2_core_sys_info_dsm_info_temperature
      above: 55
  action:
    - service: notify.telegram
      data:
        message: >-
          🔥 NAS is at
          {{ states('sensor.synology_nas_192_168_1_2_core_sys_info_dsm_info_temperature') }}°C!

# Get notified when a DSM update is available
- alias: "DSM Update Available"
  trigger:
    - platform: state
      entity_id: sensor.synology_nas_192_168_1_2_core_sys_info_upgrade_status_available_version
  action:
    - service: notify.telegram
      data:
        message: >-
          📦 DSM {{ states('sensor.synology_nas_192_168_1_2_core_sys_info_upgrade_status_available_version') }}
          is available!

# Create daily snapshots at 3 AM
- alias: "Daily NAS Snapshot"
  trigger:
    - platform: time
      at: "03:00:00"
  action:
    - service: synology_pro.create_snapshot
      data:
        share: "Documents"
        description: "Daily auto-snapshot"
```

## Entities Created

All entities are dynamically discovered from your NAS via the
[`synology-api`](https://github.com/N4S4/synology-api) Python library —
**every available API endpoint is probed** and its data exposed as sensors.

### How many entities?

The integration scans **25+ DSM modules** (System Info, Docker, Download Station,
File Station, Security Advisor, Audio Station, VPN, Virtualization, Log Center,
Snapshots, Photos, Cloud Sync, DHCP, LDAP, OAuth, USB Copy, Note Station, and more).
Depending on your NAS configuration and account permissions, this can produce
**2,000+ entities**.

> **Only ~20 entities start enabled.** Everything else is disabled by default
> and won't clutter your dashboard. You enable the ones you need manually.

### Entity naming

Entities are named dynamically based on the API response:

```
sensor.synology_nas_<ip>_<module>_<method>_<field>
binary_sensor.synology_nas_<ip>_<module>_<method>_<field>
```

Examples:
- `sensor.synology_nas_192_168_1_2_core_sys_info_dsm_info_model` → `DS218+`
- `sensor.synology_nas_192_168_1_2_core_sys_info_dsm_info_temperature` → `42`
- `sensor.synology_nas_192_168_1_2_core_sys_info_get_cpu_utilization_system_load` → `5.2`
- `binary_sensor.synology_nas_192_168_1_2_core_sys_info_dsm_info_temperature_warn` → `off`

### Enabled by default (curated ~20 entities)

Only the most useful sensors start active:

| Group | Methods | What you see |
|-------|---------|-------------|
| **System** | `dsm_info` | Model, serial, DSM version, RAM, temperature, uptime |
| **Performance** | `cpu_utilization`, `memory_utilization` | CPU load, RAM usage |
| **Health** | `system_health`, `sys_status`, `upgrade_status` | Overall health, update available |
| **Security** | `security_advisor.general_info` | Security rating |
| **Docker** | `containers.count` | Total container count |
| **Downloads** | `tasks_list.count` | Active download count |
| **File Station** | `get_info` | File Station status |

### Enabling additional entities

1. Go to **Settings → Devices & Services → Synology NAS → *N entities disabled***
2. Browse the disabled entities grouped by module
3. Enable the ones you want with one click

> **Tip for admin accounts:** if you use a DSM administrator account,
> the number of discovered entities grows significantly (2,000+). The
> curated default keeps your dashboard clean — enable only what you need.

### Account permissions matter

The number of entities depends on your DSM account's privileges:

| Account Type | Typical Entities | Modules Available |
|-------------|-----------------|-------------------|
| **User** | ~500 | File Station, Download Station, basic info |
| **Admin** | ~2,000+ | All modules including Docker, Security Advisor, Logs, VPN, etc. |

Use a restricted account for a leaner integration, or an admin account for full visibility.

## Requirements

- Home Assistant 2024.1+
- Python 3.9+
- `synology-api` library (auto-installed)

## Contributing

Pull requests welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-3.0 — see [LICENSE](LICENSE).

## Related Projects

- [synology-api](https://github.com/N4S4/synology-api) — Python wrapper for Synology DSM APIs
<!---- [synology-dashboard](https://github.com/N4S4/synology-dashboard) — Web dashboard for NAS management--->
<!---- [synology-api-recipes](https://github.com/N4S4/synology-api-recipes) — Cookbook of ready-to-use scripts--->
