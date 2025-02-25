# Module image-emailer

This module enables a Raspberry Pi to connect to a remote Viam machine, capture images hourly on the hour (e.g., 7:00 AM, 8:00 AM EST) from a camera during specified operating hours, crop them to focus on a shelf, and email them as a daily report. Designed for monitoring stock levels, it prioritizes simplicity and reliability, automatically resuming captures after power cycles using the last captured image’s timestamp. Ideal for demo purposes and development testing.

## Model hunter:sensor:image-emailer

A sensor component that captures images from a remote camera, processes them, and sends them via email on a configurable schedule. It runs locally on a Raspberry Pi, connects to a store's Viam machine, and operates from 7 AM to 7 PM EST by default, sending a report at 7 PM EST.

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
  "crop_height": <int>
}
```

#### Attributes

The following attributes are available for this model:

| Name          | Type   | Inclusion | Description                |
|---------------|--------|-----------|----------------------------|
| `email` | string  | Required  | GMail address for sending emails |
| `password` | string | Required  | GMail App Password for authentication (generate via Google Account settings) |
| `camera` | string | Required  | Name of the remote camera (e.g., "remote.camera") |
| `timeframe` | list of int | Optional  | Start and end hours in EST for captures (e.g., [7, 19] for 7 AM-7 PM) |
| `recipients` | int | Optional  | Email addresses to receive the report |
| `send_time` | int | Optional  | Hour in EST (0-23) to send the daily email (default: 19, i.e., 7 PM) |
| `save_dir` | string | Optional  | Directory to save images locally |
| `crop_top` | int | Optional  | Top pixel coordinate for cropping (default: 0, full height) |
| `crop_left` | int | Optional  | Left pixel coordinate for cropping (default: 0, full width) |
| `crop_width` | int | Optional  | Width of the crop region (default: 0, full width) |
| `crop_height` | int | Optional  | Height of the crop region (default: 0, full height) |


#### Example Configuration

```json
{
  "email": "user@example.com",
  "password": "your-app-password",
  "camera": "remote:camera",
  "timeframe": [7, 19],
  "recipients": ["recipient1@example.com", "recipient2@example.com"],
  "send_time": 19,
  "save_dir": "/home/user/images",
  "crop_top": 100,
  "crop_left": 100,
  "crop_width": 400,
  "crop_height": 300
}
```

### Setup Instructions

1. **Install Dependencies**: Run ./setup.sh to create a virtual environment and install requirements (viam-sdk, pillow, typing-extensions).
2. **Configure Remote Part**: On the Raspberry Pi, add the store's Viam machine as a remote part named "store" via the Viam app’s CONFIGURE tab.
3. **Run the Module**: Execute ./run.sh to start the module.
4. **Test Configuration**:
* Ensure the camera name matches the remote part’s camera (e.g., "remote:camera").
* Adjust crop_* parameters to focus on the shelf.
* Set send_time to a near hour (e.g., 10 for 10:00 AM EST) for testing emails during development.


### Notes
* **Capture Logic**: Captures images hourly at the start of each hour (e.g., 7:00, 8:00 AM EST) within timeframe. On power-up, it uses the latest image’s timestamp from `save_dir/YYYYMMDD` to resume at the next hour.
* **Image Storage**: Images are saved in daily subdirectories (e.g., `/home/hunter.volkman/images/20250225`) and never deleted—adjust `save_dir` for persistent storage.
* **Email Report**: Sends one report per `send_time` hour with the latest image per hour captured that day.
* **Power Cycles**: Automatically resumes capturing at the next hour after restart, based on the last saved image’s timestamp.
* **Development Mode**: Adjust `send_time` in the config and restart the module to test emails at different hours.

### Example Logs

On restart:
```text
Reconfigured sensor-1 with base_dir: /home/hunter.volkman/images, last_capture_time: 2025-02-25 09:19:57
```

During capture:
```text
get_readings called for sensor-1 at EST 10:00:05, hour: 10
Checking timeframe [7.0, 19.0]
Last capture time: 2025-02-25 09:19:57, last_hour: 9, next_hour: 10
Saved image: /home/hunter.volkman/images/20250225/image_20250225_100005_EST.jpg for hour 10
```

### DoCommand

If your model implements DoCommand, provide an example payload of each command that is supported and the arguments that can be used. If your model does not implement DoCommand, remove this section.

#### Example DoCommand

```json
{
  "command_name": {
    "arg1": "foo",
    "arg2": 1
  }
}
```
