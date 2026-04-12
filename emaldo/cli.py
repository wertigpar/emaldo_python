#!/usr/bin/env python3
"""Emaldo CLI - Command line tool for Emaldo battery systems.

Usage:
    python -m emaldo.cli login --email user@example.com --password pass
    python -m emaldo.cli homes
    python -m emaldo.cli battery
    python -m emaldo.cli schedule
    python -m emaldo.cli override --show
"""

import argparse
import json
import os
import sys
import time

from emaldo import EmaldoClient
from emaldo.const import (
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    MIN_MARKER_GAP,
    SLOT_CHARGE_DEFAULT,
    SLOT_IDLE,
    SLOT_NO_OVERRIDE,
    decode_slot_action,
    encode_override_action,
    price_unit_for_timezone,
)
from emaldo.exceptions import (
    EmaldoAPIError,
    EmaldoAuthError,
    EmaldoConnectionError,
    EmaldoE2EError,
)

SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".emaldo_session.json")


# ── Session persistence ──────────────────────────────────────────────────

def _make_client(args, *, session: dict | None = None) -> EmaldoClient:
    """Create a client, passing --app-version if provided."""
    from emaldo.const import get_default_app_version
    app_version = getattr(args, "app_version", None) or get_default_app_version()
    return EmaldoClient(session=session, app_version=app_version)


def load_client(args=None) -> EmaldoClient:
    """Create a client with a saved session (if any)."""
    session = {}
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE) as f:
            session = json.load(f)
    if args is not None:
        return _make_client(args, session=session)
    return EmaldoClient(session=session)


def save_session(client: EmaldoClient) -> None:
    """Persist the client session to disk."""
    with open(SESSION_FILE, "w") as f:
        json.dump(client.export_session(), f, indent=2)


# ── Auto-discovery helpers ───────────────────────────────────────────────

def get_home_id(args, client: EmaldoClient) -> str:
    """Resolve home_id from CLI args, session cache, or auto-discovery."""
    if getattr(args, "home_id", None):
        return args.home_id
    session = client.export_session()
    home_id = session.get("home_id", "")
    if not home_id:
        home_id, home_name = client.find_home()
        session["home_id"] = home_id
        client.import_session(session)
        save_session(client)
        print(f"  [Auto-selected home: {home_name}]", file=sys.stderr)
    return home_id


def get_device_id(args, client: EmaldoClient, home_id: str) -> tuple[str, str]:
    """Resolve (device_id, model) from CLI args, session cache, or auto-discovery."""
    if getattr(args, "device_id", None):
        model = getattr(args, "model", None) or client.export_session().get("model", "")
        if not model:
            raise SystemExit("Error: --model is required when using --device-id")
        return args.device_id, model
    session = client.export_session()
    device_id = session.get("device_id", "")
    model = session.get("model", "")
    if not device_id:
        device_id, model, name = client.find_device(home_id)
        session["device_id"] = device_id
        session["model"] = model
        client.import_session(session)
        save_session(client)
        print(f"  [Auto-selected device: {name} ({model})]", file=sys.stderr)
    return device_id, model


# ── CLI Commands ─────────────────────────────────────────────────────────

def cmd_login(args):
    client = _make_client(args)
    identifier = args.email or args.phone
    if not identifier:
        print("Error: --email or --phone required", file=sys.stderr)
        sys.exit(1)
    try:
        data = client.login(identifier, args.password, use_phone=bool(args.phone))
        save_session(client)
        print(f"Login successful!")
        print(f"  User ID: {data.get('user_id', '')}")
        print(f"  UID:     {data.get('uid', '')}")
        print(f"  Email:   {data.get('email', '')}")
        print(f"  Token:   {data.get('token', '')[:20]}...")
    except EmaldoAuthError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_version_check(args):
    client = _make_client(args)
    try:
        info = client.check_version()
        print(f"  Server required version: {info['version']}")
        print(f"  Mandatory update:        {'yes' if info['must'] else 'no'}")
        print(f"  Client app_version:      {client._app_version}")
        print(f"  Up to date:              {'yes' if info['up_to_date'] else 'NO'}")
        if info.get('url'):
            print(f"  Update URL:              {info['url']}")
    except EmaldoAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_homes(args):
    client = load_client(args)
    homes = client.list_homes()
    session = client.export_session()

    if getattr(args, "select", None):
        idx = args.select - 1
        if 0 <= idx < len(homes):
            h = homes[idx]
            session.pop("device_id", None)
            session.pop("model", None)
            session["home_id"] = h["home_id"]
            client.import_session(session)
            save_session(client)
            print(f"Selected home: {h.get('home_name')} ({h['home_id']})")
            return
        else:
            print(f"Error: Invalid index {args.select}. Valid: 1-{len(homes)}", file=sys.stderr)
            sys.exit(1)

    for i, h in enumerate(homes, 1):
        marker = " *" if h.get("home_id") == session.get("home_id") else ""
        print(f"  [{i}] {h.get('home_id')}  {h.get('home_name'):<20s} level={h.get('level')}{marker}")
    if not homes:
        print("No homes found.")
    else:
        print(f"\nUse 'homes --select N' to set active home. Current marked with *")


