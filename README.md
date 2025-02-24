# Module image-emailer

This module enables a Raspberry Pi to connect to a remote Viam machine, capture hourly images from a camera during specified operating hours, crop them to focus on a shelf, and email them as a daily report. Designed for monitoring stock levels, it prioritizes simplicity and security for demo purposes.

## Model hunter:sensor:image-emailer

A sensor component that captures images from a remote camera, processes them, and sends them via email on a schedule. It runs locally on a Raspberry Pi, connects to a store's Viam machine, and operates from 7 AM to 7 PM EST by default.

### Configuration

Configure the model using the following JSON template in your Viam robot configuration:

```json
{
  "email": "<string>",
  "password": "<string>",
  "camera": "<string>",
  "frequency": <int>,
  "timeframe": [<int>, <int>],
  "recipients": ["<string>", "<string>"],
  "save_dir": "<string>",
  "crop_top": <int>,
  "crop_left": <int>,
  "crop_width": <int>,
  "crop_height": <int>
}

### Configuration
The following attribute template can be used to configure this model:

```json
{
  "email": "<string>",
  "password": "<string>",
  "camera": "<string>",
  "frequency": <int>,
  "timeframe": [<int>, <int>],
  "recipients": ["<string>", "<string>"],
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
| `email` | string  | Required  | GMail address for sending emails1 |
| `password` | string | Required  | GMail app password for authentication |
| `camera` | string | Required  | Name of the remote camera (e.g., "example.camera") |
| `frequency` | int | Optional  | Capture frequency in seconds (default: 1 hour) |
| `timeframe` | list of int | Optional  | Start and end hours in EST (e.g., [7, 19]) |
| `recipients` | list of string | Optional  | Email addresses to receive the report |
| `save_dir` | string | Optional  | Directory to save images locally |
| `crop_top` | int | Optional  | Top pixel coordinate for cropping |
| `crop_left` | int | Optional  | Left pixel coordinate for cropping |
| `crop_width` | int | Optional  | Width of the crop region |
| `crop_height` | int | Optional  | Height of the crop region |


#### Example Configuration

```json
{
  "email": "user@example.com",
  "password": "your-app-password",
  "camera": "store.camera",
  "frequency": 3600,
  "timeframe": [7, 19],
  "recipients": ["recipient1@example.com", "recipient2@example.com"],
  "save_dir": "/home/pi/images",
  "crop_top": 100,
  "crop_left": 100,
  "crop_width": 400,
  "crop_height": 300
}
```

### Setup Instructions

1. ***Install Dependencies***: Run ./setup.sh to create a virtual environment and install requirements (viam-sdk, pillow, typing-extensions).
2. ***Configure Remote Part***: On the Raspberry Pi, add the store's Viam machine as a remote part named "store" via the Viam app’s CONFIGURE tab.
3. ***Run the Module***: Execute ./run.sh to start the module.
4. ***Test Configuration***: Ensure the camera name matches the remote part's camera (e.g., "store.camera") and adjust crop parameters to focus on the shelf.

### Notes
* Images are saved locally and emailed hourly during the timeframe. Adjust `save_dir1 for persistent storage.
* Use a GMail app password for secure email sending (generate one via Google Account settings).

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
