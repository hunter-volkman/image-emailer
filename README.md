# Module image-emailer

This module enables a Raspberry Pi to autonomously capture images from a remote camera, process them (cropping to focus on a shelf), and email a daily report. It now supports an optional animated GIF generation from the day’s images and requires a location identifier for improved report context. The module connects to a store’s Viam machine and resumes capturing after power cycles by using a persisted state file. It operates without needing the Viam app’s CONTROL tab open.

## Model `hunter:sensor:image-emailer`

A custom sensor component that autonomously captures images from a remote camera, processes them (cropping and optionally creating an animated GIF), and sends a daily email report. Running locally on a Raspberry Pi, it connects to a store’s Viam machine and functions independently once configured.

### Configuration

Configure the model using the following JSON template in your Viam robot configuration:

```json
{
  "email": "<string>",
  "password": "<string>",
  "camera": "<string>",
  "timeframe": [<int>, <int>],
  "recipients": ["<string>", "<string>"],
  "send_time": <int>,
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
| `email` | string  | Required  | GMail address for sending emails. |
| `password` | string | Required  | GMail App Password for authentication (generate via Google Account settings). |
| `camera` | string | Required  | Name of the remote camera (e.g., "remote:camera"). |
| `timeframe` | list of int | Optional  | Start and end hours in EST for image captures. Defaults to `[6, 20]` (6 AM to 8 PM). |
| `recipients` | int | Optional  |Email addresses to receive the daily report. |
| `send_time` | int | Optional  | Hour in EST (0-23) to send the daily email report. Defaults to 20 (8 PM). |
| `save_dir` | string | Optional  | Directory to save images locally. |
| `crop_top` | int | Optional  | Top pixel coordinate for cropping. Defaults to 0 (no cropping from the top). |
| `crop_left` | int | Optional  | Left pixel coordinate for cropping. Defaults to 0 (no cropping from the left). |
| `crop_width` | int | Optional  | Width of the crop region. Defaults to 0 (full width if 0). |
| `crop_height` | int | Optional  | Height of the crop region. Defaults to 0 (full height if 0). |
| `location` | string | Required  | Identifier for the store or monitoring site; used in the email subject for clarity. |
| `make_gif` | boolean | Optional  | Flag to enable creation of a daily animated GIF from the captured images. Defaults to false. |


#### Example Configuration

```json
{
  "email": "user@example.com",
  "password": "your-app-password",
  "camera": "remote:camera",
  "timeframe": [7, 20],
  "recipients": ["recipient1@example.com", "recipient2@example.com"],
  "send_time": 20,
  "save_dir": "/home/hunter.volkman/images",
  "crop_top": 100,
  "crop_left": 100,
  "crop_width": 400,
  "crop_height": 300,
  "location": "Test Location",
  "make_gif": true
}
```

### Setup Instructions

1. **Install Dependencies**: Run `./setup.sh` to create a virtual environment and install requirements (`viam-sdk`, `pillow`, `typing-extensions`).
2. **Configure Remote Part**: On the Raspberry Pi, add the store's Viam machine as a remote part named `"remote"` via the Viam app’s CONFIGURE tab.
3. **Run the Module**: Execute `./run.sh` to start the module.
4. **Test Configuration**:
* Ensure the `camera` name matches the remote part’s camera (e.g., `"remote:camera"`).
* Adjust `crop_*` parameters to focus on the shelf.
* Set `send_time` to a near hour (e.g., 10 for 10:00 AM EST) for testing emails during development.


### Notes
* **Capture Logic**: The module captures images hourly at the start of each hour (e.g., 7:00, 8:00 AM EST) within the defined`timeframe`. On power-up, it reads the last capture timestamp from the persistent state file (saved as `state.json` in the `save_di`r) to resume at the correct time.
* **Image Storage**: Captured images are saved in daily subdirectories (e.g., `/home/hunter.volkman/images/20250225`) and are preserved until manually managed.
* **Email Report**: At the hour specified by `send_time` (default 8 PM EST), the module sends a daily report that:
    * Attaches each captured image from that day.
    * Optionally creates and attaches an inline animated GIF (if `make_gif` is enabled).
    * Uses the `location` attribute in the email subject (e.g., "Daily Report - Location - 2025-03-05").
* **Power Cycles**: The module persists its state (last capture time and last sent date) in a `state.json` file, ensuring that captures resume correctly after a restart.
* **Asynchronous Scheduling and Locking**: A scheduled loop wakes at the start of each hour and uses an inter-process lock (via a lock file) to prevent duplicate runs.

### Example Logs

On restart:
```text
Reconfigured sensor-1 with base_dir: /home/hunter.volkman/images, last_capture_time: 2025-03-05 07:15:30, make_gif: True, location: Test Location
```

During capture:
```text
get_readings called for sensor-1 at EST 10:00:05
Saved image: /home/hunter.volkman/images/20250305/image_20250305_100005_EST.jpg
```

### DoCommand

The module supports two commands via the do_command interface for manual operations:

**Send Email Command**
Manually trigger the email report for a specific day.

Payload example:
```json
{
  "command": "send_email",
  "day": "20250305"
}
```

This command sends the report for the specified day (format: YYYYMMDD) if images exist for that date.

**Create GIF Command**

Manually create an animated GIF from the captured images of a specific day.

Payload exmaple:
```json
{
  "command": "create_gif",
  "day": "20250305"
}
```

This command creates a GIF from the day’s images and returns its storage path.
