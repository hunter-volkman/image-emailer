# Module image-emailer

This module enables a Raspberry Pi to autonomously capture images from a remote camera, process them (cropping and annotating with timestamps), and email a daily report with optional animated GIF generation. It requires a location identifier for context and connects to a store’s Viam machine. The module persists its state to resume after power cycles or restarts, operates independently of the Viam app’s CONTROL tab, and uses twice-daily `viam-agent` restarts (6:00 AM and 6:00 PM EST) to ensure predictable behavior despite updates or connection issues.

## Model `hunter:sensor:image-emailer`

A custom sensor component that captures images at specified times, processes them (cropping, annotating static images, and optionally creating a GIF), and sends a daily email report. It runs locally on a Raspberry Pi, connects to a store’s Viam machine, and uses a scheduled loop with inter-process locking for reliability.

### Configuration

Configure the model using the following JSON template in your Viam robot configuration:

```json
{
  "email": "<string>",
  "password": "<string>",
  "camera": "<string>",
  "capture_times_weekday": ["<string>", "<string>"],
  "capture_times_weekend": ["<string>", "<string>"],
  "recipients": ["<string>", "<string>"],
  "send_time": "<string>",
  "save_dir": "<string>",
  "crop_top": <int>,
  "crop_left": <int>,
  "crop_width": <int>,
  "crop_height": <int>,
  "location": "<string>",
  "make_gif": <boolean>
}
```

#### Attributes


| Name          | Type   | Inclusion | Description                |
|---------------|--------|-----------|----------------------------|
| `email` | string | Required  | Gmail address for sending emails. |
| `password` | string | Required  | Gmail App Password for authentication (generate via Google Account settings). |
| `camera` | string | Required  | Name of the remote camera (e.g., "remote-1:ffmpeg"). |
| `capture_times_weekday` | list of string | Optional  | Capture times in EST ("HH:MM") for weekdays (Mon-Fri). Defaults to `["7:00", "7:15", "8:00", "11:00", "11:30"]`. |
| `capture_times_weekend` | list of string | Optional  | Capture times in EST ("HH:MM") for weekends (Sat-Sun). Defaults to `["8:00", "8:15", "9:00", "11:00", "11:30"]`. |
| `recipients` | int | Optional  | Email addresses to receive the daily report. |
| `send_time` | int | Optional  | Time in EST ("HH:MM") to send the daily report. Defaults to `"20:00"`. |
| `save_dir` | string | Optional  | Directory to save images. Defaults to `"/home/hunter.volkman/images"`. |
| `crop_top` | int | Optional | Top pixel coordinate for cropping. Defaults to 0. |
| `crop_left` | int | Optional | Left pixel coordinate for cropping. Defaults to 0. |
| `crop_width` | int | Optional | Width of the crop region. Defaults to 0 (full width). |
| `crop_height` | int | Optional | Height of the crop region. Defaults to 0 (full height). |
| `location` | string | Required | Location identifier for the email subject and body. |
| `make_gif` | boolean | Optional | Enable daily animated GIF creation. Defaults to `false`. |


#### Example Configuration

```json
{
  "email": "user.name@viam.com",
  "password": "<gmail-app-password",
  "camera": "remote:camera",
  "capture_times_weekday": ["7:00", "7:15", "8:00", "11:00", "11:30"],
  "capture_times_weekend": ["8:00", "8:15", "9:00", "11:00", "11:30"],
  "recipients": ["my-email-list@viam.com"],
  "send_time": "13:30",
  "save_dir": "/home/user.name/images",
  "crop_top": 0,
  "crop_left": 0,
  "crop_width": 0,
  "crop_height": 0,
  "location": "Test Location",
  "make_gif": true
}
```

### Setup Instructions

1. **Install Dependencies**: 
  * Run `./setup.sh` to create a virtual environment and install requirements (`viam-sdk`, `pillow`, `typing-extensions`).