def cmd_devices(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    devices = client.list_devices(home_id)
    if devices:
        for b in devices:
            print(f"  ID: {b.get('id')}")
            print(f"  Name: {b.get('name')}")
            print(f"  Model: {b.get('model')}")
            print(f"  Country: {b.get('country_code')} / {b.get('delivery_area')}")
            print(f"  Address: {b.get('addr')}")
            print()
    else:
        print("No battery devices found in this home.")


def cmd_search(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.search_device(home_id, device_id, model)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_battery(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_battery(home_id, device_id, model)

    sensor = data["sensor"]
    bat_data = data["battery"]
    level_data = data["power_level"]
    dual = data["dual_power"]

    print(f"Battery Overview")
    print(f"  Device:       {device_id} ({model})")
    print(f"  B-Sensor:     {'installed' if sensor.get('b_sensor_installed') else 'not installed'}")
    print(f"  Reserve:      {sensor.get('reserve')}")
    print(f"  Dual Power:   {'enabled' if dual.get('is_open') else 'disabled'}")

    if isinstance(level_data, dict):
        entries = level_data.get("data", [])
        for e in reversed(entries):
            if len(e) >= 2:
                h, m = divmod(e[0], 60)
                print(f"  Current SoC:  {e[1]}% (as of {h:02d}:{m:02d})")
                break

    if isinstance(bat_data, dict):
        entries = bat_data.get("data", [])
        total_charged = sum(e[2] + e[3] for e in entries if len(e) >= 4) * 5 / 60
        total_discharged = sum(e[1] for e in entries if len(e) >= 2) * 5 / 60
        print(f"  Charged today:    {total_charged/1000:.1f} kWh")
        print(f"  Discharged today: {total_discharged/1000:.1f} kWh")
        print(f"  Net:              {(total_charged - total_discharged)/1000:.1f} kWh")


def cmd_battery_detail(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)

    def e2e_log(msg: str):
        print(f"  [E2E] {msg}", file=sys.stderr)

    print("Reading battery cell data via E2E...", file=sys.stderr)
    batteries = client.get_battery_info(home_id, device_id, model, log=e2e_log)

    if args.json_output:
        print(json.dumps(batteries, indent=2, ensure_ascii=False))
        return

    if not batteries:
        print("No battery data received.")
        return

    use_color = sys.stdout.isatty()
    if use_color:
        C_HDR, C_OK, C_WARN, C_DIM, C_R = "\033[1m", "\033[32m", "\033[33m", "\033[90m", "\033[0m"
    else:
        C_HDR = C_OK = C_WARN = C_DIM = C_R = ""

    # Sort by cabinet index
    batteries.sort(key=lambda b: b.get("cabinet_index", 0))

    # Summary line
    total_cur = sum(b["current_energy_wh"] for b in batteries)
    total_full = sum(b["full_energy_wh"] for b in batteries)
    avg_soc = sum(b["soc"] for b in batteries) / len(batteries)
    total_power_w = sum(b["voltage_v"] * abs(b["current_a"]) for b in batteries)

    print(f"{C_HDR}Battery Cells{C_R}  ({len(batteries)} cells)")
    print(f"  Total Energy: {total_cur:,} / {total_full:,} Wh ({avg_soc:.0f}% avg SoC)")
    if total_power_w > 0:
        direction = "discharging" if batteries[0]["current_a"] < 0 else "charging"
        print(f"  Total Power:  {total_power_w:.0f} W ({direction})")
    print()

    for b in batteries:
        status_parts = []
        if b["discharge_on"]:
            status_parts.append("discharge")
        if b["charge_on"]:
            status_parts.append("charge")
        status = ", ".join(status_parts) if status_parts else "off"

        soc_color = C_OK if b["soc"] >= 50 else C_WARN if b["soc"] >= 20 else "\033[31m" if use_color else ""
        soh_color = C_OK if b["soh"] >= 90 else C_WARN

        print(f"  {C_HDR}Cell #{b['cabinet_index']}{C_R}  {C_DIM}{b['serial']}{C_R}")
        print(f"    Model:       {b['model']}")
        print(f"    SoC:         {soc_color}{b['soc']}%{C_R}   SoH: {soh_color}{b['soh']}%{C_R}   Cycles: {b['cycle_count']}")
        print(f"    Voltage:     {b['voltage_v']:.2f} V")
        print(f"    Current:     {b['current_a']:.2f} A ({status})")
        print(f"    Energy:      {b['current_energy_wh']:,} / {b['full_energy_wh']:,} Wh")
        print(f"    BMS Temp:    {b['bms_temp_c']:.1f} °C")
        print(f"    Cell Temps:  A={b['electrode_a_temp_c']:.1f} °C  B={b['electrode_b_temp_c']:.1f} °C")
        if b["fault_bits"]:
            print(f"    {C_WARN}Faults:      0x{b['fault_bits']:04x}{C_R}")
        if b["capacity"]:
            print(f"    Capacity:    {b['capacity']} W")
        print()


def cmd_usage(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_usage(home_id, device_id, model, offset=args.offset)

    usage = data["usage"]
    bat = data["battery"]
    solar = data["solar"]
    grid = data["grid"]
    level = data["power_level"]

    tz = usage.get("timezone", "") if isinstance(usage, dict) else ""
    interval = usage.get("interval", 5) if isinstance(usage, dict) else 5
    print(f"Usage Stats  (offset={args.offset}, tz={tz}, interval={interval}min)")
    print()

    if isinstance(usage, dict):
        entries = usage.get("data", [])
        total_usage = sum(e[2] for e in entries if len(e) >= 3) * interval / 60 / 1000
        peak_power = max((e[2] for e in entries if len(e) >= 3), default=0)
        print(f"  Usage:       {total_usage:.1f} kWh,  Power Peak {peak_power/1000:.1f} kW")

    if isinstance(bat, dict):
        entries = bat.get("data", [])
        total_charged = sum(e[2] + e[3] for e in entries if len(e) >= 4) * interval / 60 / 1000
        total_discharged = sum(e[1] for e in entries if len(e) >= 2) * interval / 60 / 1000
        net = total_charged - total_discharged
        print(f"  Battery-Net: {abs(net):.1f} kWh ({'charged' if net > 0 else 'discharged'}),"
              f"  Discharged {total_discharged:.1f} kWh,  Charged {total_charged:.1f} kWh")

    if isinstance(solar, dict):
        entries = solar.get("data", [])
        total_solar = 0
        peak_solar = 0
        for e in entries:
            val = e[5] if len(e) >= 6 else (e[1] if len(e) >= 2 else 0)
            total_solar += val
            peak_solar = max(peak_solar, val)
        total_solar_kwh = total_solar * interval / 60 / 1000
        print(f"  Solar:       {total_solar_kwh:.1f} kWh,  Peak Power {peak_solar}W")

    if isinstance(grid, dict):
        entries = grid.get("data", [])
        total_import = sum(e[1] for e in entries if len(e) >= 2) * interval / 60 / 1000
        total_export = sum(e[2] for e in entries if len(e) >= 3) * interval / 60 / 1000
        net_grid = total_import - total_export
        print(f"  Grid-Net:    {abs(net_grid):.1f} kWh ({'imported' if net_grid > 0 else 'exported'}),"
              f"  Imported {total_import:.1f} kWh,  Exported {total_export:.1f} kWh")

    if isinstance(level, dict) and not args.no_graph:
        entries = level.get("data", [])
        print(f"\n  Battery SoC:")
        for e in entries:
            if len(e) >= 2 and e[0] % 30 == 0:
                h, m = divmod(e[0], 60)
                bar = "#" * (e[1] // 2)
                print(f"    {h:02d}:{m:02d}  {e[1]:3d}% |{bar}")

    if args.graph:
        print(f"\n  Usage Graph (5-min intervals):")
        if isinstance(usage, dict):
            for e in usage.get("data", []):
                if len(e) >= 4 and any(v != 0 for v in e[1:]):
                    h, m = divmod(e[0], 60)
                    print(f"    {h:02d}:{m:02d}  usage={e[2]}W  charge={e[1]}W  grid={e[3]}W")


def cmd_revenue(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_revenue(home_id, device_id, model, offset=args.offset)

    if isinstance(data, dict):
        print(f"  Start time: {data.get('start_time')}")
        print(f"  Timezone: {data.get('timezone')}")
        print(f"  Interval: {data.get('interval')} minutes")
        entries = data.get("data", [])
        print(f"  Data points: {len(entries)}")
        for entry in entries:
            if len(entry) >= 3:
                h, m = divmod(entry[0], 60)
                print(f"    {h:02d}:{m:02d}  {entry[1]}  {entry[2]}")
    else:
        print(json.dumps(data, indent=2))


def cmd_fcr(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    data = client.get_fcr(home_id)

    if isinstance(data, dict):
        cur = data.get("currency", "")
        print(f"  Monthly predicted revenue: {data.get('monthly_pr', 0)/100:.2f} {cur}")
        print(f"  Daily predicted revenue:   {data.get('daily_pr', 0)/100:.2f} {cur}")
        print(f"  Total actual revenue:      {data.get('total_ar', 0)/100:.2f} {cur}")
        print(f"  Last month PR:             {data.get('last_monthly_pr', 0)/100:.2f} {cur}")
        print(f"  Last month DA posted:      {data.get('last_month_da_posted')}")
        print(f"  Model type:                {data.get('last_month_model_type')}")
    else:
        print(json.dumps(data, indent=2))


def cmd_schedule(args):
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_schedule(home_id, device_id, model)

    if not isinstance(data, dict) or "hope_charge_discharges" not in data:
        print(json.dumps(data, indent=2))
        return

    slots = data["hope_charge_discharges"]
    prices = data.get("market_prices", [])
    solar = data.get("forecast_solars", [])
    smart = data.get("smart", 0)
    emergency = data.get("emergency", 0)
    start_time = data.get("start_time", 0)
    tz_name = data.get("timezone", "UTC")
    gap = data.get("gap", 15)
    p_unit = price_unit_for_timezone(tz_name)

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    day_start = datetime.fromtimestamp(start_time, tz)
    elapsed_sec = (now - day_start).total_seconds()
    current_slot = max(0, int(elapsed_sec / (gap * 60)))
    total_slots = len(slots)

    use_color = sys.stdout.isatty()
    if use_color:
        C_CHG, C_DSC, C_IDLE = "\033[32m", "\033[33m", "\033[90m"
        C_HDR, C_DIM, C_R = "\033[1m", "\033[2m", "\033[0m"
    else:
        C_CHG = C_DSC = C_IDLE = C_HDR = C_DIM = C_R = ""

    def action_label(v):
        if v == 100:
            return "Charge", C_CHG
        elif v < 0:
            return "Discharge", C_DSC
        else:
            return "Idle", C_IDLE

    print(f"{C_HDR}Schedule{C_R}  Smart={smart}%  Emergency={emergency}%")
    print()

    counts: dict[str, int] = {"Charge": 0, "Discharge": 0, "Idle": 0}
    price_sum: dict[str, float] = {"Charge": 0.0, "Discharge": 0.0}
    last_day = None

    for i in range(current_slot, total_slots):
        slot_time = day_start + timedelta(minutes=i * gap)
        day_num = i // 96

        if day_num != last_day:
            if last_day is not None:
                print()
            label = "Today" if day_num == 0 else "Tomorrow"
            day_str = slot_time.strftime("%a %-d %b")
            print(f"  {C_HDR}\u2500\u2500 {label}, {day_str} {'─' * 28}{C_R}")
            last_day = day_num

        v = slots[i]
        slot_price = prices[i] if i < len(prices) else 0
        sol = solar[i] if i < len(solar) else 0

        act, color = action_label(v)
        counts[act] = counts.get(act, 0) + 1
        if act in price_sum:
            price_sum[act] += slot_price

        price_c = slot_price * 100
        solar_str = f"  \u2600 {sol} Wh" if sol and sol > 0 else ""
        time_str = slot_time.strftime("%H:%M")
        print(f"  {time_str}  {color}{act:<10s}{C_R}  {price_c:5.1f} {p_unit}{solar_str}")

    print()
    remaining = total_slots - current_slot
    parts = []
    for act in ("Charge", "Discharge", "Idle"):
        n = counts.get(act, 0)
        if n:
            hrs = n * gap / 60
            parts.append(f"{act}: {hrs:.1f}h")
    print(f"  {C_DIM}Remaining {remaining} slots ({remaining * gap / 60:.0f}h): {', '.join(parts)}{C_R}")
    if counts.get("Charge", 0) and price_sum.get("Charge", 0):
        avg_chg = price_sum["Charge"] / counts["Charge"] * 100
        print(f"  {C_DIM}Avg charge price: {avg_chg:.1f} {p_unit}{C_R}")
    if counts.get("Discharge", 0) and price_sum.get("Discharge", 0):
        avg_dsc = price_sum["Discharge"] / counts["Discharge"] * 100
        print(f"  {C_DIM}Avg discharge price: {avg_dsc:.1f} {p_unit}{C_R}")


def cmd_power(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_power(home_id, device_id, model)

    def latest(d):
        if not isinstance(d, dict):
            return None
        entries = d.get("data", [])
        for e in reversed(entries):
            if len(e) >= 2 and any(v != 0 for v in e[1:]):
                return e
        return entries[-1] if entries else None

    print("Realtime Power")

    g = latest(data["grid"])
    if g and len(g) >= 3:
        h, m = divmod(g[0], 60)
        net = g[1] - g[2]
        print(f"  Grid:        {net/1000:.1f} kW  (import {g[1]}W, export {g[2]}W)  [{h:02d}:{m:02d}]")

    b = latest(data["battery"])
    if b and len(b) >= 4:
        h, m = divmod(b[0], 60)
        net = b[1] - b[3]
        if net > 0:
            print(f"  Battery:     +{net/1000:.1f} kW  (charging {b[1]}W)  [{h:02d}:{m:02d}]")
        elif net < 0:
            print(f"  Battery:     {net/1000:.1f} kW  (discharging {b[3]}W)  [{h:02d}:{m:02d}]")
        else:
            print(f"  Battery:     0 kW  (idle)  [{h:02d}:{m:02d}]")

    dp = data["dual_power"]
    dp_status = "enabled" if dp.get("is_open") else "disabled"

    u = latest(data["usage"])
    if u and len(u) >= 4:
        h, m = divmod(u[0], 60)
        print(f"  Load:        {u[2]/1000:.1f} kW  [{h:02d}:{m:02d}]")
    print(f"  Dual Power:  {dp_status}")


def cmd_power_e2e(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)

    def e2e_log(msg: str):
        if args.verbose:
            print(f"  [E2E] {msg}", file=sys.stderr)

    print("Reading power flow via E2E (type 0x30)...", file=sys.stderr)
    data = client.get_power_flow(home_id, device_id, model, log=e2e_log)

    if args.json_output:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if not data:
        print("No power flow data received.")
        return

    use_color = sys.stdout.isatty()
    if use_color:
        C_HDR, C_OK, C_WARN, C_DIM, C_R = "\033[1m", "\033[32m", "\033[33m", "\033[90m", "\033[0m"
    else:
        C_HDR = C_OK = C_WARN = C_DIM = C_R = ""

    is_power_core = model.startswith("PC")

    print(f"{C_HDR}Realtime Power Flow (E2E){C_R}")
    print(f"  {C_DIM}Device: {model}{' (Power Core)' if is_power_core else ''}{C_R}")

    bat = data.get("battery_w", 0)
    if bat > 0:
        print(f"  Battery:     {C_WARN}+{bat} W{C_R}  (charging)")
    elif bat < 0:
        print(f"  Battery:     {C_OK}{bat} W{C_R}  (discharging)")
    else:
        print(f"  Battery:     0 W  (idle)")

    grid = data.get("grid_w", 0)
    if grid > 0:
        print(f"  Grid:        {C_WARN}+{grid} W{C_R}  (importing)")
    elif grid < 0:
        print(f"  Grid:        {C_OK}{grid} W{C_R}  (exporting)")
    else:
        print(f"  Grid:        0 W")

    dual = data.get("dual_power_w", 0)
    if dual < 0:
        print(f"  Consumption: {C_WARN}{abs(dual)} W{C_R}")
    elif dual > 0:
        print(f"  Consumption: {C_OK}-{dual} W{C_R}  (producing)")
    else:
        print(f"  Consumption: 0 W")

    if is_power_core:
        solar = data.get("solar_w", 0)
        print(f"  Solar:       {C_OK}{solar} W{C_R}" if solar > 0 else f"  Solar:       {solar} W")

        ev = data.get("ev_w", 0)
        if ev:
            print(f"  Car Charge:  {C_WARN}{ev} W{C_R}")
        else:
            print(f"  Car Charge:  0 W")

    other = data.get("other_load_w", 0)
    if other:
        print(f"  Backup Box:  {other} W")

    gv = data.get("grid_valid", False)
    bv = data.get("bsensor_valid", False)
    print(f"  {C_DIM}Grid valid: {gv}  BSensor valid: {bv}{C_R}")


def cmd_power_debug(args):
    import json as _json
    client = load_client(args)
    home_id = get_home_id(args, client)

    # Use the same endpoint the official app uses for realtime power
    json_data = {
        "home_id": home_id,
        "models": [],
        "page_size": 30,
        "addtime": 1,
        "order": "asc",
    }
    result = client.api_request("/bmt/list-bmt/", json_data=json_data)
    data = result.get("Result", {})

    print("Raw /bmt/list-bmt/ response")
    print("=" * 60)

    if isinstance(data, dict):
        # Print top-level keys (excluding bmts for separate handling)
        top_keys = {k: v for k, v in data.items() if k != "bmts"}
        if top_keys:
            print(f"\nTop-level keys: {list(data.keys())}")
            for k, v in top_keys.items():
                print(f"  {k}: {_json.dumps(v, indent=4) if isinstance(v, (dict, list)) else v}")

        bmts = data.get("bmts", [])
        for i, bmt in enumerate(bmts):
            print(f"\n{'='*60}")
            print(f"  Device {i}: {bmt.get('name', '?')} ({bmt.get('id', '?')})")
            print(f"{'='*60}")
            for k in sorted(bmt.keys()):
                v = bmt[k]
                if isinstance(v, (dict, list)):
                    print(f"  {k}: {_json.dumps(v, indent=4)}")
                else:
                    print(f"  {k}: {v}")
    else:
        print(_json.dumps(data, indent=2))


def cmd_solar(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_solar(home_id, device_id, model, offset=args.offset)

    if isinstance(data, dict):
        entries = data.get("data", [])
        interval = data.get("interval", 5)
        total = 0
        peak = 0
        for e in entries:
            val = e[4] if len(e) >= 5 else (e[1] if len(e) >= 2 else 0)
            total += val
            peak = max(peak, val)

        total_kwh = total * interval / 60 / 1000
        print(f"Solar Generation  (offset={args.offset})")
        print(f"  Total:      {total_kwh:.1f} kWh")
        print(f"  Peak Power: {peak}W ({peak/1000:.1f}kW)")
        print()

        for e in entries:
            if len(e) >= 5:
                mins, pv_total = e[0], e[4]
            elif len(e) >= 2:
                mins, pv_total = e[0], e[1]
            else:
                continue
            if pv_total > 0 or mins % 60 == 0:
                h, m = divmod(mins, 60)
                bar = "#" * (pv_total // 100) if pv_total > 0 else ""
                print(f"    {h:02d}:{m:02d}  {pv_total:5d}W  {bar}")
    else:
        print(json.dumps(data, indent=2))


def cmd_grid(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_grid(home_id, device_id, model, offset=args.offset)

    if isinstance(data, dict):
        entries = data.get("data", [])
        interval = data.get("interval", 5)
        total_import = sum(e[1] for e in entries if len(e) >= 2) * interval / 60 / 1000
        total_export = sum(e[2] for e in entries if len(e) >= 3) * interval / 60 / 1000
        peak_import = max((e[1] for e in entries if len(e) >= 2), default=0)

        print(f"Grid Stats  (offset={args.offset})")
        print(f"  Net:      {abs(total_import - total_export):.1f} kWh ({'imported' if total_import > total_export else 'exported'})")
        print(f"  Imported: {total_import:.1f} kWh  (peak {peak_import/1000:.1f}kW)")
        print(f"  Exported: {total_export:.1f} kWh")
        print()

        for e in entries:
            if len(e) >= 3 and (e[1] > 0 or e[2] > 0 or e[0] % 60 == 0):
                h, m = divmod(e[0], 60)
                imp_bar = "+" * (e[1] // 200) if e[1] > 0 else ""
                exp_bar = "-" * (e[2] // 200) if e[2] > 0 else ""
                print(f"    {h:02d}:{m:02d}  import={e[1]:5d}W  export={e[2]:5d}W  {imp_bar}{exp_bar}")
    else:
        print(json.dumps(data, indent=2))


def cmd_strategy(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_strategy(home_id, device_id, model)

    summary = data["fcr_summary"]
    daily = data["fcr_daily"]
    sched = data["schedule"]
    price = data["price_thresholds"]
    rev = data["revenue"]

    print("AI Mode Strategy")
    print()

    if isinstance(summary, dict):
        cur = summary.get("currency", "EUR")
        print(f"  Revenue Summary ({cur}):")
        print(f"    Monthly predicted: {summary.get('monthly_pr', 0)/100:.2f}")
        print(f"    Daily predicted:   {summary.get('daily_pr', 0)/100:.2f}")
        print(f"    Total actual:      {summary.get('total_ar', 0)/100:.2f}")
        print()

    if isinstance(daily, dict):
        cur = daily.get("currency", "EUR")
        entries = daily.get("data", [])
        print(f"  FCR Daily Predictions ({cur}):")
        for e in entries:
            if len(e) >= 2:
                print(f"    Day {e[0]}: {e[1]/100:.2f} {cur}")
        print()

    if isinstance(price, dict):
        print(f"  Price Thresholds:")
        print(f"    Smart reserve:     {price.get('smart_reserve')}%")
        print(f"    Emergency reserve: {price.get('emergency_reserve')}%")
        print(f"    Plenty reserve:    {price.get('plenty_reserve')}%")
        print(f"    C1={price.get('c1')}, C2={price.get('c2')}, C3={price.get('c3')}")
        print(f"    S1={price.get('s1')}, S2={price.get('s2')}")
        print()

    if isinstance(sched, dict) and isinstance(rev, dict):
        slots = sched.get("hope_charge_discharges", [])
        rev_entries = rev.get("data", [])

        print(f"  Charge/Discharge Plan + Revenue:")
        print(f"    {'Hour':>6s}  {'Plan':>8s}  {'Revenue':>10s}")
        print(f"    {'----':>6s}  {'----':>8s}  {'-------':>10s}")

        hourly_rev: dict[int, float] = {}
        for e in rev_entries:
            if len(e) >= 3:
                h = e[0] // 60
                hourly_rev[h] = hourly_rev.get(h, 0) + e[1] + e[2]

        for h in range(24):
            plan_parts = []
            for q in range(4):
                idx = h * 4 + q
                if idx < len(slots):
                    v = slots[idx]
                    if v == 100:
                        plan_parts.append("CHG")
                    elif v < 0:
                        plan_parts.append(f"{v}")
                    else:
                        plan_parts.append("---")
            plan_str = "/".join(plan_parts)
            rev_val = hourly_rev.get(h, 0)
            rev_str = f"{rev_val/100:.2f}" if rev_val else "-"
            print(f"    {h:02d}:00   {plan_str:<20s}  {rev_str:>6s}")


def cmd_region(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_region(home_id, device_id, model)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_contract(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    data = client.get_contract(home_id)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_features(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)
    data = client.get_features(home_id, device_id, model)
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_raw(args):
    client = load_client(args)
    json_data = None
    if args.json:
        json_data = json.loads(args.json)
    result = client.api_request(args.path, json_data=json_data, need_token=not args.no_token)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_encrypt(args):
    from emaldo.crypto import encrypt_field as _enc
    print(_enc(args.text))


def cmd_decrypt(args):
    from emaldo.crypto import decrypt_response as _dec
    print(_dec(args.hex))


def _cmd_manual_control(args, *, command_name: str = "sell"):
    """Shared implementation for sell / emergency-charge (same E2E protocol)."""
    from datetime import datetime, timedelta
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)

    def e2e_log(msg: str):
        print(f"  [E2E] {msg}", file=sys.stderr)

    verbose = getattr(args, "verbose", False)
    log = e2e_log if verbose else None

    if args.cancel:
        try:
            cancel_label = f"Cancel {command_name}"
            success = client.cancel_sell(home_id, device_id, model, label=cancel_label, log=log)
            if success:
                print(f"{command_name.title()} cancelled.")
            else:
                print("Cancel sent but no acknowledgement received.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Determine duration
    duration_seconds = None

    if args.hours:
        duration_seconds = int(args.hours * 3600)
    elif args.until:
        # Parse HH:MM or YYYY-MM-DD HH:MM
        now = datetime.now()
        try:
            if len(args.until) <= 5:  # HH:MM
                parts = args.until.split(":")
                target = now.replace(
                    hour=int(parts[0]), minute=int(parts[1]),
                    second=0, microsecond=0,
                )
                if target <= now:
                    target += timedelta(days=1)
            else:
                target = datetime.strptime(args.until, "%Y-%m-%d %H:%M")
        except (ValueError, IndexError):
            print(f"Error: cannot parse time '{args.until}'. Use HH:MM or YYYY-MM-DD HH:MM",
                  file=sys.stderr)
            sys.exit(1)
        duration_seconds = int((target - now).total_seconds())
        if duration_seconds <= 0:
            print("Error: target time is in the past.", file=sys.stderr)
            sys.exit(1)

    if duration_seconds is None:
        print(f"Usage: {command_name} --hours N | --until HH:MM | --cancel")
        print()
        print("Options:")
        print("  --hours N         Duration in hours (accepts decimals, e.g. 2.5)")
        print("  --until HH:MM     Active until the specified time")
        print("  --cancel          Cancel active command")
        print("  --verbose         Show E2E protocol details")
        print()
        print(f"Examples:")
        print(f"  {command_name} --hours 4")
        print(f"  {command_name} --hours 0.5")
        print(f"  {command_name} --until 18:00")
        print(f"  {command_name} --cancel")
        sys.exit(0)

    hours = duration_seconds / 3600
    end_time = datetime.now() + timedelta(seconds=duration_seconds)
    print(f"{command_name.title()} for {hours:.1f} hours (until {end_time.strftime('%Y-%m-%d %H:%M')})")

    if args.dry_run:
        print("  [Dry run - not sending]")
        return

    try:
        success = client.send_sell(
            home_id, device_id, model, duration_seconds,
            label=command_name.title(), log=log,
        )
        if success:
            print(f"  {command_name.title()} command sent successfully!")
        else:
            print(f"  Command sent but no acknowledgement received.", file=sys.stderr)
    except (EmaldoE2EError, EmaldoAPIError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_sell(args):
    """Handle the 'sell' subcommand."""
    _cmd_manual_control(args, command_name="sell")


def cmd_emergency_charge(args):
    """Handle the 'emergency-charge' subcommand."""
    _cmd_manual_control(args, command_name="emergency-charge")


def cmd_override(args):
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)

    def e2e_log(msg: str):
        print(f"  [E2E] {msg}", file=sys.stderr)

    # ── --show ─────────────────────────────────────────────────────
    if args.show:
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        data = client.get_schedule(home_id, device_id, model)
        if not isinstance(data, dict) or "hope_charge_discharges" not in data:
            print("Error: Could not read current schedule.", file=sys.stderr)
            sys.exit(1)

        slots = data["hope_charge_discharges"]
        prices = data.get("market_prices", [])
        solar = data.get("forecast_solars", [])
        smart = data.get("smart", 0)
        emergency = data.get("emergency", 0)
        start_time = data.get("start_time", 0)
        tz_name = data.get("timezone", "UTC")
        gap = data.get("gap", 15)
        p_unit = price_unit_for_timezone(tz_name)

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)
        day_start = datetime.fromtimestamp(start_time, tz)
        elapsed_sec = (now - day_start).total_seconds()
        current_slot = max(0, int(elapsed_sec / (gap * 60)))
        total_slots = len(slots)

        use_color = sys.stdout.isatty()
        if use_color:
            C_CHG, C_DSC, C_IDLE, C_OVR = "\033[32m", "\033[33m", "\033[90m", "\033[36m"
            C_HDR, C_DIM, C_R = "\033[1m", "\033[2m", "\033[0m"
        else:
            C_CHG = C_DSC = C_IDLE = C_OVR = C_HDR = C_DIM = C_R = ""

        def action_label(v, is_override=False):
            if is_override:
                if v == SLOT_IDLE:
                    return "Idle*", C_OVR
                elif v == SLOT_NO_OVERRIDE:
                    return "", ""
                elif 1 <= v <= 100:
                    return f"Chg{v}%*", C_OVR
                elif v > 128:
                    thr = 256 - v
                    return f"Dsc{thr}%*", C_OVR
                else:
                    return f"?{v}*", C_OVR
            if v == 100:
                return "Charge", C_CHG
            elif v < 0:
                return "Discharge", C_DSC
            else:
                return "Idle", C_IDLE

        # Read E2E overrides
        e2e_state = None
        e2e_overrides = None
        marker_high = DEFAULT_MARKER_HIGH
        marker_low = DEFAULT_MARKER_LOW
        try:
            print("  [E2E] Reading overrides...", file=sys.stderr)
            e2e_state = client.get_overrides(home_id, device_id, model, log=e2e_log)
            if e2e_state is not None:
                e2e_overrides = e2e_state["slots"]
                marker_high = e2e_state["high_marker"]
                marker_low = e2e_state["low_marker"]
        except Exception as ex:
            print(f"  [E2E] Could not read overrides: {ex}", file=sys.stderr)

        has_overrides = e2e_overrides is not None and any(
            v != SLOT_NO_OVERRIDE for v in e2e_overrides
        )

        print(f"{C_HDR}Schedule{C_R}  Smart={smart}%  Emergency={emergency}%  Markers: low={marker_low}% high={marker_high}%")
        if has_overrides:
            n_ovr = sum(1 for v in e2e_overrides if v != SLOT_NO_OVERRIDE)
            print(f"  {C_DIM}Slots marked * are E2E overrides ({n_ovr} active){C_R}")
        elif e2e_overrides is not None:
            print(f"  {C_DIM}No active overrides{C_R}")
        else:
            print(f"  {C_DIM}Override state unknown (E2E read failed){C_R}")
        print()

        counts: dict[str, int] = {"Charge": 0, "Discharge": 0, "Idle": 0}
        price_sum: dict[str, float] = {"Charge": 0.0, "Discharge": 0.0}
        last_day = None

        for i in range(current_slot, total_slots):
            slot_time = day_start + timedelta(minutes=i * gap)
            day_num = 0 if i < 96 else 1

            if day_num != last_day:
                if last_day is not None:
                    print()
                label = "Today" if day_num == 0 else "Tomorrow"
                day_str = slot_time.strftime("%a %-d %b")
                print(f"  {C_HDR}\u2500\u2500 {label}, {day_str} \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500{C_R}")
                last_day = day_num

            v = slots[i] if i < total_slots else 0
            slot_price = prices[i] if i < len(prices) else 0
            sol = solar[i] if i < len(solar) else 0

            ovr_val = None
            if e2e_overrides:
                # Rolling 24h model: E2E positions map to time-of-day,
                # but which DAY depends on current_slot:
                #   positions >= current_slot → today
                #   positions <  current_slot → tomorrow
                if i < 96 and i >= current_slot:
                    # Today's remaining slots → E2E position = i
                    if e2e_overrides[i] != SLOT_NO_OVERRIDE:
                        ovr_val = e2e_overrides[i]
                elif i >= 96:
                    # Tomorrow's slots → E2E position = i-96 (wrapped)
                    e2e_idx = i - 96
                    if e2e_idx < current_slot and e2e_idx < 96 and e2e_overrides[e2e_idx] != SLOT_NO_OVERRIDE:
                        ovr_val = e2e_overrides[e2e_idx]

            if ovr_val is not None:
                act, color = action_label(ovr_val, is_override=True)
            else:
                act, color = action_label(v)

            base_act = act.rstrip("*").strip()
            if base_act.startswith("Chg"):
                base_act = "Charge"
            if base_act in counts:
                counts[base_act] += 1
            if base_act in price_sum:
                price_sum[base_act] += slot_price

            price_c = slot_price * 100
            solar_str = f"  \u2600 {sol} Wh" if sol and sol > 0 else ""
            time_str = slot_time.strftime("%H:%M")
            print(f"  {time_str}  {color}{act:<10s}{C_R}  {price_c:5.1f} {p_unit}{solar_str}")

        print()
        remaining = total_slots - current_slot
        parts = []
        for act in ("Charge", "Discharge", "Idle"):
            n = counts.get(act, 0)
            if n:
                hrs = n * gap / 60
                parts.append(f"{act}: {hrs:.1f}h")
        print(f"  {C_DIM}Remaining {remaining} slots ({remaining * gap / 60:.0f}h): {', '.join(parts)}{C_R}")
        if counts.get("Charge", 0) and price_sum.get("Charge", 0):
            avg_chg = price_sum["Charge"] / counts["Charge"] * 100
            print(f"  {C_DIM}Avg charge price: {avg_chg:.1f} {p_unit}{C_R}")
        if counts.get("Discharge", 0) and price_sum.get("Discharge", 0):
            avg_dsc = price_sum["Discharge"] / counts["Discharge"] * 100
            print(f"  {C_DIM}Avg discharge price: {avg_dsc:.1f} {p_unit}{C_R}")
        return

    # ── Parsing helpers ────────────────────────────────────────────

    def parse_time_to_slot(time_str):
        parts = time_str.split(":")
        h, m = int(parts[0]), int(parts[1])
        m = (m // 15) * 15
        slot = h * 4 + m // 15
        if slot < 0 or slot > 95:
            raise ValueError(f"Slot {slot} out of range 0-95")
        return slot

    def parse_action_to_e2e(action_str, low=DEFAULT_MARKER_LOW, high=DEFAULT_MARKER_HIGH):
        action = action_str.lower().strip()
        # Named marker-relative actions
        if action in ("charge-low", "cl"):
            return low
        elif action in ("charge-high", "ch"):
            return high
        elif action in ("charge-100", "c100", "cf"):
            return 100
        elif action in ("discharge-low", "dl"):
            return (256 - low) & 0xFF
        elif action in ("discharge-high", "dh"):
            return (256 - high) & 0xFF
        elif action in ("discharge", "dsc", "d"):
            return (256 - high) & 0xFF
        elif action in ("charge", "chg", "c"):
            return high
        elif action in ("idle", "off", "stop", "i", "0"):
            return SLOT_IDLE
        elif action in ("clear", "none", "auto", "x"):
            return SLOT_NO_OVERRIDE
        elif action.startswith(("charge", "chg")):
            num = "".join(c for c in action if c.isdigit())
            return int(num) if num else high
        elif action.startswith(("discharge", "dsc")):
            num = "".join(c for c in action if c.isdigit())
            return (256 - int(num)) & 0xFF if num else (256 - high) & 0xFF
        else:
            try:
                v = int(action_str)
                if 0 <= v <= 255:
                    return v
                raise ValueError(f"Value {v} out of range 0-255")
            except ValueError:
                raise ValueError(
                    f"Unknown action '{action_str}'. Use: charge, charge-low (cl), "
                    f"charge-high (ch), charge-100, idle, discharge (d), "
                    f"discharge-low (dl), discharge-high (dh), clear, "
                    f"or a number 0-255 (0=idle, 128=no-override)"
                )

    # Resolve markers: --markers > current from device > defaults
    marker_high = DEFAULT_MARKER_HIGH
    marker_low = DEFAULT_MARKER_LOW

    if hasattr(args, 'markers') and args.markers:
        marker_low, marker_high = args.markers
        if marker_high - marker_low < MIN_MARKER_GAP:
            print(f"Error: markers must be at least {MIN_MARKER_GAP}% apart "
                  f"(got low={marker_low}, high={marker_high})", file=sys.stderr)
            sys.exit(1)
        if not (0 <= marker_low <= 100 and 0 <= marker_high <= 100):
            print("Error: markers must be 0-100", file=sys.stderr)
            sys.exit(1)
        print(f"Using markers: low={marker_low}% high={marker_high}%")
    elif not args.reset:
        # Read current markers from device
        try:
            e2e_state = client.get_overrides(home_id, device_id, model, log=e2e_log)
            if e2e_state:
                marker_high = e2e_state["high_marker"]
                marker_low = e2e_state["low_marker"]
                print(f"Current markers: low={marker_low}% high={marker_high}%")
        except Exception:
            print(f"  Using default markers: low={marker_low}% high={marker_high}%")

    # Build 96-byte slot array
    slot_values = bytearray([SLOT_NO_OVERRIDE] * 96)

    if args.reset:
        print("Resetting all overrides (all slots → follow base schedule)")

    elif args.range:
        for spec in args.range:
            try:
                time_range, action_str = spec.split("=")
                start_str, end_str = time_range.split("-")
                start_slot = parse_time_to_slot(start_str)
                end_slot = parse_time_to_slot(end_str)
                val = parse_action_to_e2e(action_str, marker_low, marker_high)
                if end_slot <= start_slot:
                    end_slot += 96
                for i in range(start_slot, end_slot):
                    slot_values[i % 96] = val
            except Exception as e:
                print(f"Error parsing range '{spec}': {e}", file=sys.stderr)
                sys.exit(1)

    elif args.slots:
        for spec in args.slots:
            try:
                time_part, action_part = spec.split("=")
                if ":" in time_part:
                    slot_idx = parse_time_to_slot(time_part)
                else:
                    slot_idx = int(time_part)
                slot_values[slot_idx] = parse_action_to_e2e(action_part, marker_low, marker_high)
            except Exception as e:
                print(f"Error parsing '{spec}': {e}", file=sys.stderr)
                sys.exit(1)
    else:
        print("Usage: override [options] [HH:MM=action ...]")
        print()
        print("Actions:")
        print("  charge (c)      - Charge at high marker %")
        print("  charge-low (cl) - Charge at low marker %")
        print("  charge-high (ch)- Charge at high marker %")
        print("  charge-100 (cf) - Charge at 100%")
        print("  chargeNN        - Charge at NN%")
        print("  idle (i, 0)     - No charge/discharge")
        print("  discharge (d)   - Discharge at high marker %")
        print("  discharge-low (dl)  - Discharge at low marker %")
        print("  discharge-high (dh) - Discharge at high marker %")
        print("  dischargeNN     - Discharge at NN%")
        print("  clear (x)       - Remove override (follow AI)")
        print()
        print("Options:")
        print("  --markers LOW HIGH  - Set battery markers (default 20 72)")
        print("  --range HH:MM-HH:MM=ACTION  - Override a time range")
        print("  --reset             - Clear all overrides")
        print("  --show              - Show current schedule + overrides")
        print("  --dry-run           - Preview without sending")
        print()
        print("Examples:")
        print("  override --range 01:00-05:00=charge-low")
        print("  override --range 17:00-20:00=discharge-high")
        print("  override --range 21:00-22:00=idle")
        print("  override --markers 21 73 --range 01:00-05:00=cl")
        print("  override --reset")
        print("  override --show")
        sys.exit(0)

    # Show planned overrides (with rolling day labels)
    active = [(i, v) for i, v in enumerate(slot_values) if v != SLOT_NO_OVERRIDE]
    if active:
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now = datetime.now()
        now_slot = (now.hour * 60 + now.minute) // 15
        print(f"Override slots ({len(active)}):")
        for idx, val in active:
            h = idx // 4
            m = (idx % 4) * 15
            day_label = "today" if idx >= now_slot else "tomorrow"
            desc = decode_slot_action(val, marker_low, marker_high)
            print(f"  Slot {idx:2d} ({h:02d}:{m:02d} {day_label}): {desc}")
    elif not args.reset:
        print("No overrides to send.")
        return

    # Send via E2E
    if args.dry_run:
        print("\n  [Dry run - not sending]")
        return

    try:
        success = client.set_override(
            home_id, device_id, model, bytes(slot_values),
            high_marker=marker_high, low_marker=marker_low, log=e2e_log
        )
        if success:
            print("\n  Override sent successfully!")
    except (EmaldoE2EError, EmaldoAPIError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_DAY_LOOKUP = {n.lower(): i for i, n in enumerate(_DAY_NAMES)}
# Also accept full names and single-letter abbreviations
for _i, _full in enumerate(["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]):
    _DAY_LOOKUP[_full] = _i


def _format_days(bitmask: int) -> str:
    """Format a repeat-days bitmask as human-readable day names."""
    days = [_DAY_NAMES[i] for i in range(7) if bitmask & (1 << i)]
    return ", ".join(days) if days else "none"


def _parse_days(text: str) -> int:
    """Parse day names (comma/plus separated) or an integer bitmask."""
    try:
        return int(text)
    except ValueError:
        pass
    bitmask = 0
    for part in text.replace("+", ",").split(","):
        part = part.strip().lower()
        if part in _DAY_LOOKUP:
            bitmask |= 1 << _DAY_LOOKUP[part]
        else:
            raise ValueError(f"Unknown day: {part!r}. "
                             f"Use: {', '.join(_DAY_NAMES)}")
    return bitmask


def cmd_peak_shaving(args):
    """Handle the 'peak-shaving' subcommand."""
    client = load_client(args)
    home_id = get_home_id(args, client)
    device_id, model = get_device_id(args, client, home_id)

    def e2e_log(msg: str):
        if getattr(args, "verbose", False):
            print(f"  [E2E] {msg}", file=sys.stderr)

    # ── --show (default) ───────────────────────────────────────────
    if args.show or not any([
        args.enable, args.disable,
        args.peak_reserve is not None, args.ups_reserve is not None,
        args.schedule, args.redundancy is not None,
        args.all_day, args.no_all_day,
    ]):
        try:
            data = client.get_peak_shaving(home_id, device_id, model, log=e2e_log)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        cfg = data.get("config")
        sched = data.get("schedule")

        print("Peak Shaving Configuration")
        print("\u2500" * 40)
        if cfg:
            status = "ON" if cfg["enabled"] else "OFF"
            print(f"  Status:         {status}")
            print(f"  Peak reserve:   {cfg['peak_reserve_pct']}%")
            print(f"  UPS reserve:    {cfg['ups_reserve_pct']}%")
            print(f"  Redundancy:     {cfg['redundancy']}")
        else:
            print("  Config: not available")

        print()
        if sched:
            print("Schedule")
            print("\u2500" * 40)
            print(f"  ID:             {sched['schedule_id']}")
            if sched.get('all_day'):
                print(f"  Time:           All day")
            else:
                print(f"  Time:           {sched['start_time']} \u2013 {sched['end_time']}")
            print(f"  Peak power:     {sched['min_peak_power_w']} W")
            print(f"  Repeat days:    {_format_days(sched['repeat_days'])}")
            if sched["created_ts"]:
                from datetime import datetime
                created = datetime.fromtimestamp(sched["created_ts"])
                print(f"  Created:        {created.strftime('%Y-%m-%d %H:%M:%S')}")
        else:
            print("  Schedule: not available")
        return

    # ── --enable / --disable ───────────────────────────────────────
    if args.enable or args.disable:
        enabled = bool(args.enable)
        try:
            ok = client.toggle_peak_shaving(
                home_id, device_id, model, enabled, log=e2e_log)
            label = "enabled" if enabled else "disabled"
            if ok:
                print(f"Peak shaving {label}.")
            else:
                print("No response when toggling peak shaving.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── --peak-reserve / --ups-reserve ────────────────────────────
    if args.peak_reserve is not None or args.ups_reserve is not None:
        peak_r = args.peak_reserve
        ups_r = args.ups_reserve
        if peak_r is None or ups_r is None:
            try:
                data = client.get_peak_shaving(
                    home_id, device_id, model, log=e2e_log)
                cfg = data.get("config") or {}
                if peak_r is None:
                    peak_r = cfg.get("peak_reserve_pct", 45)
                if ups_r is None:
                    ups_r = cfg.get("ups_reserve_pct", 10)
            except Exception:
                if peak_r is None:
                    peak_r = 45
                if ups_r is None:
                    ups_r = 10
        try:
            ok = client.set_peak_shaving_points(
                home_id, device_id, model, peak_r, ups_r, log=e2e_log)
            if ok:
                print(f"Reserves set: peak={peak_r}%, UPS={ups_r}%")
            else:
                print("No response.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── --redundancy ──────────────────────────────────────────────
    if args.redundancy is not None:
        try:
            ok = client.set_peak_shaving_redundancy(
                home_id, device_id, model, args.redundancy, log=e2e_log)
            if ok:
                print(f"Redundancy set to {args.redundancy}.")
            else:
                print("No response.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── --all-day / --no-all-day ───────────────────────────────────
    if args.all_day or args.no_all_day:
        set_all_day = bool(args.all_day)
        try:
            data = client.get_peak_shaving(
                home_id, device_id, model, log=e2e_log)
            sched = data.get("schedule") or {}
            schedule_id = sched.get("schedule_id", 26)
            start_sec = sched.get("start_seconds", 0)
            end_sec = sched.get("end_seconds", 86340)
            repeat_days = sched.get("repeat_days", 127)
            power_w = sched.get("min_peak_power_w", 15000)
            trailing = sched.get("_trailing", b"")
            ok = client.set_peak_shaving_schedule(
                home_id, device_id, model,
                schedule_id, start_sec, end_sec,
                repeat_days, power_w,
                all_day=set_all_day,
                trailing=trailing,
                log=e2e_log,
            )
            label = "All day ON" if set_all_day else "All day OFF"
            if ok:
                print(f"Schedule updated: {label}")
            else:
                print("No response.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # ── --schedule START-END POWER [DAYS] [ID] ────────────────────
    if args.schedule:
        parts = args.schedule
        if len(parts) < 2:
            print("Usage: --schedule HH:MM-HH:MM POWER_W [DAYS] [SCHEDULE_ID]",
                  file=sys.stderr)
            sys.exit(1)

        time_range = parts[0]
        power_w = int(parts[1])

        if "-" not in time_range:
            print("Time range must be HH:MM-HH:MM", file=sys.stderr)
            sys.exit(1)

        start_str, end_str = time_range.split("-", 1)
        sh, sm = int(start_str.split(":")[0]), int(start_str.split(":")[1])
        eh, em = int(end_str.split(":")[0]), int(end_str.split(":")[1])
        start_sec = sh * 3600 + sm * 60
        end_sec = eh * 3600 + em * 60

        repeat_days = _parse_days(parts[2]) if len(parts) > 2 else 6
        schedule_id = int(parts[3]) if len(parts) > 3 else 26

        try:
            ok = client.set_peak_shaving_schedule(
                home_id, device_id, model,
                schedule_id, start_sec, end_sec,
                repeat_days, power_w,
                all_day=False,
                log=e2e_log,
            )
            if ok:
                print(f"Schedule set: {start_str}-{end_str}, {power_w}W, "
                      f"days={_format_days(repeat_days)}, id={schedule_id}")
            else:
                print("No response.", file=sys.stderr)
        except (EmaldoE2EError, EmaldoAPIError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Emaldo CLI - interact with Emaldo battery systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s login --email user@example.com --password MyPass123
  %(prog)s homes
  %(prog)s devices
  %(prog)s battery
  %(prog)s usage
  %(prog)s schedule
  %(prog)s override --show
  %(prog)s override --range 01:00-05:00=charge
""",
    )

    parser.add_argument("--home-id", help="Home ID (auto-detected if omitted)")
    parser.add_argument("--device-id", help="Device ID (auto-detected if omitted)")
    parser.add_argument("--model", help="Device model (auto-detected if omitted)")
    from emaldo.const import get_default_app_version
    parser.add_argument(
        "--app-version",
        default=get_default_app_version(),
        help=f"App version to report (default: {get_default_app_version()})",
    )

    sub = parser.add_subparsers(dest="command", help="Command")

    p_login = sub.add_parser("login", help="Login to Emaldo")
    p_login.add_argument("--email", help="Email address")
    p_login.add_argument("--phone", help="Phone number (alternative to email)")
    p_login.add_argument("--password", required=True, help="Password")

    p_homes = sub.add_parser("homes", help="List homes")
    p_homes.add_argument("--select", type=int, metavar="N", help="Select home by number")
    sub.add_parser("devices", help="List battery devices")
    sub.add_parser("search", help="Search device details")
    sub.add_parser("battery", help="Battery overview (SoC, capacity, sensor)")

    p_bdet = sub.add_parser("battery-detail", help="Battery cell detail via E2E (per-cell SoC, temp, current)")
    p_bdet.add_argument("--json", dest="json_output", action="store_true", help="Output raw JSON")

    p_usage = sub.add_parser("usage", help="Daily usage stats summary")
    p_usage.add_argument("--offset", type=int, default=0, help="Day offset (0=today)")
    p_usage.add_argument("--graph", action="store_true", help="Show detailed 5-min graph")
    p_usage.add_argument("--no-graph", action="store_true", help="Hide SoC graph")

    p_rev = sub.add_parser("revenue", help="Revenue stats")
    p_rev.add_argument("--offset", type=int, default=0, help="Day offset")

    sub.add_parser("fcr", help="FCR predicted revenue")
    sub.add_parser("schedule", help="Charge/discharge schedule")
    sub.add_parser("power", help="Realtime power (grid, battery, load)")

    p_pe2e = sub.add_parser("power-e2e", help="Realtime power flow via E2E protocol (most accurate)")
    p_pe2e.add_argument("-v", "--verbose", action="store_true", help="Show E2E session details")
    p_pe2e.add_argument("--json", dest="json_output", action="store_true", help="Output raw JSON")

    sub.add_parser("power-debug", help="Dump raw /bmt/list-bmt/ device data (same API the app uses)")

    p_solar = sub.add_parser("solar", help="Solar/MPPT generation stats")
    p_solar.add_argument("--offset", type=int, default=0, help="Day offset")

    p_grid = sub.add_parser("grid", help="Grid import/export stats")
    p_grid.add_argument("--offset", type=int, default=0, help="Day offset")

    sub.add_parser("strategy", help="AI mode strategy (FCR + schedule + revenue)")

    # sell and emergency-charge use the same E2E protocol (type 0x01)
    for cmd_name, cmd_help in [("sell", "Sell (discharge-to-grid) via E2E"),
                                ("emergency-charge", "Emergency charge via E2E")]:
        p_mc = sub.add_parser(cmd_name, help=cmd_help)
        p_mc.add_argument("--hours", type=float, help="Duration in hours (decimals OK)")
        p_mc.add_argument("--until", help="Active until HH:MM or YYYY-MM-DD HH:MM")
        p_mc.add_argument("--cancel", action="store_true", help="Cancel active command")
        p_mc.add_argument("--dry-run", action="store_true", help="Preview without sending")
        p_mc.add_argument("--verbose", action="store_true", help="Show E2E protocol details")

    p_override = sub.add_parser("override", help="Override charge/discharge via E2E")
    p_override.add_argument("slots", nargs="*",
                            help="Overrides: HH:MM=action (charge/idle/clear)")
    p_override.add_argument("--show", action="store_true", help="Show current schedule")
    p_override.add_argument("--reset", action="store_true", help="Clear all overrides")
    p_override.add_argument("--range", nargs="+", metavar="HH:MM-HH:MM=ACTION",
                            help="Override a time range")
    p_override.add_argument("--markers", nargs=2, type=int, metavar=("LOW", "HIGH"),
                            help="Set battery markers (e.g. --markers 20 72)")
    p_override.add_argument("--dry-run", action="store_true", help="Build without sending")

    p_ps = sub.add_parser("peak-shaving", help="Peak shaving config & schedule via E2E")
    p_ps.add_argument("--show", action="store_true", help="Show current peak shaving state (default)")
    p_ps.add_argument("--enable", action="store_true", help="Enable peak shaving")
    p_ps.add_argument("--disable", action="store_true", help="Disable peak shaving")
    p_ps.add_argument("--peak-reserve", type=int, metavar="PCT", help="Peak reserve percentage")
    p_ps.add_argument("--ups-reserve", type=int, metavar="PCT", help="UPS reserve percentage")
    p_ps.add_argument("--schedule", nargs="+", metavar="ARG",
                      help="Set schedule: HH:MM-HH:MM POWER_W [DAYS] [ID]. "
                           "DAYS: Mon,Wed,Fri or bitmask int")
    p_ps.add_argument("--all-day", action="store_true", default=None,
                      help="Enable all-day mode for the schedule")
    p_ps.add_argument("--no-all-day", action="store_true", default=None,
                      help="Disable all-day mode (use schedule times)")
    p_ps.add_argument("--redundancy", type=int, metavar="N", help="Set redundancy value")
    p_ps.add_argument("--verbose", action="store_true", help="Show E2E protocol details")

    sub.add_parser("region", help="Device region info")
    sub.add_parser("contract", help="Balance contract info")
    sub.add_parser("features", help="Device features")
    sub.add_parser("version-check", help="Check app version against server")

    p_raw = sub.add_parser("raw", help="Raw API request")
    p_raw.add_argument("--path", required=True, help="API path")
    p_raw.add_argument("--json", help="JSON body")
    p_raw.add_argument("--no-token", action="store_true", help="Skip token")

    p_enc = sub.add_parser("encrypt", help="Encrypt string (test)")
    p_enc.add_argument("text")
    p_dec = sub.add_parser("decrypt", help="Decrypt hex (test)")
    p_dec.add_argument("hex")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "login": cmd_login, "homes": cmd_homes, "devices": cmd_devices,
        "search": cmd_search, "battery": cmd_battery,
        "battery-detail": cmd_battery_detail, "usage": cmd_usage,
        "revenue": cmd_revenue, "fcr": cmd_fcr, "schedule": cmd_schedule,
        "power": cmd_power, "solar": cmd_solar, "grid": cmd_grid,
        "strategy": cmd_strategy, "sell": cmd_sell,
        "emergency-charge": cmd_emergency_charge, "override": cmd_override,
        "peak-shaving": cmd_peak_shaving,
        "region": cmd_region, "contract": cmd_contract, "features": cmd_features,
        "raw": cmd_raw, "encrypt": cmd_encrypt, "decrypt": cmd_decrypt,
        "version-check": cmd_version_check, "power-debug": cmd_power_debug,
        "power-e2e": cmd_power_e2e,
    }

    cmd_func = commands.get(args.command)
    if not cmd_func:
        parser.print_help()
        sys.exit(1)

    try:
        cmd_func(args)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)
    except EmaldoAuthError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("Run 'login' first.", file=sys.stderr)
        sys.exit(1)
    except EmaldoConnectionError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except EmaldoE2EError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except EmaldoAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
