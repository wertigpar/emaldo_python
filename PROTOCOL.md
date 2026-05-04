
## 1. Transport

- **UDP**, raw MSCT binary frames (NOT KCP)
- **Relay**: `e2e2.emaldo.com:1050` (IP `35.187.68.18`)
- No TCP fallback; all E2E traffic goes through the cloud relay to the device

---

## 2. Encryption

### 2.1 REST API (HTTPS)

- **RC4** symmetric cipher (key = `app_secret` from credentials)
- Responses are **RC4 decrypted → Snappy decompressed**
- Request fields sent as `encrypt_field(json_string)` → RC4 → hex string
- See `emaldo/crypto.py`

### 2.2 E2E UDP

- **AES-256-CBC** with **PKCS#7** padding
- IV = session nonce (16-byte ASCII alphanumeric string sent in-band)
- Two distinct keys — determined by packet type:

| Key | Field in credentials | Used for |
|-----|---------------------|----------|
| `end_secret` | `home_end_secret` / `sender_end_secret` | Alive packets (relay auth) |
| `chat_secret` | `chat_secret` | Heartbeat, wake, all device commands |

---

## 3. Credentials

All obtained via `EmaldoClient.e2e_login()` (REST API call to `/bmt/search-bmt/`).

| Field | Type | Description |
|-------|------|-------------|
| `host` | `str` | Relay hostname (usually `e2e2.emaldo.com:1050`) |
| `sender_end_id` | `str (32 chars)` | App's endpoint ID |
| `sender_group_id` | `str (32 chars)` | App's group ID |
| `sender_end_secret` | `str (32 chars)` | App's end secret (for alive packets) |
| `recipient_end_id` | `str (32 chars)` | Device endpoint ID |
| `home_end_id` | `str (32 chars)` | Home hub endpoint ID |
| `home_group_id` | `str (32 chars)` | Home hub group ID |
| `home_end_secret` | `str (32 chars)` | Home hub end secret |
| `chat_secret` | `str (32 chars)` | AES key for all device communication |

---

## 4. Packet Structure

### 4.1 Byte-Swap Rule (critical)

Command codes are encoded as a 16-bit value where the high byte is the mode and the low byte is the type.
On the wire these are **byte-swapped** to `[type_byte, mode_byte]`.

Example: code `0xA041` → wire bytes `[0x41, 0xA0]` → `msg_type=0x41, mode=0xA0`.

### 4.2 Mode Bytes

| Value | Meaning |
|-------|---------|
| `0xA0` | Subscribe / fire-and-forget (server-held state or write command) |
| `0x10` | Direct request to device (expects a response payload) |

### 4.3 Command Packet (subscription / write)

```
0xD9 0xA0 0xA0              header + END_ID tag
<sender_end_id: 32 bytes>
0xA0 0xA1                   GROUP_ID tag
<sender_group_id: 32 bytes>
0xA0 0xA2                   RECEIVER_ID tag
<recipient_end_id: 32 bytes>
0x90 0xA3                   AES nonce tag
<nonce: 16 bytes ASCII>
0x81 0xF1 0x01              PROXY (1 byte value=1)
0xA0 0xB5                   APP_ID tag
<app_id: 32 bytes>
0x82 0xF5 <type> <mode>     METHOD (2 bytes: type byte, mode byte)
0x9B 0xF6                   MSGID tag
<msg_id: 27 bytes ASCII>
0x10 0xB7                   Content-Type tag (LAST)
b"application/byte"
<AES-256-CBC encrypted payload>
```

**msg_id format**: `"and_" + 10 random alphanumeric chars + 13-digit millisecond timestamp` (27 chars total)

### 4.4 Alive Packet