2. **Configure Remote Part**: 
  * On the Raspberry Pi, add the store's Viam machine as a remote part named `"remote-1"` via the Viam app’s CONFIGURE tab.
3. **Run the Module**: 
  * Execute `./run.sh` to start the module.
4. **Setup Daily Restarts**:
  * Create a restart script at `/home/user.name/scripts/restart.sh`:
```bash
#!/bin/bash
LOG_FILE="/home/user.name/scripts/restart.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
mkdir -p "$(dirname "$LOG_FILE")"
echo "[$TIMESTAMP] Starting viam-agent restart" >> "$LOG_FILE"
if sudo systemctl restart viam-agent >> "$LOG_FILE" 2>&1; then
    echo "[$TIMESTAMP] viam-agent restarted successfully" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] ERROR: viam-agent restart failed" >> "$LOG_FILE"
    exit 1
fi
sleep 5
if systemctl is-active viam-agent | grep -q "active"; then
    echo "[$TIMESTAMP] viam-agent confirmed running" >> "$LOG_FILE"
else
    echo "[$TIMESTAMP] ERROR: viam-agent not running after restart" >> "$LOG_FILE"
    exit 1
fi
```
  * Make it executable: `chmod +x /home/user.name/scripts/restart.sh`
  * Add to root’s crontab (`sudo crontab -e`):
```text
0 6 * * * /home/user.name/scripts/restart.sh
0 18 * * * /home/user.name/scripts/restart.sh
```
  * This restarts `viam-agent` daily at 6:00 AM EST and 6:00 PM EST.
5. **Test Configuration**:
* Ensure the `camera` name matches the remote part’s camera name.
* Adjust `capture_times` and `send_time` (e.g., `"20:00"` for 8:00 PM EST).
* Verify email delivery, static image annotations, and GIF generation (if enabled).


### Notes
* **Capture Logic**: Captures occur at times in capture_times (e.g., `"7:00"`, `"8:00"`). The module persists the last capture time in `state.json` (in `save_dir`) to resume after restarts.
* **Image Storage**: Images are saved in daily subdirectories (e.g., `/home/user.name/images/20250305`) and retained until manually deleted.
* **Email Report**: Sent at send_time (e.g., `"20:00"`), including:
    * All images from the day as attachments, each annotated with its capture timestamp (e.g., `"16:00:00 EST"`) in the bottom-right corner on a semi-transparent black background with white text.
    * An optional inline animated GIF (if `make_gif` is `true`), with frames similarly annotated.
    * Subject: `"Daily Report - <location> - YYYY-MM-DD"`.
* **Resilience**:
    * Uses `state.jso`n to track `last_capture_time`, `last_sent_date`, and `last_sent_time`, preventing duplicates or missed actions.
    * Twice-daily `viam-agent` restarts (6:00 AM and 6:00 PM EST) via cron ensure stability against Viam updates, connection drops, or duplicate tasks.
* **Logging**:
    * Module logs: `viam logs` or `journalctl -u viam-agent`.
    * Restart logs: `/home/user.name/scripts/restart.log`.

### Example Logs

On restart:
```text
Reconfigured sensor-1 with base_dir: /home/hunter.volkman/images, last_capture_time: 2025-03-05 19:00:00, capture_times: ["7:00", "8:00", ..., "19:00"], make_gif: True, location: Test Location
```

During capture:
```text
Saved image: /home/hunter.volkman/images/20250305/image_20250305_160000_EST.jpg
```

Restart log:
```text
[2025-03-05 16:45:01] Starting viam-agent restart
[2025-03-05 16:45:01] viam-agent restarted successfully
[2025-03-05 16:45:06] viam-agent confirmed running
```

### DoCommand

Supports manual operations via `do_command`:

* **Send Email**:
```json
{"command": "send_email", "day": "20250305"}
```
  * Sends a report for the specified day with annotated images.

* **Create GIF**:
```json
{"command": "create_gif", "day": "20250305"}
```
  * Creates an annotated GIF from the day’s images and returns its path.
