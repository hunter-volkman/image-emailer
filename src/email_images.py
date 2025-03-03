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
from viam.logging import getLogger
from PIL import Image
from io import BytesIO
import functools
import json
import fasteners

# Viam logger setup
LOGGER = getLogger(__name__)

class EmailImages(Sensor, EasyResource):
    MODEL: ClassVar[Model] = Model(ModelFamily("hunter", "sensor"), "image-emailer")

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]) -> Self:
        sensor = cls(config)
        LOGGER.info(f"Created new EmailImages instance for {config.name} with PID {os.getpid()}")
        sensor._dependencies = dependencies  # Store dependencies for later use
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
        self.send_time = 20       # 8 PM EST
        self.camera = None
        self.camera_name = ""
        self.recipients = []
        self.base_dir = "/home/hunter.volkman/images"
        self.last_capture_time = None
        self.last_sent_date = None
        self.email_status = "not_sent"
        self.capture_loop_task = None
        self.crop_top = 0
        self.crop_left = 0
        self.crop_width = 0
        self.crop_height = 0
        self.state_file = os.path.join(self.base_dir, "state.json")
        self.lock_file = os.path.join(self.base_dir, "lockfile")
        self._load_state()
        LOGGER.info(f"Initialized EmailImages with name: {self.name}, base_dir: {self.base_dir}, PID: {os.getpid()}")

    def _load_state(self):
        """Load persistent state from file."""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
                self.last_sent_date = state.get("last_sent_date")
                self.last_capture_time = (
                    datetime.datetime.fromisoformat(state["last_capture_time"])
                    if state.get("last_capture_time")
                    else None
                )
            LOGGER.info(f"Loaded state: last_sent_date={self.last_sent_date}, last_capture_time={self.last_capture_time}")
        else:
            LOGGER.info(f"No state file at {self.state_file}, starting fresh")

    def _save_state(self):
        """Save state to file for persistence across restarts."""
        state = {
            "last_sent_date": self.last_sent_date,
            "last_capture_time": self.last_capture_time.isoformat() if self.last_capture_time else None
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f)
        LOGGER.debug(f"Saved state to {self.state_file}")

    def reconfigure(self, config: ComponentConfig, dependencies: Mapping[ResourceName, ResourceBase]):
        """Configure the module and start the scheduled loop."""
        attributes = struct_to_dict(config.attributes)
        self.email = attributes["email"]
        self.password = attributes["password"]
        self.timeframe = attributes.get("timeframe", [7, 20])
        self.send_time = int(float(attributes.get("send_time", 20)))
        self.camera_name = attributes["camera"]
        self.recipients = attributes["recipients"]
        self.base_dir = attributes.get("save_dir", "/home/hunter.volkman/images")
        self.crop_top = int(float(attributes.get("crop_top", 0)))
        self.crop_left = int(float(attributes.get("crop_left", 0)))
        self.crop_width = int(float(attributes.get("crop_width", 0)))
        self.crop_height = int(float(attributes.get("crop_height", 0)))

        # Update dependencies on reconfigure
        self._dependencies = dependencies
        LOGGER.info(f"Reconfigured {self.name} with base_dir: {self.base_dir}, last_capture_time: {self.last_capture_time}")

        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

        if self.capture_loop_task:
            self.capture_loop_task.cancel()
        self.capture_loop_task = asyncio.create_task(self.run_scheduled_loop())

    async def run_scheduled_loop(self):
        """Run a scheduled loop waking up at the start of each hour."""
        lock = fasteners.InterProcessLock(self.lock_file)
        if not lock.acquire(blocking=False):
            LOGGER.info(f"Another instance already running (PID {os.getpid()}), exiting")
            return
        try:
            LOGGER.info(f"Started scheduled loop with PID {os.getpid()}")
            while True:
                now = datetime.datetime.now()
                next_hour = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                sleep_seconds = (next_hour - now).total_seconds()
                LOGGER.debug(f"Sleeping for {sleep_seconds:.0f} seconds until {next_hour}")
                await asyncio.sleep(sleep_seconds)

                now = datetime.datetime.now()
                today_str = now.strftime("%Y%m%d")
                start_time, end_time = self.timeframe

                # Capture if within timeframe and new hour
                if start_time <= now.hour < end_time and (self.last_capture_time is None or now.hour > self.last_capture_time.hour):
                    camera_resource_name = ResourceName(
                        namespace="rdk", type="component", subtype="camera", name=self.camera_name
                    )
                    self.camera = self._dependencies.get(camera_resource_name)
                    if not self.camera:
                        LOGGER.error(f"Camera {self.camera_name} not available")
                    else:
                        await self.capture_image(now)
                        self._save_state()
                    self.camera = None  # Reset to avoid holding connection

                # Send email if it's send_time and not sent today
                if now.hour == self.send_time and self.last_sent_date != today_str:
                    await self.send_report(now)
                    self.last_sent_date = today_str
                    self._save_state()
        except Exception as e:
            LOGGER.error(f"Scheduled loop failed: {str(e)}")
        finally:
            lock.release()
            LOGGER.info(f"Released lock, loop exiting (PID {os.getpid()})")

    async def capture_image(self, now):
        """Capture an image with retry logic for flaky connections."""
        for attempt in range(3):
            try:
                LOGGER.info(f"Attempting capture at {now} (attempt {attempt + 1})")
                image = await self.camera.get_image()
                img = Image.open(BytesIO(image.data))
                crop_width = self.crop_width or img.width - self.crop_left
                crop_height = self.crop_height or img.height - self.crop_top
                crop_top = max(0, min(self.crop_top, img.height - 1))
                crop_left = max(0, min(self.crop_left, img.width - 1))
                crop_width = min(crop_width, img.width - crop_left)
                crop_height = min(crop_height, img.height - crop_top)
                cropped_img = img.crop((crop_left, crop_top, crop_left + crop_width, crop_top + crop_height))

                today_str = now.strftime('%Y%m%d')
                daily_dir = os.path.join(self.base_dir, today_str)
                os.makedirs(daily_dir, exist_ok=True)
                filename = f"image_{now.strftime('%Y%m%d_%H%M%S')}_EST.jpg"
                save_path = os.path.join(daily_dir, filename)
                cropped_img.save(save_path, "JPEG")
                self.last_capture_time = now
                LOGGER.info(f"Saved image: {save_path}")
                break
            except Exception as e:
                LOGGER.warning(f"Capture failed (attempt {attempt + 1}): {str(e)}")
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    LOGGER.error(f"All capture attempts failed at {now}")

    async def send_report(self, now):
        """Send a daily report with all captured images."""
        today_str = now.strftime('%Y%m%d')
        daily_dir = os.path.join(self.base_dir, today_str)
        if not os.path.exists(daily_dir):
            LOGGER.info(f"No directory for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{today_str}") and f.endswith("_EST.jpg")]
        if not all_images:
            LOGGER.info(f"No images for {today_str}, skipping report")
            self.email_status = "no_images"
            return

        images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
        try:
            LOGGER.info(f"Sending report with {len(images_to_send)} images at {now}")
            await asyncio.get_running_loop().run_in_executor(
                None,
                functools.partial(self._send_daily_report_sync, images_to_send, now, daily_dir)
            )
            self.email_status = "sent"
            LOGGER.info(f"Sent report with {len(images_to_send)} images to {', '.join(self.recipients)}")
        except Exception as e:
            self.email_status = f"error: {str(e)}"
            LOGGER.error(f"Email send error at {now}: {str(e)}")

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
            LOGGER.info(f"Daily report sent to {msg['To']}")

    async def do_command(self, command: Mapping[str, Any], *, timeout: Optional[float] = None, **kwargs) -> Mapping[str, Any]:
        if command.get("command") == "send_email":
            day = command.get("day", datetime.datetime.now().strftime('%Y%m%d'))
            try:
                timestamp = datetime.datetime.strptime(day, '%Y%m%d')
                daily_dir = os.path.join(self.base_dir, day)
                if not os.path.exists(daily_dir):
                    LOGGER.info(f"No directory for {day}")
                    return {"status": f"No images directory for {day}"}

                all_images = [f for f in os.listdir(daily_dir) if f.startswith(f"image_{day}") and f.endswith("_EST.jpg")]
                if not all_images:
                    LOGGER.info(f"No images for {day}")
                    return {"status": f"No images found for {day}"}

                images_to_send = sorted(all_images, key=lambda x: x.split('_')[1] + x.split('_')[2].split('.')[0])
                LOGGER.info(f"Manual send for {day} with {len(images_to_send)} images")
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    functools.partial(self._send_daily_report_sync, images_to_send, timestamp, daily_dir)
                )
                LOGGER.info(f"Manual report sent with {len(images_to_send)} images to {', '.join(self.recipients)}")
                return {"status": f"Sent email with {len(images_to_send)} images for {day}"}
            except ValueError:
                return {"status": f"Invalid day format: {day}, use YYYYMMDD"}
            except Exception as e:
                return {"status": f"Error sending email: {str(e)}"}
        return {"status": "Unknown command"}

    async def get_readings(self, *, extra: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None, **kwargs) -> Mapping[str, SensorReading]:
        now = datetime.datetime.now()
        LOGGER.info(f"get_readings called for {self.name} at EST {now.strftime('%H:%M:%S')}")
        return {
            "status": "running",
            "last_capture_time": str(self.last_capture_time) if self.last_capture_time else "None",
            "email_status": self.email_status,
            "last_sent_date": self.last_sent_date if self.last_sent_date else "Never",
            "pid": os.getpid()  # Added PID to track the running process
        }

async def main():
    module = Module.from_args()
    module.add_model_from_registry(Sensor.API, EmailImages.MODEL)
    await module.start()

if __name__ == "__main__":
    asyncio.run(main())