```
0xD9 0xA0 0xA0
<sender_end_id: 32 bytes>
0xA0 0xA1
<sender_group_id: 32 bytes>
0x90 0xA3
<nonce: 16 bytes>
0x85 0xF5                   METHOD = string "alive" (5 bytes)
b"alive"
0x9B 0xF6
<msg_id: 27 bytes>
0x10 0xB7
b"application/json"
<AES-CBC( end_secret, nonce, '{"__time":<unix_ts>}' )>
```

Note: Alive packets do **not** include RECEIVER_ID or APP_ID.

### 4.5 Heartbeat Packet

Same structure as command packet but `METHOD` field is `0x89 0xF5 b"heartbeat"` (9-byte string).
Uses `chat_secret`. Payload is `{"__time": <unix_ts>}` JSON.

### 4.6 Wake Packet

Similar to heartbeat but `METHOD` is `0x84 0xF5 b"wake"` (4-byte string).
No MSGID+CT suffix — the MSGID tag byte is `0x1B 0xF6` and is the last field.
Uses `chat_secret`.

### 4.7 Override Packet (special)

Type `0x1A`. Has a slightly different structure: uses `0x84 0xF1 0x00 0x00 0x00 0x01` (4-byte PROXY) and no APP_ID. Payload format:

```
byte 0:   high_marker     (battery % charge cutoff, default 72)
byte 1:   low_marker      (battery % discharge cutoff, default 20)
byte 2:   version_flag    (0x00)
byte 3:   slot_count      (0x60=96 slots today, 0xC0=192 slots today+tomorrow)
bytes 4+: slot_values     (96 or 192 bytes)
```

---

## 5. Session Flow

Every interaction requires a full handshake before sending commands:

```
1. Alive(home)   — sender=home_end_id, key=home_end_secret   → relay auth for hub
2. Alive(device) — sender=sender_end_id, key=sender_end_secret → relay auth for app
3. Wake          — key=chat_secret, wakes relay routing table
4. Heartbeat     — key=chat_secret
5. sleep(0.2s)
6. Command(s)    — key=chat_secret
```

Each session uses a fresh **session nonce** (16-char random alphanumeric).
All packets in a session share the same nonce (except alive packets which generate their own).

---

## 6. Known Commands

| Type | Mode | Direction | Payload | Response |
|------|------|-----------|---------|----------|
| `0x01` | `0xA0` | Write | `[on u8, start u32le, end u32le]` 9B (zeros=cancel) | ACK 161B |
| `0x06` | `0x10` | Read | `[cabinet_idx u8]` 1B | Battery info ≥80B |
| `0x1A` | `0xA0` | Write | Override payload (see §4.7) | ACK 161B |
| `0x1B` | `0xA0` | Subscribe | (empty) | Override state (see §7.2) |
| `0x20` | `0xA0` | Subscribe | (empty) | EV charging mode 6B |
| `0x22` | `0xA0` | Write | EV smart mode 9B | ACK |
| `0x29` | `0xA0` | Write | `[instant_on u8]` 1B | ACK |
| `0x30` | `0xA0` | Subscribe | `[0x01]` 1B | Power flow 20–22B |
| `0x31` | `0xA0` | Write | EV instant mode 4B | ACK |
| `0x41` | `0xA0` | Write | `[on u8]` 1B (1=on,0=off) | ACK (fire-and-forget) |
| `0x45` | `0xA0` | Subscribe | (empty) | FCR/mFRR state 2–4B |
| `0x57` | `0xA0` | Write | `[enabled u8]` 1B | ACK |
| `0x58` | `0xA0` | Write | `[peak_pct u8, ups_pct u8]` 2B | ACK |
| `0x5A` | `0xA0` | Write | Peak schedule 15B+ | ACK |
| `0x5B` | `0xA0` | Subscribe | (empty) | Peak shaving config 20B |
| `0x5C` | `0xA0` | Subscribe | (empty) | Peak schedule 28B |
| `0x77` | `0xA0` | Write | `[redundancy u8]` 1B | ACK |
| `0x80` | `0xA0` | Write | `[on u8, target u32le, expand u8]` 6B | ACK |
| `0x81` | `0xA0` | Subscribe | `b""` | Manual selling state 10B |

