# Emaldo Python Client

Unofficial Python library and CLI for interacting with [Emaldo](https://emaldo.com) home battery systems. This is an API-level reimplementation of the Emaldo mobile app, communicating with the same backend servers and E2E device protocol.

This unofficial project is intended to exist only until an official API is provided
by Emaldo. See [Legal Basis](#legal-basis) for the regulatory framework supporting
this implementation.

> **Disclaimer:** This is an **unofficial**, community-developed client. It is
> **not affiliated with, endorsed by, or supported by Emaldo** or any of its
> subsidiaries. There is **no warranty** of any kind — express or implied —
> including but not limited to warranties of merchantability, fitness for a
> particular purpose, or non-infringement. Use of this software is **entirely
> at your own risk**. The authors accept **no responsibility or liability** for
> any damage, data loss, warranty voiding, or other consequences arising from
> its use. By using this software you acknowledge that you understand and accept
> these terms.

## Features

- **Library API** — `EmaldoClient` class for programmatic access
- **CLI tool** — Full-featured command line interface
- **Battery monitoring** — SoC, power levels, charge/discharge stats
- **Battery detail** — Per-cell voltage, temperature, current, cycle count via E2E
- **Schedule reading** — 15-minute charge/discharge slot plans
- **Override control** — Charge, discharge, and idle overrides via E2E protocol
- **Battery markers** — Configurable high/low marker thresholds
- **Sell to grid** — Manual sell (discharge-to-grid) command via E2E
- **Emergency charge** — Force-charge the battery via E2E
- **Peak shaving** — Full peak shaving control via E2E (toggle, reserves, schedule, all-day, redundancy)
- **Grid frequency regulation** — Read FCR/mFRR balancing state via E2E
- **Usage analytics** — Solar, grid, battery, and revenue data

## Installation

```bash
pip install .
```

Or install in development mode:

```bash
pip install -e .
```

### Dependencies

- `requests` — HTTP API communication
- `pycryptodome` — AES and RC4 encryption
- `cramjam` — Snappy decompression

## Setup



Before using the client, extract the required app parameters from an Emaldo APK:

```bash
python -m emaldo.extract_keys path/to/base.apk --update
```

By default, this creates `.emaldo_params.json` in the package directory (where `extract_keys.py` is located). For most users, you should save it to your home directory or current working directory for best compatibility.

**To save directly to your home directory or another location, use:**

```bash
python -m emaldo.extract_keys path/to/base.apk --update --output ~/.emaldo_params.json
```

**Important:**

- For the CLI or library to work, `.emaldo_params.json` must be present in one of the following locations:
  - Your home directory (`~/.emaldo_params.json`)
  - The directory where you run the `emaldo` command (your current working directory)
  - (Advanced) The `emaldo/` package directory inside your Python environment (not recommended)

The CLI and library will search these locations in order. Placing the file in your home directory is recommended for most users.

To verify the extracted parameters:

```bash
python -m emaldo.extract_keys path/to/base.apk --json
```

## Quick Start

### CLI

```bash
# Login
emaldo login --email user@example.com --password MyPassword

# List homes and devices
emaldo homes
emaldo devices

# Select active home (if you have multiple)
emaldo homes --select 2

# Battery status
emaldo battery

# Battery cell detail (per-cell SoC, temperature, current via E2E)
emaldo battery-detail
emaldo battery-detail --json

# Daily usage summary (--offset N for past days)
emaldo usage
emaldo usage --offset 1

# Revenue data
emaldo revenue

# FCR predicted revenue
emaldo fcr

# Charge/discharge schedule
emaldo schedule

# Realtime power
emaldo power

# Solar generation (--offset N for past days)
emaldo solar

# Grid import/export
emaldo grid

# AI strategy composite view
emaldo strategy

# View schedule with overrides, markers, and electricity prices
emaldo override --show

# Override actions (7 types, with shortcuts)
emaldo override --range 01:00-05:00=charge-low     # cl: charge < low marker %
emaldo override --range 01:00-05:00=charge-high    # ch: charge < high marker %
emaldo override --range 01:00-05:00=charge-100     # cf: charge < 100%
emaldo override --range 12:00-14:00=idle            # i:  no charge/discharge
emaldo override --range 17:00-20:00=discharge-low   # dl: discharge > low marker %
emaldo override --range 17:00-20:00=discharge-high  # dh: discharge > high marker %
emaldo override --range 14:00-15:00=clear           # x:  remove override

# Override with custom markers
emaldo override --markers 21 73 --range 01:00-05:00=cl

# Clear all overrides
emaldo override --reset

# Reset overrides and restore default markers
emaldo override --markers 20 72 --reset

# Sell (discharge to grid) for 2 hours
emaldo sell --hours 2
emaldo sell --until 18:00
emaldo sell --cancel

# Emergency charge for 1 hour
emaldo emergency-charge --hours 1
emaldo emergency-charge --until 06:00
emaldo emergency-charge --cancel

# Peak shaving
emaldo peak-shaving                                              # Show current state
emaldo peak-shaving --enable                                     # Enable peak shaving
emaldo peak-shaving --disable                                    # Disable peak shaving
emaldo peak-shaving --peak-reserve 80 --ups-reserve 20           # Set reserves
emaldo peak-shaving --schedule 06:00-22:00 5000 Mon,Wed,Fri      # Set schedule
emaldo peak-shaving --schedule 06:00-22:00 5000 Mon-Fri --all-day  # All-day mode
emaldo peak-shaving --no-all-day                                 # Disable all-day
emaldo peak-shaving --redundancy 1                               # Set redundancy

# Grid frequency regulation (balancing) state
emaldo balancing-state                  # Show current state (idle/pre_balancing/balancing/balancing_failed)
emaldo balancing-state --json           # Raw JSON output
```

### Library

```python
from emaldo import EmaldoClient

client = EmaldoClient()
client.login("user@example.com", "password123")

# List homes
homes = client.list_homes()
home_id = homes[0]["home_id"]

# Auto-discover device
device_id, model, name = client.find_device(home_id)

# Get battery status
battery = client.get_battery(home_id, device_id, model)
print(battery["sensor"])
print(battery["power_level"])

# Get battery cell detail (per-cell voltage, temp, current)
detail = client.get_battery_info(home_id, device_id, model)
for cell in detail:
    print(f"Cell {cell['cabinet']}: {cell['soc']}% {cell['voltage_mV']}mV {cell['temp_bms']}°C")

# Get schedule
schedule = client.get_schedule(home_id, device_id, model)
slots = schedule["hope_charge_discharges"]

# Read current overrides (returns dict with slots, high_marker, low_marker)
state = client.get_overrides(home_id, device_id, model)
print(f"Markers: low={state['low_marker']}% high={state['high_marker']}%")
print(f"Slots: {len(state['slots'])} values")

# Set override: charge-low from 01:00 to 05:00
from emaldo.const import SLOT_NO_OVERRIDE, encode_override_action
slot_values = bytearray([SLOT_NO_OVERRIDE] * 96)
for slot in range(4, 20):  # slots 4-19 = 01:00-05:00
    slot_values[slot] = encode_override_action("charge-low", low_marker=20, high_marker=72)
client.set_override(home_id, device_id, model, bytes(slot_values))

# Set override with custom markers
client.set_override(home_id, device_id, model, bytes(slot_values),
                    high_marker=73, low_marker=21)

# Clear all overrides
client.reset_overrides(home_id, device_id, model)
```

### Session Persistence


The library keeps session state in memory. Export/import it for persistence:

```python
# Save session
import json
session = client.export_session()
with open("session.json", "w") as f:
  json.dump(session, f)

# Restore session
with open("session.json") as f:
  session = json.load(f)
client = EmaldoClient(session=session)
```

**CLI session file location:**

- When using the CLI, session state is saved to `.emaldo_session.json` in the same directory as the installed `cli.py` file. For installed packages, this is typically inside your Python environment's `site-packages/emaldo/` directory. For source usage, it's in your local `emaldo/` folder.
- You generally do not need to manage this file manually unless you want to migrate sessions between environments.

## API Reference

### EmaldoClient

| Method | Description |
|--------|-------------|
| `login(identifier, password)` | Authenticate with email/phone + password |
| `list_homes()` | List all homes on the account |
| `list_devices(home_id)` | List battery devices in a home |
| `find_home()` | Auto-discover first home with devices |
| `find_device(home_id)` | Auto-discover first device in a home |
| `get_battery(home_id, device_id, model)` | Battery overview (SoC, capacity, sensor) |
| `get_usage(home_id, device_id, model, offset)` | Comprehensive daily usage data |
| `get_revenue(home_id, device_id, model, offset)` | Revenue data |
| `get_fcr(home_id)` | FCR predicted revenue summary |
| `get_schedule(home_id, device_id, model)` | Charge/discharge schedule (96-192 slots) |
| `get_power(home_id, device_id, model)` | Realtime power readings |
| `get_solar(home_id, device_id, model, offset)` | Solar/MPPT generation |
| `get_grid(home_id, device_id, model, offset)` | Grid import/export |
| `get_strategy(home_id, device_id, model)` | Composite AI strategy view |
| `get_battery_info(home_id, device_id, model)` | Per-cell battery data via E2E |
| `get_overrides(home_id, device_id, model)` | Read override state (slots + markers) |
| `set_override(home_id, ..., slots, high_marker, low_marker)` | Set override values + markers |
| `reset_overrides(home_id, ..., high_marker, low_marker)` | Clear overrides (optionally set markers) |
| `send_sell(home_id, device_id, model, duration_seconds)` | Sell (discharge-to-grid) for N seconds |
| `cancel_sell(home_id, device_id, model)` | Cancel active sell command |
| `emergency_charge_on(home_id, device_id, model, duration_seconds)` | Emergency charge for N seconds |
| `emergency_charge_off(home_id, device_id, model)` | Cancel emergency charge |
| `get_peak_shaving(home_id, device_id, model)` | Read peak shaving config & schedule via E2E |
| `toggle_peak_shaving(home_id, ..., enabled)` | Enable/disable peak shaving |
| `set_peak_shaving_points(home_id, ..., peak_pct, ups_pct)` | Set peak/UPS reserve percentages |
| `set_peak_shaving_schedule(home_id, ..., id, start, end, days, power, all_day)` | Set peak shaving schedule |
| `set_peak_shaving_redundancy(home_id, ..., redundancy)` | Set redundancy value |
| `get_regulate_frequency_state(home_id, device_id, model)` | Read grid frequency regulation (balancing) state via E2E |
| `get_region(home_id, device_id, model)` | Device region info |
| `get_contract(home_id)` | Balance contract info |
| `get_features(home_id, device_id, model)` | Device feature flags |
| `api_request(path, json_data)` | Raw API request |

### Override Slot Values

| Value | Meaning |
|-------|---------|
| `0x80` (128) | No override — follow AI schedule |
| `0x00` (0) | Force idle (no charge/discharge) |
| 1–100 | Charge when battery < N% |
| 129–255 | Discharge when battery > (256−N)% |

The 7 standard override actions map to slot values using the configured markers (default low=20%, high=72%):

| Action | Shortcut | Slot value | Example |
|--------|----------|------------|:--------|
| `charge-low` | `cl` | `low_marker` | 20 → charge < 20% |
| `charge-high` | `ch` | `high_marker` | 72 → charge < 72% |
| `charge-100` | `cf` | `100` | charge < 100% |
| `idle` | `i` | `0` | no charge/discharge |
| `discharge-low` | `dl` | `256 − low_marker` | 236 → discharge > 20% |
| `discharge-high` | `dh` | `256 − high_marker` | 184 → discharge > 72% |
| `clear` | `x` | `128` | remove override |

### Exceptions

| Exception | When |
|-----------|------|
| `EmaldoAuthError` | Login failed or session expired |
| `EmaldoAPIError` | API returned an error status |
| `EmaldoE2EError` | E2E/UDP protocol failure |

## CLI Commands

| Command | Description |
|---------|-------------|
| `login` | Authenticate (saved to `.emaldo_session.json`) |
| `homes` | List homes (`--select N` to switch) |
| `devices` | List battery devices |
| `battery` | Battery overview |
| `battery-detail` | Per-cell battery data via E2E (`--json` for JSON output) |
| `usage` | Daily usage stats (`--offset N` for past days) |
| `revenue` | Revenue data |
| `fcr` | FCR predicted revenue |
| `schedule` | Charge/discharge schedule |
| `power` | Realtime power |
| `solar` | Solar generation (`--offset N`) |
| `grid` | Grid import/export (`--offset N`) |
| `strategy` | AI mode strategy composite view |
| `override` | Override control (`--show`, `--reset`, `--range`, `--markers`) |
| `sell` | Sell (discharge-to-grid) via E2E (`--hours`, `--until`, `--cancel`) |
| `emergency-charge` | Emergency charge via E2E (`--hours`, `--until`, `--cancel`) |
| `peak-shaving` | Peak shaving via E2E (`--show`, `--enable`, `--disable`, `--peak-reserve`, `--ups-reserve`, `--schedule`, `--all-day`, `--no-all-day`, `--redundancy`) |
| `balancing-state` | Grid frequency regulation (balancing) state via E2E (`--json`, `--verbose`) |
| `region` | Region info |
| `contract` | Contract info |
| `features` | Feature flags |
| `raw` | Raw API request (`--path`, `--json`) |

## Legal Basis

The device's built-in AI optimization sometimes makes scheduling decisions that are
not economical for the consumer. Correcting these manually is impractical due to
critically small UI elements in the official app. This API addresses both issues:
the CLI provides direct control over scheduling decisions, and the library could
serve as a foundation for integration with home automation systems like
Home Assistant, offering proper UI and automation capabilities.

This implementation is made in accordance with the following:

**Data access & interoperability**
- [EU Data Act](https://eur-lex.europa.eu/eli/reg/2023/2854) (Regulation 2023/2854,
  Chapter II, Art. 4–5) — users' right to access data generated by their own
  connected devices in a machine-readable format, and to share it with third parties
  of their choosing
- [Directive 2009/24/EC on the legal protection of computer programs](https://eur-lex.europa.eu/eli/dir/2009/24),
  Art. 6 — permits reverse engineering for interoperability purposes; any protocol
  analysis in this project was performed solely to enable interoperability with the
  user's own device and data

**Energy consumer rights**
- [EU Electricity Directive](https://eur-lex.europa.eu/eli/dir/2019/944) (Directive
  2019/944, Art. 21–23) — affirms active consumers' right to participate in energy
  markets, manage demand response, and optimize self-consumption using their own
  assets; a battery system the owner cannot effectively control undermines this right
- [EU Energy Efficiency Directive](https://eur-lex.europa.eu/eli/dir/2023/1791)
  (Directive 2023/1791, recast) — supports consumers' ability to actively monitor
  and optimize their own energy consumption

**Consumer protection**
- [Sale of Goods Directive](https://eur-lex.europa.eu/eli/dir/2019/771) (Directive
  2019/771, Art. 7) — goods must conform to their described purpose; a device sold
  as a cost-optimization tool that demonstrably increases costs and degrades itself
  through unnecessary charge cycles may constitute a conformity defect
- [EU Consumer Rights Directive](https://eur-lex.europa.eu/eli/dir/2011/83)
  (Directive 2011/83/EU) — consumers' rights with respect to digital products and
  services associated with physical goods
- [EU Unfair Commercial Practices Directive](https://eur-lex.europa.eu/eli/dir/2005/29)
  (Directive 2005/29/EC) — if a device is marketed as optimizing electricity costs
  but produces measurable adverse financial outcomes, this may constitute a
  misleading commercial practice

**Accessibility**
- [European Accessibility Act](https://eur-lex.europa.eu/eli/dir/2019/882)
  (Directive 2019/882) — supports users' right to accessible interfaces for
  interacting with their own devices and data. The official application renders
  96 individually adjustable time slots per day within approximately half the screen
  width, as the layout statically allocates space for 192 slots regardless of
  how many are visible. Each interactive control is consequently rendered at
  a size that falls far below the minimum touch target size guidelines established
  by [WCAG 2.5.5](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced)
  (44×44 CSS pixels) and
  [WCAG 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum)
  (24×24 CSS pixels). This makes precise adjustment effectively impossible for
  users with motor impairments, and impractical for most users regardless of
  ability.

## License

MIT
