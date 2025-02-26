import asyncio
import datetime
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Any, ClassVar, Mapping, Optional, Sequence
from typing_extensions import Self
from viam.components.camera import Camera
from viam.components.sensor import Sensor
from viam.module.module import Module
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.easy_resource import EasyResource
from viam.resource.types import Model, ModelFamily
from viam.utils import SensorReading, struct_to_dict
from PIL import Image
from io import BytesIO
import functools

class EmailImages(Sensor, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("hunter", "sensor"), "image-emailer")

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        sensor = cls(config)
        print(f"Created new EmailImages instance for {config.name}")
        sensor.reconfigure(config, dependencies)
        return sensor

    @classmethod
    def validate_config(cls, config: ComponentConfig) -> Sequence[str]:
        attributes = struct_to_dict(config.attributes)
        required = ["email", "password", "camera", "recipients"]
        for attr in required:
            if attr not in attributes:
                raise Exception(f"{attr} is required")
        return [attributes["camera"]]

    def __init__(self, config: ComponentConfig):
        super().__init__(config.name)
        self.email = ""
        self.password = ""
        self.timeframe = [7, 20]  # 7 AM to 8 PM EST
        self.send_time = 19       # 7 PM EST
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        self.last_capture_time = None
        self.sent_today = False
        self.email_status = "not_sent"
        self.loop_task = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        print(f"Initialized EmailImages with name: {self.name}, base_dir: {self.base_dir}")

    def _get_last_capture_time(self, daily_dir):
        if not os.path.exists(daily_dir):
            print(f"No daily directory exists at {daily_dir}, last_capture_time remains None")
            return None
        images = [f for f in os.listdir(daily_dir) if f.startswith("image_") and f.endswith("_EST.jpg")]
        if not images:
            print(f"No valid images found in {daily_dir}, last_capture_time remains None")
            return None
        latest = max(images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        timestamp_str = latest.split('_')[1] + "_" + latest.split('_')[2].split('.')[0]
        try:
            last_time = datetime.datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
            print(f"Found latest image {latest}, setting last_capture_time to {last_time}")
            return last_time
        except ValueError:
            print(f"Invalid timestamp in {latest}, last_capture_time remains None")
            return None

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.timeframe = attributes.get("timeframe", [7, 20])
        self.send_time = attributes.get("send_time", 19)
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/images")
        self.crop_top = attributes.get("crop_top", 0)
        self.crop_left = attributes.get("crop_left", 0)
        self.crop_width = attributes.get("crop_width", 0)
        self.crop_height = attributes.get("crop_height", 0)

        camera_resource_name = ResourceName(
            namespace="rdk", type="component", subtype="camera", name=self.camera_name
        )
        self.camera = dependencies.get(camera_resource_name)
        if not self.camera:
            print(f"Could not resolve camera: {self.camera_name}. Check configuration.")
        else:
            print(f"Successfully resolved camera: {self.camera_name}")

        today = datetime.datetime.now().strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today)
        self.last_capture_time = self._get_last_capture_time(daily_dir)
        self.sent_today = False
        self.email_status = "not_sent"
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)
        print(f"Reconfigured {self.name} with base_dir: {self.base_dir}, last_capture_time: {self.last_capture_time}")

        if self.loop_task:
            self.loop_task.cancel()
        self.loop_task = asyncio.create_task(self.capture_loop())

    def generate_events(self, day: datetime.date):
        events = []
        start_time, end_time = self.timeframe
        for hour in range(start_time, end_time):
            event_time = datetime.datetime.combine(day, datetime.time(hour=hour, minute=0, second=0))
            events.append((event_time, "capture"))
        send_time = datetime.datetime.combine(day, datetime.time(hour=self.send_time, minute=0, second=0))
        events.append((send_time, "send"))
        return sorted(events, key=lambda x: x[0])

    async def capture_loop(self):
        while True:
            try:
                now = datetime.datetime.now()
                today = now.date()
                future_events = self.generate_events(today)
                future_events = [(time, event) for time, event in future_events if time > now]

                if not future_events:
                    tomorrow = today + datetime.timedelta(days=1)
                    future_events = self.generate_events(tomorrow)
                    next_time = future_events[0][0]
                    sleep_seconds = (next_time - now).total_seconds()
                    print(f"All events for {today} done, sleeping until {next_time} ({sleep_seconds} seconds)")
                    await asyncio.sleep(sleep_seconds)
                    continue

                next_time, next_type = future_events[0]
                sleep_seconds = max(0, (next_time - now).total_seconds())
                print(f"Scheduling {next_type} at {next_time}, sleeping for {sleep_seconds} seconds")
                await asyncio.sleep(sleep_seconds)

                now = datetime.datetime.now()
                if next_type == "capture":
                    await self.capture_image(next_time)
                elif next_type == "send" and not self.sent_today:
                    await self.send_report(next_time)
            except Exception as e:
                print(f"Capture loop error: {str(e)}, retrying in 60 seconds")
                await asyncio.sleep(60)  # Retry after a delay

    async def capture_image(self, time: datetime.datetime):
        if not self.camera:
            print(f"No camera available at {time}, skipping capture")
            return
        try:
            print(f"Capturing image at {time}")
            image = await self.camera.get_image()
            img = Image.open(BytesIO(image.data))
            crop_width = self.crop_width or img.width - self.crop_left
            crop_height = self.crop_height or img.height - self.crop_top
            crop_top = max(0, min(self.crop_top, img.height - 1))
            crop_left = max(0, min(self.crop_left, img.width - 1))
            crop_width = min(crop_width, img.width - crop_left)
            crop_height = min(crop_height, img.height - crop_top)
            cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))

            today_str = time.strftime('%Y%m%d')
            daily_dir = os.path.join(self.base_dir, today_str)
            if not os.path.exists(daily_dir):
                os.makedirs(daily_dir)
            filename = f"image_{time.strftime('%Y%m%d_%H%M%S')}_EST.jpg"
            save_path = os.path.join(daily_dir, filename)
            cropped_img.save(save_path, format="JPEG")
            self.last_capture_time = time
            print(f"Saved image: {save_path}")
        except Exception as e:
            print(f"Capture error at {time}: {str(e)}")

    async def send_report(self, time: datetime.datetime):
        today_str = time.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today_str)
        if not os.path.exists(daily_dir):
            print(f"No directory for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today_str}") and f.endswith("_EST.jpg")]
        if not all_images:
            print(f"No images for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        try:
            print(f"Sending report with {len(images_to_send)} images at {time}")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                functools.partial(self._send_daily_report_sync, images_to_send, time, daily_dir)
            )
            self.sent_today = True
            self.email_status = "sent"
            print(f"Sent report with {len(images_to_send)} images to {', '.join(self.recipients)}")
        except Exception as e:
            self.email_status = f"error: {str(e)}"
            print(f"Email send error at {time}: {str(e)}")

    def _send_daily_report_sync(self, image_files, timestamp, daily_dir):
        msg = MIMEMultipart()
        msg["From"] = self.email
        msg["Subject"] = f"Daily Inventory Report - 389 5th Ave, New York, NY - {timestamp.strftime('%Y-%m-%d')}"
        body = f"Attached are {len(image_files)} inventory images from the store at 389 5th Ave, New York, NY captured on {timestamp.strftime('%Y-%m-%d')}, ordered from earliest to latest."
        msg.attach(MIMEText(body, "plain"))
        msg["To"] = ", ".join(self.recipients)

        for image_file in image_files:
            image_path = os.path.join(daily_dir, image_file)
            with open(image_path, "rb") as file:
                attachment = MIMEBase("application", "octet-stream")
                attachment.set_payload(file.read())
                encoders.encode_base64(attachment)
                attachment.add_header(
                    "Content-Disposition", f"attachment; filename={image_file}"
                )
                msg.attach(attachment)

        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(self.email, self.password)
            smtp.send_message(msg)
            print(f"Daily report sent to {msg['To']}")

    async def do_command(self, command: Mapping[str, Any], *, timeout: Optional[float] = None, **kwargs) -> Mapping[str, Any]:
        if command.get("command") == "send_email":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                timestamp = datetime.datetime.strptime(day, '%Y%m%d')
                daily_dir = os.path.join(self.base_dir, day)
                if not os.path.exists(daily_dir):
                    print(f"No directory for {day}")
                    return {"status": f"No images directory for {day}"}

                all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{day}") and f.endswith("_EST.jpg")]
                if not all_images:
                    print(f"No images for {day}")
                    return {"status": f"No images found for {day}"}

                images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
                print(f"Manual send for {day} with {len(images_to_send)} images")
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    functools.partial(self._send_daily_report_sync, images_to_send, timestamp, daily_dir)
                )
                print(f"Manual report sent with {len(images_to_send)} images to {', '.join(self.recipients)}")
                return {"status": f"Sent email with {len(images_to_send)} images for {day}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error sending email: {str(e)}"}
        return {"status": "Unknown command"}

    async def get_readings(self, *, extra: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()
        print(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}")
        if not self.camera:
            return {"error": "No camera available"}
        return {
            "status": "running",
            "last_capture_time": str(self.last_capture_time) if self.last_capture_time else "None",
            "email_status": self.email_status
        }

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())