---

## 7. Response Payload Formats

All payloads are little-endian unless noted.

### 7.1 Power Flow (`0x30`, 20–22 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0–1 | `s16` | `battery_w` | ×100 W; positive=charging, negative=discharging |
| 2–3 | `s16` | `solar_w` | ×100 W |
| 4–5 | `s16` | `grid_w` | ×100 W; positive=import, negative=export |
| 6–7 | `s16` | `addition_load_w` | ×100 W |
| 8–9 | `s16` | `other_load_w` | ×100 W |
| 10–11 | `s16` | `ev_w` | ×100 W |
| 12–13 | `u16` | `ip2_w` | ×100 W (unsigned) |
| 14–15 | `u16` | `op2_w` | ×100 W (unsigned) |
| 16 | `u8` | `grid_valid` | 1 = CT sensor present |
| 17 | `u8` | `bsensor_valid` | 1 = battery sensor present |
| 18 | `u8` | `solar_efficiency` | enum |
| 19 | `u8` | `thirdparty_pv_on` | 1 = third-party PV enabled |
| 20–21 | `s16` | `dual_power_w` | ×100 W |

### 7.2 Override State (`0x1B`, ≥105 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0 | `u8` | `high_marker` | Charge cutoff % (default 72) |
| 1 | `u8` | `low_marker` | Discharge cutoff % (default 20) |
| 2 | `u8` | `version` | Dirty/version flag |
| 3 | `u8` | — | (subscription tag) |
| 4–7 | — | — | Extended header |
| 8 | `u8` | `slot_count` | `0x60`=96 (today only), `0xC0`=192 (today+tomorrow) |
| 9+ | `u8[]` | `slots` | Per-slot override values (see §8) |

### 7.3 Battery Info (`0x06`, ≥80 bytes, request mode `0x10`)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0–1 | `u16` | `state_flags` | bit0=discharging, bit1=charging, bits2+= faults |
| 2–3 | `u16` | `bms_temp` | deciKelvin (÷10 − 273.15 = °C) |
| 4–5 | `u16` | `electrode_a_temp` | deciKelvin |
| 6–7 | `u16` | `electrode_b_temp` | deciKelvin |
| 8–9 | `u16` | `voltage_mv` | millivolts |
| 10–13 | `s32` | `current_ma` | milliamps; negative = discharging |
| 14–15 | `u16` | `soc` | State of charge % |
| 16–17 | `u16` | `current_energy_wh` | Current stored energy Wh |
| 18–19 | `u16` | `full_energy_wh` | Rated capacity Wh |
| 20–21 | `u16` | `cycle_count` | Charge cycle count |
| 22–23 | `u16` | `soh` | State of health % |
| 24+ | variable | `id_info`, `version`, `barcode` | Length-prefixed strings |
| + | `u8` | `index` | Cabinet index |
| + | `u8` | `cabinet_index` | Cabinet index (redundant) |
| + | `u8` | `cabinet_position` | Position in cabinet |
| + | `u16` | `capacity` | Capacity Wh |

### 7.4 FCR/mFRR State (`0x45`, 2–4 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0–1 | `u16` | `state` | 0=Idle,1=OnHold,2=FcrN,3=FcrDUp,4=FcrDDown,5=FcrDUpDown,6=MFRRUp,7=MFRRDown |
| 2–3 | `u16` | `error_flag` | present in 4-byte variant; `≠1` means error |

### 7.5 Peak Shaving Config (`0x5B`, 20 bytes)

| Offset | Type | Field |
|--------|------|-------|
| 0 | `u8` | `enabled` |
| 5 | `u8` | `peak_reserve_pct` |
| 6 | `u8` | `ups_reserve_pct` |
| 18 | `u8` | `redundancy` |

### 7.6 Peak Schedule (`0x5C`, ≥16 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0–1 | `u16` | `schedule_id` | |
| 2 | `u8` | `all_day` | 1 = ignore start/end times |
| 3–6 | `u32` | `start_seconds` | Seconds from midnight |
| 7–10 | `u32` | `end_seconds` | Seconds from midnight |
| 11 | `u8` | `repeat_days` | Day-of-week bitmask |
| 12–13 | `u16` | `min_peak_power_w` | Watts |
| 16–19 | `u32` | `created_ts` | Unix timestamp (if ≥20B) |

### 7.7 Manual Selling State (`0x81`, 10 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0 | `u8` | `first_use` | 1 = never used before |
| 1 | `u8` | `enabled` | 1 = currently selling |
| 2–5 | `u32` | `target_deci_kwh` | Target in 0.1 kWh units |
| 6–9 | `u32` | `sold_deci_kwh` | Sold so far in 0.1 kWh units |

### 7.8 EV Charging Mode (`0x20`, 6 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| 0 | `u8` | `mode_minus1` | mode enum − 1; add 1 to get: 1=LowestPrice, 2=SolarOnly, 3=Scheduled, 4=InstantFull, 5=InstantFixed |
| 1–2 | `u16` | `fixed_kwh` | kWh slider value |
| 3–4 | `u16` | `fixed_full_kwh` | kWh slider max |
| 5 | `u8` | `price_percent` | (semantics unknown) |

---

## 8. Override Slot Values

96 slots per day (15 min per slot). Slots index 0=00:00, 95=23:45.
192 slots = today (0–95) + tomorrow (96–191).

| Value | Meaning |
|-------|---------|
| `0x80` (128) | No override — follow smart schedule |
| `0x00` (0) | Idle — neither charge nor discharge |
| 1–100 | Charge when SoC < N% |
| 129–255 | Discharge when SoC > (256 − N)% |

Constants in `const.py`:
- `SLOT_NO_OVERRIDE = 0x80`
- `SLOT_IDLE = 0x00`
- `SLOT_CHARGE_DEFAULT = 0x48` (charge when SoC < 72%)
- `DEFAULT_MARKER_HIGH = 72`, `DEFAULT_MARKER_LOW = 20`

---

## 9. EV Charging Mode Commands

### `0x22` SET_EV_CHARGING_MODE (Smart modes, 9 bytes)

```
byte 0:   mode − 1        (0=LowestPrice, 1=SolarOnly, 2=Scheduled)
byte 1:   no_schedule     (1 if no hour bitmaps, 0 if bitmaps supplied)
bytes 2–4: weekday bitmap  (24-bit, LSB=hour 0, packed 8h/byte)
bytes 5–7: weekend bitmap  (same format)
byte 8:   sync            (1 = sync to other home devices)
```

### `0x31` SET_EVCHARGINGMODE_INSTANT (Instant modes, 4 bytes)

```
byte 0:   mode − 1        (3=InstantFull, 4=InstantFixed)
byte 1:   consume_flag    (0 if mode==InstantFixed, else 1)
bytes 2–3: fixed_kwh      (LE u16; 0 for InstantFull)
```

### `0x29` SET_EVCHARGINGMODE_INSTANTCHARGE (toggle, 1 byte)

```
byte 0:   instant_on      (1=enable Instant, 0=return to Smart)
```

---

## 10. Emergency Charge / Manual Selling Commands

### `0x01` set_emergency_charge (9 bytes)

```
byte 0:   on              (1=enable, 0=disable)
bytes 1–4: start_unix     (LE u32; unix timestamp)
bytes 5–8: end_unix       (LE u32; unix timestamp)
9 zero bytes = cancel
```
Default window when enabling: now → top-of-current-hour + 48h.

### `0x80` set_manual_selling (6 bytes)

```
byte 0:   on              (1=enable, 0=disable)
bytes 1–4: target_kwh     (LE u32; integer kWh)
byte 5:   expand          (1=expand selling, 0=no)
```

---

## 11. Response Sizes / Error Patterns

| Size | Meaning |
|------|---------|
| 212B | `CONN_NOT_ESTABLISHED` — stale credentials or device offline |
| 166B | Relay routing echo — command forwarded but device not yet connected |
| 161B | Normal ACK for write commands |
| 146B | `MEMBER_EXSPIRED` — credentials expired |

The relay always responds (even to commands it forwarded); the actual device response (if any) arrives as a separate UDP packet.

---

## 12. REST API

Base URL: `https://api.emaldo.com` (some data endpoints on `https://dp.emaldo.com`)

### Auth

```
POST /user/login/
Body (form-encoded): json=<encrypt_field({"username":…,"password":…})>, gm=1
Response: {"Status":1, "Result":{"token":…, "user_id":…}}
```

Token is stored locally and passed in subsequent requests as `token=<encrypt_field(token + timestamp)>`.
Auth status `-12` → session expired → re-login required.

### Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `/home/list-homes/` | List homes |
| `/bmt/list-bmt/` | List devices in home |
| `/bmt/search-bmt/` | Get E2E credentials for device |
| `/bmt/stats/b-sensor/` | Battery sensor overview |
| `/bmt/stats/battery-v2/day/` | Daily battery stats |
| `/bmt/stats/battery/power-level/day/` | Daily SoC levels |
| `/bmt/stats/load/usage-v2/day/` | Daily load usage |
| `/bmt/stats/mppt-v2/day/` | Daily solar (MPPT) data |
| `/bmt/stats/grid/day/` | Daily grid import/export |
| `/bmt/stats/get-charging-discharging-plans-v2-minute/` | Current charge schedule |
| `/bmt/stats/revenue-v2/day/` | Daily revenue |
| `/bmt/is-dual-power-open/` | Dual-power / third-party PV status |
| `/bmt/get-manual-selling-history/` | Manual selling history |
| `/home/get-home-fcr-predict-revenue-summary/` | FCR revenue summary |
| `/home/get-home-fcr-predict-revenue-daily/` | FCR revenue by day |
| `/domain/getappversionstate/` | App version info |

All JSON bodies sent as `json=<encrypt_field(json_string)>` + `token=<encrypt_field(token+ts)>` + `gm=1`.
Responses contain `{"Status": int, "Result": …}`. `Result` is RC4+Snappy encrypted when it is a string.

---

## 13. App Credentials (APK)

Hardcoded in APK, extracted via `emaldo/extract_keys.py`:

| Field | Description |
|-------|-------------|
| `app_id` | 32-char app identifier used in E2E packets and REST requests |
| `app_secret` | 32-char RC4 key for REST API encryption |

---

## 14. Implementation Notes

- **Credential freshness**: Always call `e2e_login()` fresh; never re-use cached E2E credentials from a previous session. The relay validates credentials and returns 212B if they are stale.
- **Command timing**: Wait ≥200ms after heartbeat before sending commands, or the relay may reject them.
- **Fire-and-forget**: Commands like `0x41` (third-party PV) have `setIsNeedResult=false` in the APK — they send no application-level response payload. Only a relay ACK (~161B) is returned.
- **State lag**: After a write command, wait ≥1–2s before reading back state via a subscribe command — the device takes time to apply changes.
- **Multiple responses**: Subscribe commands (`0xA0` mode) may return multiple UDP packets. The first is often a relay echo/ACK; the actual data arrives in a subsequent packet.
- **Battery probing**: Send one `0x06` request per cabinet index (0, 1, 2, …); stop after two consecutive short (<250B) or missing responses.

---

# See also

https://medium.com/@ylenius/how-i-reverse-engineered-my-home-batterys-protocol-in-one-day-with-an-ai-pair-programmer-60de36e75df9
